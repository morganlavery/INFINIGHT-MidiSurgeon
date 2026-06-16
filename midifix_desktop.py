#!/usr/bin/env python3
"""Run midifix as a native desktop window."""

import argparse
from pathlib import Path
import threading

import webview

import midifix


def start_server(host, port, block_file):
    port = midifix.find_available_port(port)
    midifix.ACTIVITY_MONITOR.ensure_started()
    state = midifix.MidiFixState(block_file)
    handler = type(
        "DesktopMidiFixHandler",
        (midifix.MidiFixHandler,),
        {"state": state},
    )
    server = midifix.MidiFixServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=midifix.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=midifix.DEFAULT_PORT)
    parser.add_argument(
        "--block-file",
        default=midifix.DEFAULT_BLOCK_FILE,
        help="path to the blocklist controlled by the app",
    )
    args = parser.parse_args(argv)

    server, url = start_server(args.host, args.port, args.block_file)
    try:
        webview.create_window("midifix", url, width=1280, height=900, min_size=(720, 620))
        webview.start(gui="cocoa")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
