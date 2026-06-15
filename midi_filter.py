#!/usr/bin/env python3
"""Filter noisy MIDI controls from one input to a clean virtual output."""

import argparse
import collections
from pathlib import Path
import re
import signal
import sys
import time

import mido


BLOCK_RE = re.compile(r"^(?P<type>cc|note):(?P<channel>\d+):(?P<number>\d+)$")
DEFAULT_BLOCK_FILE = Path(__file__).with_name("blocked_controls.txt")


def parse_block(value):
    match = BLOCK_RE.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            "block rules must look like cc:1:77 or note:10:36"
        )

    message_type = "control_change" if match.group("type") == "cc" else "note"
    human_channel = int(match.group("channel"))
    if not 1 <= human_channel <= 16:
        raise argparse.ArgumentTypeError("MIDI channel must be between 1 and 16")

    number = int(match.group("number"))
    if not 0 <= number <= 127:
        raise argparse.ArgumentTypeError("CC/note number must be between 0 and 127")

    return message_type, human_channel - 1, number


def block_to_text(rule):
    message_type, channel, number = rule
    prefix = "cc" if message_type == "control_change" else "note"
    return f"{prefix}:{channel + 1}:{number}"


def describe_block(rule):
    message_type, channel, number = rule
    label = "CC" if message_type == "control_change" else "note"
    return f"{label} {number} on MIDI channel {channel + 1}"


def load_block_file(path):
    path = Path(path).expanduser()
    if not path.exists():
        return set()

    rules = set()
    with path.open() as block_file:
        for line_number, raw_line in enumerate(block_file, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                rules.add(parse_block(line))
            except argparse.ArgumentTypeError as exc:
                raise SystemExit(f"{path}:{line_number}: {exc}") from exc
    return rules


def write_block_file(path, rules):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# MIDI controls blocked by midifix.",
        "# Format: cc:<midi-channel>:<cc-number> or note:<midi-channel>:<note-number>",
        "# Example: cc:1:77",
        "",
    ]
    lines.extend(block_to_text(rule) for rule in sorted(rules))
    path.write_text("\n".join(lines) + "\n")


def block_file_mtime(path):
    path = Path(path).expanduser()
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def load_all_blocks(args):
    return set(args.block) | load_block_file(args.block_file)


def print_blocks(rules, stream=sys.stdout):
    if not rules:
        print("No blocked MIDI controls.", file=stream)
        return
    for rule in sorted(rules):
        print(f"{block_to_text(rule):<12} {describe_block(rule)}", file=stream)


def message_key(msg):
    if msg.type == "control_change":
        return msg.type, msg.channel, msg.control
    if msg.type in {"note_on", "note_off"}:
        return "note", msg.channel, msg.note
    return None


def should_block(msg, block_rules):
    key = message_key(msg)
    return key in block_rules if key else False


def find_port(name_fragment, names):
    if name_fragment in names:
        return name_fragment

    lowered = name_fragment.lower()
    matches = [name for name in names if lowered in name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(
            f"No MIDI port matching {name_fragment!r}.\n"
            f"Available ports:\n  " + "\n  ".join(names)
        )
    raise SystemExit(
        f"Port name {name_fragment!r} is ambiguous:\n  " + "\n  ".join(matches)
    )


def run_filter(args):
    input_name = find_port(args.input, mido.get_input_names())
    block_rules = load_all_blocks(args)
    last_block_mtime = block_file_mtime(args.block_file)
    stats = collections.Counter()
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"Input:  {input_name}", flush=True)
    print(f"Output: {args.output} (virtual)", flush=True)
    print(f"Block file: {Path(args.block_file).expanduser()}", flush=True)
    print("Blocking:", flush=True)
    print_blocks(block_rules)

    with mido.open_input(input_name) as source, mido.open_output(
        args.output, virtual=True
    ) as destination:
        last_report = time.monotonic()
        last_reload_check = time.monotonic()
        while running:
            for msg in source.iter_pending():
                if should_block(msg, block_rules):
                    stats["blocked"] += 1
                    continue
                destination.send(msg)
                stats["forwarded"] += 1

            now = time.monotonic()
            if args.reload_interval and now - last_reload_check >= args.reload_interval:
                current_mtime = block_file_mtime(args.block_file)
                if current_mtime != last_block_mtime:
                    block_rules = load_all_blocks(args)
                    last_block_mtime = current_mtime
                    print("Reloaded blocked MIDI controls:", flush=True)
                    print_blocks(block_rules)
                last_reload_check = now

            if args.report and now - last_report >= args.report:
                print(
                    f"forwarded={stats['forwarded']} blocked={stats['blocked']}",
                    flush=True,
                )
                last_report = now

            time.sleep(0.001)

    print(
        f"Stopped. Forwarded {stats['forwarded']} messages, "
        f"blocked {stats['blocked']}.",
        flush=True,
    )


def list_ports(_args):
    print("Inputs:")
    for name in mido.get_input_names():
        print(f"  {name}")
    print("Outputs:")
    for name in mido.get_output_names():
        print(f"  {name}")


def list_blocks(args):
    print(f"Block file: {Path(args.block_file).expanduser()}")
    print_blocks(load_block_file(args.block_file))


def add_block(args):
    rules = load_block_file(args.block_file)
    already_present = args.rule in rules
    rules.add(args.rule)
    write_block_file(args.block_file, rules)
    if already_present:
        print(f"Already blocked: {block_to_text(args.rule)}")
    else:
        print(f"Added block: {block_to_text(args.rule)}")


def remove_block(args):
    rules = load_block_file(args.block_file)
    if args.rule not in rules:
        print(f"Not in blocklist: {block_to_text(args.rule)}")
        return
    rules.remove(args.rule)
    write_block_file(args.block_file, rules)
    print(f"Removed block: {block_to_text(args.rule)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True)

    list_parser = subparsers.add_parser("list", help="show MIDI ports")
    list_parser.set_defaults(func=list_ports)

    blocks_parser = subparsers.add_parser("blocks", help="manage blocked controls")
    blocks_parser.add_argument(
        "--block-file",
        default=DEFAULT_BLOCK_FILE,
        help="path to the persistent blocklist",
    )
    block_actions = blocks_parser.add_subparsers(required=True)

    blocks_list_parser = block_actions.add_parser("list", help="show blocked controls")
    blocks_list_parser.set_defaults(func=list_blocks)

    blocks_add_parser = block_actions.add_parser("add", help="add a blocked control")
    blocks_add_parser.add_argument(
        "rule",
        type=parse_block,
        help="control to block, for example cc:1:77",
    )
    blocks_add_parser.set_defaults(func=add_block)

    blocks_remove_parser = block_actions.add_parser(
        "remove", help="remove a blocked control"
    )
    blocks_remove_parser.add_argument(
        "rule",
        type=parse_block,
        help="control to unblock, for example cc:1:77",
    )
    blocks_remove_parser.set_defaults(func=remove_block)

    filter_parser = subparsers.add_parser("filter", help="run a MIDI filter")
    filter_parser.add_argument(
        "--input",
        default="Launch Control XL",
        help="input port name or unique fragment",
    )
    filter_parser.add_argument(
        "--output",
        default="Launch Control XL Filtered",
        help="virtual output name to create",
    )
    filter_parser.add_argument(
        "--block",
        action="append",
        type=parse_block,
        default=[],
        help="message to drop, for example cc:1:77 for fader 1",
    )
    filter_parser.add_argument(
        "--block-file",
        default=DEFAULT_BLOCK_FILE,
        help="path to a persistent blocklist; one rule per line",
    )
    filter_parser.add_argument(
        "--reload-interval",
        type=float,
        default=1,
        help="reload the blocklist every N seconds; use 0 to disable",
    )
    filter_parser.add_argument(
        "--report",
        type=float,
        default=5,
        help="print message counts every N seconds; use 0 to disable",
    )
    filter_parser.set_defaults(func=run_filter)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
