#!/usr/bin/env python3
"""Local web UI for turning noisy MIDI controls on and off."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import mido

import midi_filter


ROOT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT_DIR / "controller_templates"
DEFAULT_BLOCK_FILE = ROOT_DIR / "blocked_controls.txt"
DEFAULT_PRESET_DIR = ROOT_DIR / "presets"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def template_files():
    if not TEMPLATE_DIR.exists():
        return []
    return sorted(TEMPLATE_DIR.glob("*.json"))


def load_templates():
    templates = []
    for path in template_files():
        with path.open() as template_file:
            template = json.load(template_file)
        for control in template.get("controls", []):
            for rule in control_rules(control):
                midi_filter.parse_block(rule)
        templates.append(template)
    return templates


def control_rules(control):
    rules = []
    if control.get("rule"):
        rules.append(control["rule"])
    rules.extend(control.get("rules", []))
    return rules


def read_json(request):
    length = int(request.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(request.rfile.read(length).decode("utf-8"))


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "preset"


def rule_text_to_tuple(rule_text):
    return midi_filter.parse_block(rule_text)


def tuple_to_rule_text(rule):
    return midi_filter.block_to_text(rule)


def message_to_rule(msg):
    key = midi_filter.message_key(msg)
    if not key:
        return None
    return tuple_to_rule_text(key)


def match_template_control(templates, rule_text):
    for template in templates:
        for control in template.get("controls", []):
            if control.get("rule") == rule_text:
                return {
                    "template_id": template.get("id"),
                    "template_name": template.get("name"),
                    "control": control,
                }
    return None


def simplify_port_name(name):
    simplified = name.strip()
    changed = True
    while changed:
        changed = False
        for suffix in (" Filtered", " HUI"):
            if simplified.lower().endswith(suffix.lower()):
                simplified = simplified[: -len(suffix)].strip()
                changed = True
    return simplified


def is_virtual_port(name):
    lowered = name.lower()
    return "filtered" in lowered or "iac bus" in lowered


def activity_input_names(requested_input):
    names = mido.get_input_names()
    if requested_input == "__all__":
        physical_names = [name for name in names if not is_virtual_port(name)]
        return physical_names or names
    return [midi_filter.find_port(requested_input, names)]


def template_for_port(templates, port_name):
    lowered = port_name.lower()
    for template in templates:
        candidates = [
            template.get("input_name", ""),
            template.get("output_name", ""),
            template.get("name", ""),
        ]
        candidates.extend(template.get("aliases", []))
        for candidate in candidates:
            candidate = candidate.lower()
            if candidate and (candidate in lowered or lowered in candidate):
                return template
    return None


def discover_controllers(templates, inputs, outputs):
    groups = {}
    for direction, names in (("input", inputs), ("output", outputs)):
        for name in names:
            base_name = simplify_port_name(name)
            key = base_name.lower()
            group = groups.setdefault(
                key,
                {
                    "id": key,
                    "name": base_name,
                    "inputs": [],
                    "outputs": [],
                    "template_id": None,
                    "template_name": None,
                    "input": None,
                    "virtual": True,
                },
            )
            group[f"{direction}s"].append(name)
            if not is_virtual_port(name):
                group["virtual"] = False

    for group in groups.values():
        matched_template = None
        for port_name in group["inputs"] + group["outputs"] + [group["name"]]:
            matched_template = template_for_port(templates, port_name)
            if matched_template:
                break

        if matched_template:
            group["template_id"] = matched_template.get("id")
            group["template_name"] = matched_template.get("name")
            group["name"] = matched_template.get("name") or group["name"]

        preferred_input = matched_template.get("input_name") if matched_template else None
        if preferred_input in group["inputs"]:
            group["input"] = preferred_input
        else:
            physical_inputs = [
                name for name in group["inputs"] if not is_virtual_port(name)
            ]
            group["input"] = (physical_inputs or group["inputs"] or [None])[0]

    def sort_key(group):
        known_rank = 0 if group["template_id"] else 1
        virtual_rank = 1 if group["virtual"] else 0
        return known_rank, virtual_rank, group["name"].lower()

    return sorted(groups.values(), key=sort_key)


class MidiFixState:
    def __init__(self, block_file, preset_dir=None):
        self.block_file = Path(block_file).expanduser()
        self.preset_dir = Path(preset_dir).expanduser() if preset_dir else self.block_file.parent / "presets"

    def load_rules(self):
        return midi_filter.load_block_file(self.block_file)

    def save_rules(self, rules):
        midi_filter.write_block_file(self.block_file, rules)

    def set_blocked(self, rule_text, blocked):
        rule = rule_text_to_tuple(rule_text)
        rules = self.load_rules()
        if blocked:
            rules.add(rule)
        else:
            rules.discard(rule)
        self.save_rules(rules)
        return tuple_to_rule_text(rule), rules

    def set_rules(self, rule_texts):
        rules = {rule_text_to_tuple(rule_text) for rule_text in rule_texts}
        self.save_rules(rules)
        return rules

    def list_presets(self):
        if not self.preset_dir.exists():
            return []

        presets = []
        for path in sorted(self.preset_dir.glob("*.json")):
            try:
                presets.append(self.load_preset(path.stem))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(
            presets,
            key=lambda item: (item.get("updated_at") or "", item.get("name") or ""),
            reverse=True,
        )

    def preset_path(self, preset_id):
        if not preset_id:
            raise ValueError("preset id is required")
        if preset_id != slugify(preset_id):
            raise ValueError("invalid preset id")
        return self.preset_dir / f"{preset_id}.json"

    def load_preset(self, preset_id):
        path = self.preset_path(preset_id)
        if not path.exists():
            raise ValueError("preset not found")
        with path.open() as preset_file:
            data = json.load(preset_file)

        name = str(data.get("name") or path.stem).strip() or path.stem
        rules = sorted(
            tuple_to_rule_text(rule_text_to_tuple(rule_text))
            for rule_text in data.get("blocked_rules", [])
        )
        return {
            "id": path.stem,
            "name": name,
            "template_id": data.get("template_id"),
            "template_name": data.get("template_name"),
            "input_name": data.get("input_name"),
            "blocked_rules": rules,
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }

    def unique_preset_id(self, name, current_id=None):
        base = slugify(name)
        if current_id:
            return current_id
        candidate = base
        index = 2
        while self.preset_path(candidate).exists():
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def save_preset(self, payload):
        current_id = payload.get("id") or None
        existing = None
        if current_id:
            existing = self.load_preset(current_id)

        name = str(payload.get("name") or existing.get("name") if existing else payload.get("name") or "").strip()
        if not name:
            raise ValueError("preset name is required")

        preset_id = self.unique_preset_id(name, current_id)
        rule_texts = payload.get("blocked_rules", [])
        if not isinstance(rule_texts, list):
            raise ValueError("blocked_rules must be a list")

        rules = sorted(
            tuple_to_rule_text(rule_text_to_tuple(rule_text))
            for rule_text in rule_texts
        )
        now = timestamp()
        preset = {
            "id": preset_id,
            "name": name,
            "template_id": payload.get("template_id"),
            "template_name": payload.get("template_name"),
            "input_name": payload.get("input_name"),
            "blocked_rules": rules,
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }

        self.preset_dir.mkdir(parents=True, exist_ok=True)
        path = self.preset_path(preset_id)
        temp_path = path.with_suffix(".tmp")
        with temp_path.open("w") as preset_file:
            json.dump(preset, preset_file, indent=2)
            preset_file.write("\n")
        temp_path.replace(path)
        return preset

    def delete_preset(self, preset_id):
        path = self.preset_path(preset_id)
        if not path.exists():
            raise ValueError("preset not found")
        path.unlink()


class ActivityMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.next_id = 1
        self.inputs = []
        self.outputs = []
        self.errors = []
        self.thread = None
        self.running = False

    def ensure_started(self):
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def snapshot(self, after_id=0):
        self.ensure_started()
        with self.lock:
            events = [event for event in self.events if event["id"] > after_id]
            return {
                "events": events,
                "inputs": list(self.inputs),
                "outputs": list(self.outputs),
                "errors": list(self.errors),
                "latest_id": self.events[-1]["id"] if self.events else after_id,
            }

    def add_event(self, port_name, msg):
        rule = message_to_rule(msg)
        if not rule:
            return
        with self.lock:
            event = {
                "id": self.next_id,
                "rule": rule,
                "input": port_name,
                "message": str(msg),
            }
            self.next_id += 1
            self.events.append(event)
            self.events = self.events[-200:]

    def update_ports(self):
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
        with self.lock:
            self.inputs = inputs
            self.outputs = outputs
        return [name for name in inputs if not is_virtual_port(name)] or inputs

    def run(self):
        sources = {}
        last_port_refresh = 0
        last_sent = {}
        while self.running:
            try:
                now = time.monotonic()
                if now - last_port_refresh >= 2:
                    desired_names = set(self.update_ports())
                    for name in list(sources):
                        if name not in desired_names:
                            sources.pop(name).close()
                    for name in desired_names:
                        if name in sources:
                            continue
                        try:
                            sources[name] = mido.open_input(name)
                        except Exception as exc:
                            with self.lock:
                                self.errors = [f"{name}: {exc}"]
                    last_port_refresh = now

                for port_name, source in list(sources.items()):
                    for msg in source.iter_pending():
                        rule = message_to_rule(msg)
                        if not rule:
                            continue
                        throttle_key = (port_name, rule)
                        if now - last_sent.get(throttle_key, 0) < 0.04:
                            continue
                        last_sent[throttle_key] = now
                        self.add_event(port_name, msg)

                time.sleep(0.01)
            except Exception as exc:
                with self.lock:
                    self.errors = [str(exc)]
                time.sleep(0.5)


ACTIVITY_MONITOR = ActivityMonitor()


def safe_port_names(kind):
    try:
        if kind == "input":
            return mido.get_input_names()
        return mido.get_output_names()
    except Exception as exc:
        return {"error": str(exc), "ports": []}


def find_available_port(preferred_port):
    port = preferred_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        while sock.connect_ex((DEFAULT_HOST, port)) == 0:
            port += 1
    return port


def build_app_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>INFINIGHT MidiSurgeon</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #28140f;
      --muted: #6b4930;
      --line: #d19a2d;
      --panel: #ffef83;
      --surface: #ffe04d;
      --accent: #c91822;
      --accent-weak: #fff3a6;
      --passing: #18a957;
      --passing-weak: #d9f6c2;
      --danger: #bd111b;
      --danger-weak: #f9c9bd;
      --warning: #ffd84f;
      --warning-strong: #d79a00;
      --activity: #ffffff;
      --activity-strong: #e00013;
      --shadow: 0 22px 48px rgba(56, 26, 16, 0.28);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background:
        radial-gradient(circle at 22px 22px, rgba(201, 24, 34, 0.14) 0 2px, transparent 3px) 0 0/44px 44px,
        linear-gradient(#f6f0df, #e8dcc7);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    button, select {
      font: inherit;
    }

    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      display: flex;
      gap: 18px;
      align-items: center;
      justify-content: space-between;
      padding: 14px 26px;
      background: #c91822;
      border-bottom: 5px solid #8f0b15;
      color: #ffe45b;
    }

    h1 {
      margin: 0;
      max-width: 560px;
      font-size: 26px;
      line-height: 1;
      font-weight: 760;
      overflow-wrap: anywhere;
    }

    .status-panel {
      position: relative;
      display: grid;
      justify-items: end;
      gap: 4px;
      min-width: min(420px, 52vw);
    }

    .midi-status-button {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      max-width: 100%;
      min-height: 30px;
      border: 2px solid rgba(255, 246, 183, 0.74);
      border-radius: 7px;
      background: rgba(143, 11, 21, 0.34);
      color: #fff3a6;
      padding: 4px 9px 5px 11px;
      font: inherit;
      font-size: 14px;
      font-weight: 800;
      cursor: pointer;
    }

    .midi-status-button:focus-visible {
      outline: 3px solid #fff6b7;
      outline-offset: 2px;
    }

    .midi-status-label {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .midi-status-chevron {
      width: 0;
      height: 0;
      border-left: 5px solid transparent;
      border-right: 5px solid transparent;
      border-top: 6px solid currentColor;
      flex: 0 0 auto;
    }

    .midi-popover {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      z-index: 20;
      width: min(360px, calc(100vw - 28px));
      border: 3px solid #31100f;
      border-radius: 8px;
      background: #fff6b7;
      color: var(--ink);
      box-shadow: 6px 6px 0 #8f0b15, 0 14px 30px rgba(56, 26, 16, 0.28);
      text-align: left;
      overflow: hidden;
    }

    .midi-popover[hidden] {
      display: none;
    }

    .midi-popover-title {
      padding: 10px 12px 8px;
      border-bottom: 2px solid rgba(49, 16, 15, 0.18);
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
      color: var(--muted);
    }

    .midi-device-list {
      display: grid;
      max-height: 280px;
      overflow: auto;
    }

    .midi-device {
      width: 100%;
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      gap: 9px;
      align-items: center;
      border: 0;
      border-bottom: 1px solid rgba(49, 16, 15, 0.14);
      background: transparent;
      color: var(--ink);
      padding: 10px 12px;
      text-align: left;
      font: inherit;
      cursor: pointer;
    }

    .midi-device:last-child {
      border-bottom: 0;
    }

    .midi-device:hover,
    .midi-device:focus-visible {
      background: #ffef83;
      outline: none;
    }

    .midi-device.selected {
      background: #dff4c7;
    }

    .midi-device-dot {
      width: 10px;
      height: 10px;
      border: 2px solid #31100f;
      border-radius: 50%;
      background: var(--passing);
      box-shadow: 0 0 0 2px rgba(24, 169, 87, 0.2);
    }

    .midi-device.receiving .midi-device-dot {
      background: var(--warning);
      box-shadow: 0 0 0 3px rgba(255, 216, 79, 0.5), 0 0 16px rgba(215, 154, 0, 0.62);
    }

    .midi-device-name,
    .midi-device-meta {
      display: block;
      overflow-wrap: anywhere;
    }

    .midi-device-name {
      font-size: 14px;
      line-height: 1.15;
      font-weight: 800;
    }

    .midi-device-meta {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      font-weight: 650;
    }

    .midi-popover-empty {
      padding: 13px 12px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }

    .status {
      min-height: 24px;
      max-width: 100%;
      color: #fff3a6;
      font-size: 14px;
      text-align: right;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    main {
      width: min(1220px, 100%);
      margin: 0 auto;
      padding: 22px;
    }

    .operation-board {
      position: relative;
      display: grid;
      grid-template-columns: 132px minmax(0, 1fr);
      gap: 18px;
      min-height: calc(100vh - 112px);
      padding: 22px;
      border: 18px solid #d20d18;
      border-radius: 28px;
      background:
        radial-gradient(circle at 6% 7%, #d20d18 0 50px, transparent 51px),
        linear-gradient(135deg, rgba(255, 255, 255, 0.16), transparent 34%),
        #f6d936;
      box-shadow:
        inset 0 0 0 4px #a50812,
        inset 0 0 0 8px rgba(255, 255, 255, 0.22),
        var(--shadow);
      overflow: hidden;
    }

    .operation-board::before,
    .operation-board::after {
      content: "";
      position: absolute;
      width: 13px;
      height: 13px;
      border-radius: 50%;
      background: radial-gradient(circle at 35% 35%, #ffffff, #9a9a9a 65%, #565656);
    }

    .operation-board::before {
      left: 14px;
      top: 14px;
    }

    .operation-board::after {
      right: 14px;
      bottom: 14px;
    }

    .patient-overlay {
      width: min(162px, 130%);
      height: auto;
      max-width: none;
      opacity: 0.82;
      pointer-events: none;
      transform: rotate(-7deg);
      transform-origin: 52% 52%;
    }

    .patient-shadow {
      fill: rgba(100, 54, 18, 0.16);
    }

    .patient-skin {
      fill: #ffd3b9;
      stroke: #67291d;
      stroke-width: 7;
      stroke-linejoin: round;
    }

    .patient-suit {
      fill: #7ed1d4;
      stroke: #67291d;
      stroke-width: 7;
      stroke-linejoin: round;
    }

    .patient-line {
      fill: none;
      stroke: #67291d;
      stroke-width: 7;
      stroke-linecap: round;
      stroke-linejoin: round;
    }

    .patient-detail {
      fill: #fff6b7;
      stroke: #67291d;
      stroke-width: 5;
      stroke-linejoin: round;
    }

    .patient-red {
      fill: #d20d18;
      stroke: #67291d;
      stroke-width: 5;
    }

    .patient-socket {
      fill: #f8f2d3;
      stroke: #d20d18;
      stroke-width: 6;
      stroke-dasharray: 9 8;
      filter: drop-shadow(0 5px 0 rgba(103, 41, 29, 0.18));
    }

    .patient-spark {
      fill: none;
      stroke: #d20d18;
      stroke-width: 6;
      stroke-linecap: round;
    }

    .patient-label {
      fill: #67291d;
      font: 900 25px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    .brand-rail,
    .playfield {
      position: relative;
      z-index: 2;
    }

    .brand-rail {
      min-height: 560px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      align-items: center;
      justify-items: center;
      gap: 12px;
      padding: 26px 0 12px;
    }

    .skill-game {
      width: 100%;
      color: #1f160f;
      font-size: 26px;
      line-height: 0.9;
      font-weight: 900;
      text-align: center;
      transform: rotate(-90deg);
      transform-origin: center;
      letter-spacing: 0;
    }

    .vertical-logo {
      display: grid;
      justify-items: center;
      gap: 8px;
      color: #c91822;
      font-family: Georgia, "Times New Roman", serif;
      text-align: center;
      text-shadow: 2px 2px 0 #ffed70;
    }

    .brand-mark {
      color: #67291d;
      font: 900 16px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    .product-mark {
      font-size: 64px;
      line-height: 0.82;
      font-weight: 900;
      letter-spacing: 0;
      writing-mode: vertical-rl;
      transform: rotate(180deg);
    }

    .playfield {
      min-width: 0;
      padding-top: 70px;
    }

    .break-sticker {
      position: absolute;
      top: 16px;
      right: 18px;
      z-index: 3;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      max-width: min(360px, calc(100% - 36px));
      padding: 8px 13px 9px 10px;
      border: 3px solid #31100f;
      border-radius: 8px;
      background: #fff6b7;
      color: var(--ink);
      box-shadow:
        4px 4px 0 #8f0b15,
        0 10px 22px rgba(56, 26, 16, 0.22);
      pointer-events: none;
      transform: rotate(2deg);
    }

    .break-copy {
      font-size: 17px;
      line-height: 0.95;
      font-weight: 900;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .poop-icon {
      position: relative;
      width: 42px;
      height: 34px;
      flex: 0 0 42px;
    }

    .poop-icon span {
      position: absolute;
      display: block;
      border: 2px solid #31100f;
      background: #6b351d;
      box-shadow: inset 3px 3px 0 rgba(255, 246, 183, 0.24);
    }

    .poop-base {
      left: 2px;
      right: 2px;
      bottom: 0;
      height: 16px;
      border-radius: 16px 16px 10px 10px;
    }

    .poop-mid {
      left: 9px;
      bottom: 11px;
      width: 25px;
      height: 15px;
      border-radius: 15px 15px 10px 10px;
    }

    .poop-top {
      left: 17px;
      bottom: 23px;
      width: 13px;
      height: 11px;
      border-radius: 12px 12px 8px 8px;
      transform: rotate(-14deg);
    }

    .poop-shine {
      left: 12px;
      bottom: 7px;
      width: 6px;
      height: 4px;
      border: 0;
      border-radius: 50%;
      background: rgba(255, 246, 183, 0.62);
      box-shadow: none;
    }

    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto auto auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
    }

    .preset-bar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 18px;
    }

    select, .button {
      height: 40px;
      border: 2px solid #8f0b15;
      border-radius: 7px;
      background: #fff6b7;
      color: var(--ink);
      padding: 0 12px;
      font-weight: 700;
    }

    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      cursor: pointer;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.8) inset;
    }

    .button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff7bf;
    }

    .button.apply {
      background: var(--passing);
      border-color: #0f7d3d;
      color: #f7fff4;
    }

    .button.danger {
      background: var(--danger);
      border-color: #8f0b15;
      color: #fff7bf;
    }

    .button:disabled {
      cursor: progress;
      opacity: 0.65;
    }

    .controllers {
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 12px;
      align-items: stretch;
      margin-bottom: 18px;
    }

    .connected-title {
      display: flex;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .controller-list {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
    }

    .controller-card {
      min-height: 70px;
      display: grid;
      gap: 4px;
      align-content: center;
      text-align: left;
      border: 2px solid #c18421;
      border-radius: 8px;
      background: #fff0a2;
      color: var(--ink);
      padding: 10px 12px;
      cursor: pointer;
    }

    .controller-card.recognized {
      border-color: #258b72;
      background: #dff4c7;
    }

    .controller-card.selected {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(201, 24, 34, 0.26);
    }

    .controller-card.virtual {
      color: var(--muted);
      background: #f9dea0;
    }

    .controller-name {
      font-size: 15px;
      font-weight: 760;
      overflow-wrap: anywhere;
    }

    .controller-meta {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .board {
      background:
        radial-gradient(circle at 82% 12%, rgba(255, 255, 255, 0.22), transparent 12%),
        #f6d936;
      border: 3px solid #c18421;
      box-shadow: inset 0 0 0 2px rgba(255, 255, 255, 0.35);
      border-radius: 18px;
      padding: 18px;
    }

    .group {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 12px;
      align-items: center;
      padding: 14px 0;
      border-top: 2px dashed rgba(122, 72, 21, 0.35);
    }

    .group:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .group:last-child {
      padding-bottom: 0;
    }

    .group-title {
      color: #442315;
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      text-align: right;
    }

    .controls {
      display: grid;
      grid-template-columns: repeat(8, minmax(62px, 1fr));
      gap: 10px;
    }

    .control {
      position: relative;
      min-height: 86px;
      display: grid;
      grid-template-rows: 1fr auto auto;
      justify-items: center;
      gap: 5px;
      padding: 10px 6px 8px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #ffd9c7;
      color: var(--ink);
      cursor: pointer;
      box-shadow:
        inset 0 0 0 3px #833222,
        inset 0 4px 12px rgba(74, 14, 10, 0.22),
        0 2px 0 rgba(255, 255, 255, 0.35);
    }

    .control.blocked {
      border-color: #8f0b15;
      background: #f8b3a5;
      color: #5f1010;
    }

    .control.active:not(.visual-only) {
      border-color: #0f7d3d;
      background: var(--passing-weak);
      box-shadow:
        inset 0 0 0 3px #0f7d3d,
        inset 0 4px 12px rgba(15, 125, 61, 0.2),
        0 0 0 2px rgba(24, 169, 87, 0.22),
        0 2px 0 rgba(255, 255, 255, 0.35);
    }

    .control.moving {
      border-color: var(--warning-strong);
      background: var(--warning);
      box-shadow:
        0 0 0 4px rgba(255, 255, 255, 0.9),
        0 0 24px rgba(215, 154, 0, 0.78);
      transform: translateY(-1px);
      animation: activityPulse 0.34s ease-in-out infinite alternate;
    }

    .control.moving .knob-face,
    .control.moving .fader-face::after,
    .control.moving .pad-face,
    .control.moving .button-face {
      border-color: var(--warning-strong);
      background-color: var(--warning);
    }

    .control.pending-change::after {
      content: "";
      position: absolute;
      top: 7px;
      right: 7px;
      width: 11px;
      height: 11px;
      border: 2px solid rgba(49, 16, 15, 0.72);
      border-radius: 50%;
      background: #fff6b7;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.65) inset;
    }

    @keyframes activityPulse {
      from {
        filter: saturate(1);
      }
      to {
        filter: saturate(1.35) brightness(1.08);
      }
    }

    .knob-face {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      border: 8px solid #31100f;
      background: radial-gradient(circle at 50% 50%, #d8d1c4 0 28%, #5c1616 29% 100%);
    }

    .fader-face {
      width: 34px;
      height: 44px;
      border-radius: 8px;
      background:
        linear-gradient(#31100f, #31100f) center/4px 100% no-repeat,
        linear-gradient(#f2d6c5, #9b4a3a);
      border: 1px solid #5a2019;
      position: relative;
    }

    .fader-face::after {
      content: "";
      position: absolute;
      left: 4px;
      right: 4px;
      top: 14px;
      height: 12px;
      border-radius: 5px;
      background: #461915;
    }

    .pad-face {
      width: 40px;
      height: 32px;
      border-radius: 7px;
      border: 2px solid #451814;
      background: linear-gradient(#a02325, #4e1718);
      box-shadow: inset 0 0 0 5px rgba(255, 255, 255, 0.08);
    }

    .key-face {
      width: 24px;
      height: 44px;
      border-radius: 0 0 5px 5px;
      border: 1px solid #7b4d2a;
      background: linear-gradient(#ffffff, #dbe3ea);
    }

    .key-face.black {
      width: 18px;
      height: 36px;
      border-color: #1e130f;
      background: linear-gradient(#3b2922, #12100d);
    }

    .wheel-face,
    .strip-face {
      width: 30px;
      height: 44px;
      border-radius: 14px;
      border: 1px solid #4c1a17;
      background: linear-gradient(#7d1f21, #32110f);
    }

    .strip-face {
      width: 20px;
      background: linear-gradient(#a61f25, #32110f);
    }

    .screen-face {
      width: 52px;
      height: 26px;
      border-radius: 5px;
      border: 1px solid #4c1a17;
      background: linear-gradient(#fff0a2, #d6232d);
      box-shadow: inset 0 0 8px rgba(255, 255, 255, 0.35);
    }

    .button-face {
      width: 34px;
      height: 24px;
      border-radius: 6px;
      border: 1px solid #5a2019;
      background: linear-gradient(#f8d7c8, #9b4a3a);
    }

    .control.blocked .knob-face,
    .control.blocked .fader-face::after,
    .control.blocked .pad-face,
    .control.blocked .button-face {
      border-color: var(--danger);
      background-color: var(--danger);
    }

    .control.active:not(.visual-only) .knob-face,
    .control.active:not(.visual-only) .fader-face::after,
    .control.active:not(.visual-only) .pad-face,
    .control.active:not(.visual-only) .button-face {
      border-color: #0f7d3d;
      background-color: var(--passing);
    }

    .control.visual-only {
      cursor: default;
      opacity: 0.86;
    }

    .control:disabled {
      color: var(--ink);
    }

    .label {
      font-size: 14px;
      font-weight: 740;
    }

    .rule {
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      white-space: nowrap;
    }

    .lcxl-board {
      max-width: 900px;
      margin: 0 auto;
      padding: 14px;
      border: 3px solid #151719;
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.12), transparent 38%),
        #2c3032;
      color: #f1f2ec;
      box-shadow:
        inset 0 0 0 2px rgba(255, 255, 255, 0.08),
        inset 0 0 40px rgba(0, 0, 0, 0.28),
        0 18px 34px rgba(58, 29, 13, 0.28);
      overflow-x: auto;
    }

    .lcxl-panel {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 122px;
      gap: 12px;
      min-width: 826px;
    }

    .lcxl-brand {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 28px;
      padding: 0 8px 2px;
      color: #f2f4ef;
      font-weight: 850;
      letter-spacing: 0;
      text-transform: uppercase;
    }

    .lcxl-brand .novation {
      font-size: 18px;
    }

    .lcxl-brand .launch {
      color: #ff7b2e;
    }

    .lcxl-brand .model {
      margin-left: 4px;
      padding: 0 4px;
      border: 1px solid rgba(241, 242, 236, 0.75);
      border-radius: 3px;
      font-size: 11px;
      vertical-align: 2px;
    }

    .lcxl-main {
      display: grid;
      gap: 10px;
      min-width: 0;
    }

    .lcxl-side {
      display: grid;
      align-content: start;
      gap: 12px;
      padding: 8px 4px 0;
      border-left: 1px solid rgba(241, 242, 236, 0.28);
    }

    .lcxl-row,
    .lcxl-side-pair,
    .lcxl-side-stack {
      position: relative;
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 8px;
      padding-top: 15px;
    }

    .lcxl-row::before,
    .lcxl-side-pair::before,
    .lcxl-side-stack::before {
      content: attr(data-label);
      position: absolute;
      left: 2px;
      top: 0;
      color: #aeb5b3;
      font-size: 10px;
      line-height: 1;
      font-weight: 700;
    }

    .lcxl-side-pair,
    .lcxl-side-stack {
      grid-template-columns: 1fr 1fr;
      gap: 7px;
      padding-top: 13px;
    }

    .lcxl-side-stack {
      grid-template-columns: 1fr;
    }

    .lcxl-fader-row {
      min-height: 176px;
      align-items: stretch;
      padding-top: 10px;
      border-top: 1px solid rgba(241, 242, 236, 0.18);
      border-bottom: 1px solid rgba(241, 242, 236, 0.18);
    }

    .lcxl-channel-row {
      padding-top: 14px;
    }

    .lcxl-board .control {
      min-width: 0;
      min-height: 66px;
      padding: 6px 4px 5px;
      gap: 3px;
      border-color: #17191a;
      border-radius: 5px;
      background: #34383a;
      color: #edf0ea;
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.08),
        0 1px 0 rgba(255, 255, 255, 0.08);
    }

    .lcxl-board .control.active:not(.visual-only) {
      background: rgba(24, 169, 87, 0.2);
      border-color: var(--passing);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.08),
        0 0 0 2px rgba(24, 169, 87, 0.55),
        0 0 14px rgba(24, 169, 87, 0.34);
    }

    .lcxl-board .control.visual-only {
      opacity: 0.78;
    }

    .lcxl-board .control.blocked {
      background: #4f2424;
      border-color: #ef3340;
      color: #fff1ed;
    }

    .lcxl-board .control.moving {
      border-color: #f5f5f0;
      box-shadow:
        0 0 0 3px rgba(255, 255, 255, 0.82),
        0 0 22px rgba(255, 123, 46, 0.76);
    }

    .lcxl-board .type-knob {
      min-height: 76px;
      background: transparent;
      border-color: transparent;
      box-shadow: none;
    }

    .lcxl-board .type-fader {
      min-height: 160px;
      grid-template-rows: 1fr auto auto;
      background: transparent;
      border-color: transparent;
      box-shadow: none;
    }

    .lcxl-board .type-button {
      min-height: 46px;
      padding: 5px 4px;
      border-radius: 4px;
    }

    .lcxl-board .knob-face {
      width: 42px;
      height: 42px;
      border: 5px solid #101112;
      background:
        linear-gradient(#f3f3ed, #f3f3ed) 50% 6px/4px 13px no-repeat,
        radial-gradient(circle at 50% 42%, #414547 0 45%, #151719 46% 100%);
      box-shadow:
        0 0 0 2px #b7bdba,
        0 0 0 4px #292d2f,
        0 5px 8px rgba(0, 0, 0, 0.38);
    }

    .lcxl-board .fader-face {
      width: 34px;
      height: 126px;
      border: 0;
      border-radius: 3px;
      background:
        repeating-linear-gradient(to bottom, transparent 0 15px, rgba(241, 242, 236, 0.72) 15px 17px, transparent 17px 27px) 27px 0/7px 100% no-repeat,
        linear-gradient(#090a0b, #090a0b) 13px 0/6px 100% no-repeat;
    }

    .lcxl-board .fader-face::after {
      left: -6px;
      right: -6px;
      top: 46px;
      height: 18px;
      border-radius: 3px;
      background: linear-gradient(#f0f0ea, #858b8c);
      border: 1px solid #17191a;
      box-shadow: 0 3px 8px rgba(0, 0, 0, 0.42);
    }

    .lcxl-board .button-face {
      width: 38px;
      height: 20px;
      border-color: #17191a;
      border-radius: 4px;
      background: linear-gradient(#e8ece8, #a5abaa);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.28);
    }

    .lcxl-board .control.active:not(.visual-only) .knob-face {
      box-shadow:
        0 0 0 2px #b7bdba,
        0 0 0 5px var(--passing),
        0 0 16px rgba(24, 169, 87, 0.62);
    }

    .lcxl-board .control.active:not(.visual-only) .fader-face::after,
    .lcxl-board .control.active:not(.visual-only) .button-face {
      border-color: #0f7d3d;
      background: linear-gradient(#7df0a8, #18a957);
    }

    .lcxl-board .control-template-user .button-face,
    .lcxl-board .control-track-control-1 .button-face,
    .lcxl-board .control-track-control-2 .button-face,
    .lcxl-board .control-track-control-3 .button-face,
    .lcxl-board .control-track-control-4 .button-face,
    .lcxl-board .control-track-control-5 .button-face,
    .lcxl-board .control-track-control-6 .button-face,
    .lcxl-board .control-track-control-7 .button-face,
    .lcxl-board .control-track-control-8 .button-face {
      background: linear-gradient(#f36b63, #b92c31);
    }

    .lcxl-board .control-track-focus-1 .button-face,
    .lcxl-board .control-track-focus-2 .button-face,
    .lcxl-board .control-track-focus-3 .button-face,
    .lcxl-board .control-track-focus-4 .button-face,
    .lcxl-board .control-track-focus-5 .button-face,
    .lcxl-board .control-track-focus-6 .button-face,
    .lcxl-board .control-track-focus-7 .button-face,
    .lcxl-board .control-track-focus-8 .button-face {
      background: linear-gradient(#9de1cf, #258b72);
    }

    .lcxl-board .control-send-select-up .button-face,
    .lcxl-board .control-send-select-down .button-face,
    .lcxl-board .control-track-select-left .button-face,
    .lcxl-board .control-track-select-right .button-face {
      background: linear-gradient(#f5f6f1, #8f9798);
    }

    .lcxl-board .control-device .button-face,
    .lcxl-board .control-mute .button-face,
    .lcxl-board .control-solo .button-face,
    .lcxl-board .control-record-arm .button-face {
      background: linear-gradient(#7ec4ff, #2785c6);
    }

    .lcxl-board .control.active:not(.visual-only) .fader-face::after,
    .lcxl-board .control.active:not(.visual-only) .button-face {
      border-color: #0f7d3d;
      background: linear-gradient(#7df0a8, #18a957);
    }

    .lcxl-board .label {
      max-width: 100%;
      font-size: 11px;
      line-height: 1.05;
      overflow-wrap: anywhere;
      text-align: center;
    }

    .lcxl-board .rule {
      color: #b9c0be;
      font-size: 9px;
    }

    .lcxl-board .visual-only .rule {
      display: none;
    }

    .lpx-board {
      width: min(760px, 100%);
      margin: 0 auto;
      padding: 16px;
      border: 3px solid #070809;
      border-radius: 12px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.09), transparent 34%),
        #121416;
      color: #f3f5ef;
      box-shadow:
        inset 0 0 0 2px rgba(255, 255, 255, 0.05),
        inset 0 0 40px rgba(0, 0, 0, 0.48),
        0 18px 34px rgba(58, 29, 13, 0.28);
      overflow-x: auto;
    }

    .lpx-panel {
      display: grid;
      gap: 8px;
      min-width: 556px;
    }

    .lpx-brand {
      min-height: 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 3px 2px;
      color: #edf1eb;
      font-weight: 850;
      text-transform: uppercase;
    }

    .lpx-brand .novation {
      font-size: 18px;
      text-transform: lowercase;
    }

    .lpx-brand .model {
      color: #bec6c3;
      font-size: 12px;
      letter-spacing: 0;
    }

    .lpx-top-shell,
    .lpx-main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 58px;
      gap: 8px;
      align-items: stretch;
    }

    .lpx-top-row,
    .lpx-pad-grid {
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 6px;
    }

    .lpx-top-row {
      align-items: stretch;
    }

    .lpx-logo-tile {
      display: grid;
      place-items: center;
      min-height: 44px;
      border: 1px solid #30363a;
      border-radius: 4px;
      background: #202528;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.06);
    }

    .lpx-logo-mark {
      width: 26px;
      height: 26px;
      border-radius: 4px;
      background:
        linear-gradient(135deg, transparent 0 34%, #eef2ed 35% 63%, transparent 64%) center/100% 100% no-repeat,
        linear-gradient(45deg, transparent 0 26%, #eef2ed 27% 48%, transparent 49%) center/100% 100% no-repeat;
      transform: rotate(-4deg);
    }

    .lpx-pad-grid {
      align-content: start;
    }

    .lpx-grid-row {
      display: contents;
    }

    .lpx-side {
      display: grid;
      grid-template-rows: repeat(8, minmax(0, 1fr));
      gap: 6px;
    }

    .lpx-board .control {
      min-width: 0;
      min-height: 44px;
      padding: 4px;
      gap: 2px;
      border-color: #070809;
      border-radius: 4px;
      background: #202528;
      color: #edf1eb;
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.07),
        0 1px 0 rgba(255, 255, 255, 0.06);
    }

    .lpx-board .control.visual-only {
      opacity: 0.92;
    }

    .lpx-board .control.active:not(.visual-only) {
      background: #202528;
      border-color: var(--passing);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.07),
        0 0 0 2px rgba(24, 169, 87, 0.52),
        0 0 14px rgba(24, 169, 87, 0.28);
    }

    .lpx-board .control.blocked {
      background: #4b1e23;
      border-color: #ef3340;
      color: #fff0eb;
    }

    .lpx-board .control.moving {
      border-color: #f4f4ee;
      box-shadow:
        0 0 0 3px rgba(255, 255, 255, 0.82),
        0 0 24px rgba(0, 210, 255, 0.78);
    }

    .lpx-board .type-pad {
      aspect-ratio: 1;
      min-height: 54px;
      grid-template-rows: 1fr auto;
      padding: 5px;
    }

    .lpx-board .type-button {
      min-height: 44px;
    }

    .lpx-board .pad-face {
      width: 100%;
      height: auto;
      aspect-ratio: 1;
      border-color: #070809;
      border-radius: 6px;
      background: linear-gradient(#f6ef7d, #e0c52d);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(246, 239, 125, 0.22);
    }

    .lpx-grid-row .type-pad:nth-child(2) .pad-face {
      background: linear-gradient(#ff8079, #dc353d);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(255, 70, 74, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(3) .pad-face {
      background: linear-gradient(#80cdff, #2d8bd4);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(72, 167, 240, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(4) .pad-face {
      background: linear-gradient(#ff7cf0, #c830bf);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(255, 92, 232, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(5) .pad-face {
      background: linear-gradient(#75f4f1, #25babe);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(87, 229, 230, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(6) .pad-face {
      background: linear-gradient(#7ef97b, #31cc4f);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(93, 235, 98, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(7) .pad-face {
      background: linear-gradient(#ffe98a, #d99d27);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(255, 213, 84, 0.24);
    }

    .lpx-grid-row .type-pad:nth-child(8) .pad-face {
      background: linear-gradient(#bd8bff, #7d43d8);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.28),
        inset 0 -7px 12px rgba(0, 0, 0, 0.18),
        0 0 14px rgba(170, 117, 255, 0.24);
    }

    .lpx-board .button-face {
      width: 100%;
      height: 19px;
      border-color: #070809;
      border-radius: 3px;
      background: linear-gradient(#30383c, #171b1e);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
    }

    .lpx-board .control-mode-session .button-face {
      background: linear-gradient(#8a85ff, #5149c9);
    }

    .lpx-board .control-mode-note .button-face {
      background: linear-gradient(#a2e878, #43ba4f);
    }

    .lpx-board .control-mode-custom .button-face {
      background: linear-gradient(#f6d86f, #ca9432);
    }

    .lpx-board .control-capture-midi .button-face {
      background: linear-gradient(#ff8782, #ca3340);
    }

    .lpx-board .control-scene-volume .button-face,
    .lpx-board .control-scene-pan .button-face,
    .lpx-board .control-scene-send-a .button-face,
    .lpx-board .control-scene-send-b .button-face,
    .lpx-board .control-scene-stop-clip .button-face,
    .lpx-board .control-scene-mute .button-face,
    .lpx-board .control-scene-solo .button-face,
    .lpx-board .control-scene-record-arm .button-face {
      background: linear-gradient(#323a3f, #15191c);
    }

    .lpx-board .control.active:not(.visual-only) .pad-face,
    .lpx-board .control.active:not(.visual-only) .button-face {
      border-color: #0f7d3d;
    }

    .lpx-board .control.blocked .pad-face,
    .lpx-board .control.blocked .button-face {
      border-color: #ef3340;
      background: linear-gradient(#ff8278, #bd111b);
    }

    .lpx-board .label {
      max-width: 100%;
      min-height: 11px;
      font-size: 9px;
      line-height: 1.15;
      overflow-wrap: anywhere;
      text-align: center;
    }

    .lpx-board .rule {
      color: #aeb6b3;
      font-size: 8px;
      line-height: 1;
    }

    .lpx-board .type-pad .rule,
    .lpx-board .visual-only .rule {
      display: none;
    }

    .apc-board {
      max-width: 980px;
      margin: 0 auto;
      padding: 14px;
      border: 3px solid #090a0b;
      border-radius: 16px;
      background:
        linear-gradient(135deg, rgba(255, 255, 255, 0.1), transparent 32%),
        #17191b;
      color: #f4f4ee;
      box-shadow:
        inset 0 0 0 2px rgba(255, 255, 255, 0.06),
        inset 0 0 42px rgba(0, 0, 0, 0.42),
        0 18px 34px rgba(58, 29, 13, 0.28);
      overflow-x: auto;
    }

    .apc-panel {
      display: grid;
      grid-template-columns: minmax(0, 2.45fr) minmax(268px, 1fr);
      gap: 14px;
      min-width: 890px;
    }

    .apc-left,
    .apc-right {
      display: grid;
      align-content: start;
      gap: 8px;
      min-width: 0;
    }

    .apc-brand {
      min-height: 36px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: #e8eceb;
      font-weight: 850;
      text-transform: uppercase;
    }

    .apc-brand .apc-logo {
      font-size: 29px;
      letter-spacing: 0;
    }

    .apc-brand .apc-mkii {
      margin-left: 4px;
      font-size: 12px;
      writing-mode: vertical-rl;
      vertical-align: middle;
    }

    .apc-row,
    .apc-grid-row {
      position: relative;
      display: grid;
      gap: 7px;
      padding-top: 13px;
    }

    .apc-row::before,
    .apc-grid-row::before,
    .apc-module::before {
      content: attr(data-label);
      position: absolute;
      left: 2px;
      top: 0;
      color: #a9b1af;
      font-size: 10px;
      line-height: 1;
      font-weight: 760;
      text-transform: uppercase;
    }

    .apc-eight {
      grid-template-columns: repeat(8, minmax(0, 1fr));
    }

    .apc-nine {
      grid-template-columns: repeat(9, minmax(0, 1fr));
    }

    .apc-clip-row {
      grid-template-columns: repeat(8, minmax(0, 1fr)) 58px;
      padding-top: 0;
    }

    .apc-mixer {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 58px;
      gap: 7px;
      align-items: start;
    }

    .apc-track-buttons {
      display: grid;
      gap: 5px;
    }

    .apc-right {
      padding-left: 13px;
      border-left: 1px solid rgba(244, 244, 238, 0.2);
    }

    .apc-module {
      position: relative;
      display: grid;
      gap: 7px;
      padding-top: 14px;
    }

    .apc-transport {
      grid-template-columns: repeat(3, 1fr);
    }

    .apc-modes {
      grid-template-columns: repeat(3, 1fr);
    }

    .apc-device-knobs {
      grid-template-columns: repeat(4, 1fr);
    }

    .apc-device-buttons {
      grid-template-columns: repeat(3, 1fr);
    }

    .apc-bank {
      width: 150px;
      grid-template-columns: repeat(3, 1fr);
      justify-self: start;
    }

    .apc-bank .control-bank-up {
      grid-column: 2;
    }

    .apc-bank .control-bank-left {
      grid-column: 1;
    }

    .apc-bank .control-bank-right {
      grid-column: 3;
    }

    .apc-bank .control-bank-down {
      grid-column: 2;
    }

    .apc-bottom-right {
      display: grid;
      grid-template-columns: 1fr;
      gap: 9px;
      align-items: start;
    }

    .apc-board .control {
      min-width: 0;
      min-height: 44px;
      padding: 5px 4px;
      gap: 3px;
      border-color: #08090a;
      border-radius: 4px;
      background: #24282a;
      color: #f2f3ee;
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.07),
        0 1px 0 rgba(255, 255, 255, 0.06);
    }

    .apc-board .control.active:not(.visual-only) {
      background: rgba(24, 169, 87, 0.18);
      border-color: var(--passing);
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.07),
        0 0 0 2px rgba(24, 169, 87, 0.5),
        0 0 14px rgba(24, 169, 87, 0.3);
    }

    .apc-board .control.visual-only {
      opacity: 0.76;
    }

    .apc-board .control.blocked {
      background: #4b1e23;
      border-color: #ef3340;
      color: #fff0eb;
    }

    .apc-board .control.moving {
      border-color: #f4f4ee;
      box-shadow:
        0 0 0 3px rgba(255, 255, 255, 0.82),
        0 0 22px rgba(0, 190, 255, 0.72);
    }

    .apc-board .type-pad {
      min-height: 58px;
      aspect-ratio: 1.12;
    }

    .apc-board .pad-face {
      width: 36px;
      height: 23px;
      border-color: #0e1011;
      border-radius: 4px;
      background: linear-gradient(#95f5d4, #35a88b);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.28);
    }

    .apc-board .control[class*="clip-2"] .pad-face,
    .apc-board .control[class*="clip-4"] .pad-face {
      background: linear-gradient(#ffa9a2, #cf4146);
    }

    .apc-board .control[class*="clip-3"] .pad-face,
    .apc-board .control[class*="clip-5"] .pad-face {
      background: linear-gradient(#ffe491, #e0a52c);
    }

    .apc-board .button-face {
      width: 35px;
      height: 16px;
      border-color: #0e1011;
      border-radius: 3px;
      background: linear-gradient(#e5e9e5, #929b9d);
    }

    .apc-board .control-scene-1 .button-face,
    .apc-board .control-scene-2 .button-face,
    .apc-board .control-scene-3 .button-face,
    .apc-board .control-scene-4 .button-face,
    .apc-board .control-scene-5 .button-face,
    .apc-board .control-stop-all-clips .button-face,
    .apc-board .control-record .button-face,
    .apc-board .control-session .button-face,
    .apc-board .control-record-arm-1 .button-face,
    .apc-board .control-record-arm-2 .button-face,
    .apc-board .control-record-arm-3 .button-face,
    .apc-board .control-record-arm-4 .button-face,
    .apc-board .control-record-arm-5 .button-face,
    .apc-board .control-record-arm-6 .button-face,
    .apc-board .control-record-arm-7 .button-face,
    .apc-board .control-record-arm-8 .button-face {
      background: linear-gradient(#ff7f6d, #cf333b);
    }

    .apc-board .knob-face {
      width: 36px;
      height: 36px;
      border: 4px solid #070809;
      background:
        linear-gradient(#e9ece8, #e9ece8) 50% 6px/3px 11px no-repeat,
        radial-gradient(circle at 50% 42%, #44494b 0 45%, #111315 46% 100%);
      box-shadow:
        0 0 0 2px #afb7b5,
        0 4px 7px rgba(0, 0, 0, 0.42);
    }

    .apc-board .type-fader {
      min-height: 148px;
      background: transparent;
      border-color: transparent;
      box-shadow: none;
    }

    .apc-board .fader-face {
      width: 30px;
      height: 112px;
      border: 0;
      border-radius: 2px;
      background:
        linear-gradient(#08090a, #08090a) center/6px 100% no-repeat,
        repeating-linear-gradient(to bottom, transparent 0 16px, rgba(244, 244, 238, 0.55) 16px 18px, transparent 18px 27px);
    }

    .apc-board .fader-face::after {
      left: -5px;
      right: -5px;
      top: 44px;
      height: 18px;
      border-radius: 3px;
      background: linear-gradient(#f0f1ec, #7f888a);
      border: 1px solid #0b0d0e;
    }

    .apc-board .type-slider {
      min-height: 70px;
      background: transparent;
      border-color: transparent;
      box-shadow: none;
    }

    .apc-board .type-slider .fader-face {
      width: 128px;
      height: 34px;
      background:
        linear-gradient(#08090a, #08090a) center/100% 6px no-repeat,
        repeating-linear-gradient(to right, transparent 0 17px, rgba(244, 244, 238, 0.55) 17px 19px, transparent 19px 28px);
    }

    .apc-board .type-slider .fader-face::after {
      top: -5px;
      bottom: -5px;
      left: 54px;
      right: auto;
      width: 18px;
      height: auto;
    }

    .apc-board .control.active:not(.visual-only) .knob-face {
      box-shadow:
        0 0 0 2px #afb7b5,
        0 0 0 5px var(--passing),
        0 0 15px rgba(24, 169, 87, 0.58);
    }

    .apc-board .control.active:not(.visual-only) .fader-face::after,
    .apc-board .control.active:not(.visual-only) .button-face,
    .apc-board .control.active:not(.visual-only) .pad-face {
      border-color: #0f7d3d;
      background: linear-gradient(#7df0a8, #18a957);
    }

    .apc-board .label {
      max-width: 100%;
      font-size: 10px;
      line-height: 1.05;
      overflow-wrap: anywhere;
      text-align: center;
    }

    .apc-board .rule {
      color: #b8c0be;
      font-size: 8px;
      line-height: 1;
    }

    .apc-board .visual-only .rule {
      display: none;
    }

    .lcxl-board .control.active:not(.visual-only),
    .lcxl-board .control.active:not(.visual-only).type-knob,
    .lcxl-board .control.active:not(.visual-only).type-fader,
    .lcxl-board .control.active:not(.visual-only).type-button,
    .lpx-board .control.active:not(.visual-only),
    .lpx-board .control.active:not(.visual-only).type-pad,
    .lpx-board .control.active:not(.visual-only).type-button,
    .apc-board .control.active:not(.visual-only),
    .apc-board .control.active:not(.visual-only).type-knob,
    .apc-board .control.active:not(.visual-only).type-fader,
    .apc-board .control.active:not(.visual-only).type-slider,
    .apc-board .control.active:not(.visual-only).type-pad,
    .apc-board .control.active:not(.visual-only).type-button {
      border-color: var(--passing);
      background: rgba(217, 246, 194, 0.9);
      color: var(--ink);
    }

    .lcxl-board .control.blocked,
    .lcxl-board .control.blocked.type-knob,
    .lcxl-board .control.blocked.type-fader,
    .lcxl-board .control.blocked.type-button,
    .lpx-board .control.blocked,
    .lpx-board .control.blocked.type-pad,
    .lpx-board .control.blocked.type-button,
    .apc-board .control.blocked,
    .apc-board .control.blocked.type-knob,
    .apc-board .control.blocked.type-fader,
    .apc-board .control.blocked.type-slider,
    .apc-board .control.blocked.type-pad,
    .apc-board .control.blocked.type-button {
      border-color: var(--danger);
      background: var(--danger-weak);
      color: #5f1010;
    }

    .lcxl-board .control.moving,
    .lcxl-board .control.moving.type-knob,
    .lcxl-board .control.moving.type-fader,
    .lcxl-board .control.moving.type-button,
    .lpx-board .control.moving,
    .lpx-board .control.moving.type-pad,
    .lpx-board .control.moving.type-button,
    .apc-board .control.moving,
    .apc-board .control.moving.type-knob,
    .apc-board .control.moving.type-fader,
    .apc-board .control.moving.type-slider,
    .apc-board .control.moving.type-pad,
    .apc-board .control.moving.type-button {
      border-color: var(--warning-strong);
      background: var(--warning);
      color: var(--ink);
      box-shadow:
        0 0 0 4px rgba(255, 255, 255, 0.92),
        0 0 24px rgba(215, 154, 0, 0.8);
    }

    .lcxl-board .control.visual-only,
    .lpx-board .control.visual-only,
    .apc-board .control.visual-only {
      background: #34383a;
      color: #edf0ea;
    }

    .empty {
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    @media (max-width: 820px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }

      .status {
        text-align: left;
      }

      .status-panel {
        width: 100%;
        min-width: 0;
        justify-items: start;
      }

      .midi-popover {
        left: 0;
        right: auto;
        width: min(360px, calc(100vw - 52px));
      }

      .toolbar {
        grid-template-columns: 1fr 1fr;
      }

      .preset-bar {
        grid-template-columns: 1fr 1fr;
      }

      .operation-board {
        grid-template-columns: 1fr;
        gap: 10px;
        border-width: 14px;
        padding: 14px;
      }

      .patient-overlay {
        width: 96px;
        opacity: 0.74;
        transform: rotate(-6deg);
      }

      .brand-rail {
        min-height: 76px;
        grid-template-columns: auto auto 1fr;
        grid-template-rows: 1fr;
        justify-items: start;
        gap: 12px;
        padding: 0;
      }

      .skill-game {
        width: auto;
        font-size: 20px;
        transform: none;
        text-align: left;
      }

      .vertical-logo {
        display: flex;
        align-items: baseline;
        gap: 8px;
        text-align: left;
      }

      .brand-mark {
        font-size: 12px;
      }

      .product-mark {
        writing-mode: horizontal-tb;
        transform: none;
        font-size: 42px;
        line-height: 1;
      }

      .playfield {
        padding-top: 78px;
      }

      .break-copy {
        font-size: 15px;
      }

      .break-sticker {
        top: 112px;
        right: 22px;
      }

      .controllers {
        grid-template-columns: 1fr;
      }

      .group {
        grid-template-columns: 1fr;
      }

      .controls {
        grid-template-columns: repeat(4, minmax(62px, 1fr));
      }
    }

    @media (max-width: 460px) {
      main {
        padding: 12px;
      }

      .operation-board {
        border-width: 10px;
        border-radius: 20px;
      }

      .patient-overlay {
        width: 76px;
        opacity: 0.68;
      }

      .vertical-logo {
        gap: 6px;
      }

      .brand-mark {
        font-size: 10px;
      }

      .product-mark {
        font-size: 32px;
      }

      .break-copy {
        font-size: 14px;
      }

      .toolbar {
        grid-template-columns: 1fr;
      }

      .preset-bar {
        grid-template-columns: 1fr;
      }

      .playfield {
        padding-top: 78px;
      }

      .break-sticker {
        top: 98px;
        right: 16px;
      }

      .break-copy {
        white-space: normal;
      }

      .controller-list {
        grid-template-columns: 1fr;
      }

      .controls {
        grid-template-columns: repeat(2, minmax(62px, 1fr));
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>INFINIGHT MidiSurgeon</h1>
      <div class="status-panel" id="statusPanel">
        <button class="midi-status-button" id="midiStatusButton" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="midiStatusPopover">
          <span class="midi-status-label" id="midiStatusLabel">Listening for MIDI</span>
          <span class="midi-status-chevron" aria-hidden="true"></span>
        </button>
        <div class="status" id="status"></div>
        <div class="midi-popover" id="midiStatusPopover" role="listbox" aria-label="Connected MIDI devices" hidden></div>
      </div>
    </header>
    <main>
      <section class="operation-board">
        <div class="break-sticker" aria-label="For when sh*t breaks">
          <span class="poop-icon" aria-hidden="true">
            <span class="poop-base"></span>
            <span class="poop-mid"></span>
            <span class="poop-top"></span>
            <span class="poop-shine"></span>
          </span>
          <span class="break-copy">For when sh*t breaks</span>
        </div>
        <aside class="brand-rail" aria-hidden="true">
          <div class="skill-game">SKILL<br>GAME</div>
          <svg class="patient-overlay" viewBox="0 0 980 720" aria-hidden="true" focusable="false">
            <ellipse class="patient-shadow" cx="515" cy="406" rx="392" ry="220"></ellipse>
            <path class="patient-suit" d="M317 246c58-78 154-112 256-92 90 18 157 78 190 164 38 98 9 210-74 283-74 65-185 79-282 37-94-41-155-124-159-221-3-68 20-125 69-171Z"></path>
            <path class="patient-skin" d="M389 160c9-61 61-106 124-102 72 4 125 62 121 132-3 68-60 119-129 116-72-3-127-65-116-146Z"></path>
            <path class="patient-line" d="M424 136c41-36 108-35 151 2M453 213c29 18 66 17 94-2"></path>
            <circle class="patient-red" cx="510" cy="178" r="18"></circle>
            <circle fill="#67291d" cx="470" cy="164" r="8"></circle>
            <circle fill="#67291d" cx="554" cy="164" r="8"></circle>
            <path class="patient-skin" d="M304 346c-63 10-111 44-144 101-13 23-4 51 20 62 23 11 48 3 61-19 19-32 45-49 83-57"></path>
            <path class="patient-skin" d="M742 329c60-1 107 22 145 70 17 21 48 24 68 7 20-18 22-47 4-68-54-66-121-100-202-96"></path>
            <path class="patient-skin" d="M404 611c-31 56-70 94-123 116-24 10-35 38-25 62 10 23 36 34 60 25 79-31 137-85 174-162"></path>
            <path class="patient-skin" d="M626 626c24 65 63 112 121 144 24 13 54 4 67-20 12-23 4-51-20-65-36-20-61-51-77-94"></path>
            <path class="patient-socket" d="M476 332c33-24 82-7 92 33 9 38-22 70-61 68-41-2-64-62-31-101Z"></path>
            <path class="patient-socket" d="M624 449c32-10 68 10 75 44 8 39-26 70-64 59-34-10-47-54-11-103Z"></path>
            <path class="patient-socket" d="M372 455c34-10 67 9 75 40 8 32-14 62-47 65-36 3-63-28-52-62 4-14 12-27 24-43Z"></path>
            <path class="patient-detail" d="M512 342l18 34-37-5 28 31"></path>
            <path class="patient-detail" d="M650 469l22 24-32 8 28 24"></path>
            <path class="patient-detail" d="M389 477l28 11-25 19 32 9"></path>
            <path class="patient-spark" d="M230 271l28 24M268 256l-9 35M746 175l-28 30M713 168l12 37M798 582l35 6M817 554l-11 32"></path>
            <path class="patient-line" d="M190 164l54-28 29 55 54-28M792 117c31 15 51 36 61 62 10 28 8 58-6 91"></path>
            <text class="patient-label" x="124" y="124" transform="rotate(-13 124 124)">BENT NOTE</text>
            <text class="patient-label" x="735" y="105" transform="rotate(13 735 105)">WOBBLY CC</text>
            <text class="patient-label" x="721" y="641" transform="rotate(-10 721 641)">BAD FADER</text>
          </svg>
          <div class="vertical-logo">
            <span class="brand-mark">INFINIGHT</span>
            <span class="product-mark">MidiSurgeon</span>
          </div>
        </aside>
        <section class="playfield">
          <div class="toolbar">
            <select id="templateSelect" aria-label="Controller template"></select>
            <select id="inputSelect" aria-label="MIDI input"></select>
            <button class="button primary" id="learnButton" type="button">Learn next control</button>
            <button class="button apply" id="applyButton" type="button" disabled>Apply</button>
            <button class="button" id="refreshButton" type="button">Refresh</button>
          </div>
          <div class="preset-bar">
            <select id="presetSelect" aria-label="Preset"></select>
            <button class="button" id="loadPresetButton" type="button" disabled>Load</button>
            <button class="button apply" id="savePresetButton" type="button" disabled>Save</button>
            <button class="button" id="saveAsPresetButton" type="button">Save As</button>
            <button class="button danger" id="deletePresetButton" type="button" disabled>Delete</button>
          </div>
          <section class="controllers" id="controllers"></section>
          <section class="board" id="board"></section>
        </section>
      </section>
    </main>
  </div>

  <script>
    const state = {
      templates: [],
      selectedTemplateId: null,
      selectedInput: null,
      rules: new Set(),
      appliedRules: new Set(),
      inputs: [],
      controllers: [],
      presets: [],
      selectedPresetId: null,
      activitySource: null,
      activityAbort: null,
      activityTimers: new Map(),
      lastActivityInput: null,
      lastActivityTimer: null,
    };

    const templateSelect = document.querySelector("#templateSelect");
    const inputSelect = document.querySelector("#inputSelect");
    const controllers = document.querySelector("#controllers");
    const board = document.querySelector("#board");
    const status = document.querySelector("#status");
    const statusPanel = document.querySelector("#statusPanel");
    const midiStatusButton = document.querySelector("#midiStatusButton");
    const midiStatusLabel = document.querySelector("#midiStatusLabel");
    const midiStatusPopover = document.querySelector("#midiStatusPopover");
    const learnButton = document.querySelector("#learnButton");
    const applyButton = document.querySelector("#applyButton");
    const refreshButton = document.querySelector("#refreshButton");
    const presetSelect = document.querySelector("#presetSelect");
    const loadPresetButton = document.querySelector("#loadPresetButton");
    const savePresetButton = document.querySelector("#savePresetButton");
    const saveAsPresetButton = document.querySelector("#saveAsPresetButton");
    const deletePresetButton = document.querySelector("#deletePresetButton");

    function setStatus(text) {
      status.textContent = text;
    }

    function midiDeviceItems() {
      const controllerItems = state.controllers
        .filter((controller) => Array.isArray(controller.inputs) && controller.inputs.length)
        .map((controller) => ({
          id: controller.id || controller.input || controller.name,
          name: controller.template_name || controller.name || controller.input,
          input: controller.input || controller.inputs[0],
          inputs: controller.inputs,
          outputs: controller.outputs || [],
          templateId: controller.template_id,
          templateName: controller.template_name,
          virtual: Boolean(controller.virtual),
        }));

      if (controllerItems.length) return controllerItems;

      return state.inputs.map((input) => ({
        id: input,
        name: input,
        input,
        inputs: [input],
        outputs: [],
        templateId: null,
        templateName: null,
        virtual: false,
      }));
    }

    function inputIsActive(item) {
      return Boolean(state.lastActivityInput && item.inputs.includes(state.lastActivityInput));
    }

    function renderMidiStatus() {
      const items = midiDeviceItems();
      if (!items.length) {
        midiStatusLabel.textContent = "No MIDI inputs detected";
      } else if (items.length === 1) {
        midiStatusLabel.textContent = `Listening for MIDI: ${items[0].name}`;
      } else if (state.lastActivityInput) {
        const activeItem = items.find((item) => item.inputs.includes(state.lastActivityInput));
        midiStatusLabel.textContent = activeItem
          ? `Listening: ${activeItem.name} +${items.length - 1}`
          : `Listening for MIDI: ${items.length} devices`;
      } else {
        midiStatusLabel.textContent = `Listening for MIDI: ${items.length} devices`;
      }

      const title = document.createElement("div");
      title.className = "midi-popover-title";
      title.textContent = items.length
        ? items.length === 1 ? "Listening to 1 device" : `Listening to ${items.length} devices`
        : "No MIDI inputs";

      midiStatusPopover.innerHTML = "";
      midiStatusPopover.append(title);

      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "midi-popover-empty";
        empty.textContent = "Connect a MIDI controller, then refresh.";
        midiStatusPopover.append(empty);
        return;
      }

      const list = document.createElement("div");
      list.className = "midi-device-list";
      for (const item of items) {
        const device = document.createElement("button");
        device.type = "button";
        device.className = [
          "midi-device",
          item.input && item.input === inputSelect.value ? "selected" : "",
          inputIsActive(item) ? "receiving" : "",
        ].filter(Boolean).join(" ");
        device.setAttribute("role", "option");
        device.setAttribute("aria-selected", item.input && item.input === inputSelect.value ? "true" : "false");

        const dot = document.createElement("span");
        dot.className = "midi-device-dot";
        dot.setAttribute("aria-hidden", "true");

        const copy = document.createElement("span");
        const name = document.createElement("span");
        name.className = "midi-device-name";
        name.textContent = item.name;
        const meta = document.createElement("span");
        meta.className = "midi-device-meta";
        const kind = item.templateName ? "recognized" : item.virtual ? "virtual" : "unknown";
        const ports = `${item.inputs.length} in / ${item.outputs.length} out`;
        const active = inputIsActive(item) ? " - receiving" : "";
        meta.textContent = item.input ? `${kind} - ${item.input} - ${ports}${active}` : `${kind} - ${ports}${active}`;
        copy.append(name, meta);

        device.append(dot, copy);
        device.addEventListener("click", () => {
          selectMidiDevice(item);
          closeMidiPopover();
        });
        list.append(device);
      }
      midiStatusPopover.append(list);
    }

    function selectMidiDevice(item) {
      if (item.templateId) {
        state.selectedTemplateId = item.templateId;
      }
      if (item.input) {
        state.selectedInput = item.input;
      }
      renderSelects();
      renderControllers();
      renderBoard();
      startActivityStream();
      renderPresets();
      setStatus(`${item.name} selected`);
    }

    function openMidiPopover() {
      midiStatusPopover.hidden = false;
      midiStatusButton.setAttribute("aria-expanded", "true");
    }

    function closeMidiPopover() {
      midiStatusPopover.hidden = true;
      midiStatusButton.setAttribute("aria-expanded", "false");
    }

    function selectedTemplate() {
      return state.templates.find((template) => template.id === state.selectedTemplateId) || state.templates[0];
    }

    function selectedPreset() {
      return state.presets.find((preset) => preset.id === state.selectedPresetId) || null;
    }

    function sortedRules(rules) {
      return [...rules].sort();
    }

    function sameRuleList(left, right) {
      const leftRules = sortedRules(left || []);
      const rightRules = sortedRules(right || []);
      return leftRules.length === rightRules.length && leftRules.every((rule, index) => rule === rightRules[index]);
    }

    function currentPresetPayload(name, id = null) {
      const template = selectedTemplate();
      return {
        id,
        name,
        template_id: template?.id || state.selectedTemplateId,
        template_name: template?.name || "",
        input_name: inputSelect.value || state.selectedInput || "",
        blocked_rules: sortedRules(state.rules),
      };
    }

    function presetHasUnsavedChanges() {
      const preset = selectedPreset();
      if (!preset) return false;
      const templateId = selectedTemplate()?.id || state.selectedTemplateId || null;
      const inputName = inputSelect.value || state.selectedInput || "";
      return preset.template_id !== templateId
        || (preset.input_name || "") !== inputName
        || !sameRuleList(preset.blocked_rules, state.rules);
    }

    function renderPresets() {
      presetSelect.innerHTML = "";
      const hasPresets = state.presets.length > 0;
      presetSelect.disabled = !hasPresets;

      if (!hasPresets) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No saved presets";
        presetSelect.append(option);
        state.selectedPresetId = null;
      } else {
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Preset: none selected";
        presetSelect.append(placeholder);

        for (const preset of state.presets) {
          const option = document.createElement("option");
          option.value = preset.id;
          const dirty = preset.id === state.selectedPresetId && presetHasUnsavedChanges() ? " *" : "";
          option.textContent = `${preset.name}${dirty}`;
          presetSelect.append(option);
        }

        if (state.selectedPresetId && state.presets.some((preset) => preset.id === state.selectedPresetId)) {
          presetSelect.value = state.selectedPresetId;
        } else {
          state.selectedPresetId = null;
          presetSelect.value = "";
        }
      }

      const preset = selectedPreset();
      const dirty = presetHasUnsavedChanges();
      loadPresetButton.disabled = !preset;
      savePresetButton.disabled = !preset;
      savePresetButton.textContent = dirty ? "Save *" : "Save";
      deletePresetButton.disabled = !preset;
    }

    function groupControls(controls) {
      return controls.reduce((groups, control) => {
        const row = control.row || "Controls";
        if (!groups.has(row)) groups.set(row, []);
        groups.get(row).push(control);
        return groups;
      }, new Map());
    }

    function renderSelects() {
      templateSelect.innerHTML = "";
      for (const template of state.templates) {
        const option = document.createElement("option");
        option.value = template.id;
        option.textContent = template.name;
        templateSelect.append(option);
      }
      const templateIds = state.templates.map((template) => template.id);
      if (state.selectedTemplateId && templateIds.includes(state.selectedTemplateId)) {
        templateSelect.value = state.selectedTemplateId;
      } else if (state.templates.length) {
        state.selectedTemplateId = state.templates[0].id;
        templateSelect.value = state.selectedTemplateId;
      }

      inputSelect.innerHTML = "";
      const template = selectedTemplate();
      const preferredInput = template ? template.input_name : "";
      const inputNames = state.inputs.length ? [...state.inputs] : [preferredInput].filter(Boolean);
      if (state.selectedInput && !inputNames.includes(state.selectedInput)) {
        inputNames.push(state.selectedInput);
      }
      for (const input of inputNames) {
        const option = document.createElement("option");
        option.value = input;
        option.textContent = input;
        inputSelect.append(option);
      }
      const selectedInput = state.selectedInput || preferredInput;
      if (selectedInput && inputNames.includes(selectedInput)) {
        inputSelect.value = selectedInput;
      } else if (preferredInput && inputNames.includes(preferredInput)) {
        inputSelect.value = preferredInput;
        state.selectedInput = preferredInput;
      }
      renderMidiStatus();
    }

    function renderControllers() {
      controllers.innerHTML = "";

      const title = document.createElement("div");
      title.className = "connected-title";
      title.textContent = "Connected MIDI";

      const list = document.createElement("div");
      list.className = "controller-list";

      if (!state.controllers.length) {
        const empty = document.createElement("div");
        empty.className = "controller-card virtual";
        empty.innerHTML = `<div class="controller-name">No MIDI inputs detected</div>`;
        list.append(empty);
      }

      for (const controller of state.controllers) {
        const card = document.createElement("button");
        card.type = "button";
        const selected = controller.input && controller.input === inputSelect.value;
        card.className = [
          "controller-card",
          controller.template_id ? "recognized" : "",
          controller.virtual ? "virtual" : "",
          selected ? "selected" : "",
        ].filter(Boolean).join(" ");
        card.dataset.input = controller.input || "";
        card.dataset.template = controller.template_id || "";

        const name = document.createElement("div");
        name.className = "controller-name";
        name.textContent = controller.name;

        const meta = document.createElement("div");
        meta.className = "controller-meta";
        const kind = controller.template_name ? "recognized" : controller.virtual ? "virtual" : "unknown";
        const ports = `${controller.inputs.length} in / ${controller.outputs.length} out`;
        meta.textContent = controller.input ? `${kind} - ${controller.input} - ${ports}` : `${kind} - ${ports}`;

        card.append(name, meta);
        card.addEventListener("click", () => selectController(controller));
        list.append(card);
      }

      controllers.append(title, list);
      renderMidiStatus();
    }

    function selectController(controller) {
      if (controller.template_id) {
        state.selectedTemplateId = controller.template_id;
      }
      if (controller.input) {
        state.selectedInput = controller.input;
      }
      renderSelects();
      renderControllers();
      renderBoard();
      startActivityStream();
      renderPresets();
      setStatus(`${controller.name} selected`);
    }

    function rulesForControl(control) {
      const rules = [];
      if (control.rule) rules.push(control.rule);
      if (Array.isArray(control.rules)) rules.push(...control.rules);
      return rules;
    }

    function primaryRule(control) {
      return rulesForControl(control)[0] || "";
    }

    function changedRules() {
      const changed = new Set();
      for (const rule of state.rules) {
        if (!state.appliedRules.has(rule)) changed.add(rule);
      }
      for (const rule of state.appliedRules) {
        if (!state.rules.has(rule)) changed.add(rule);
      }
      return changed;
    }

    function updateApplyState() {
      const count = changedRules().size;
      applyButton.disabled = count === 0;
      applyButton.textContent = count ? `Apply (${count})` : "Apply";
      renderPresets();
    }

    function controlFace(control) {
      const face = document.createElement("div");
      if (control.type === "fader" || control.type === "slider") {
        face.className = "fader-face";
      } else if (control.type === "pad") {
        face.className = "pad-face";
      } else if (control.type === "key") {
        face.className = `key-face ${control.color === "black" ? "black" : ""}`;
      } else if (control.type === "wheel") {
        face.className = "wheel-face";
      } else if (control.type === "strip") {
        face.className = "strip-face";
      } else if (control.type === "screen") {
        face.className = "screen-face";
      } else if (control.type === "button") {
        face.className = "button-face";
      } else {
        face.className = "knob-face";
      }
      return face;
    }

    function createControlButton(control) {
      const rules = rulesForControl(control);
      const ruleText = primaryRule(control);
      const hasRule = rules.length > 0;
      const isBlocked = rules.some((item) => state.rules.has(item));
      const isPending = rules.some((item) => state.rules.has(item) !== state.appliedRules.has(item));
      const button = document.createElement("button");
      button.type = "button";
      button.className = [
        "control",
        `type-${control.type || "knob"}`,
        `control-${control.id || "unknown"}`,
        isBlocked ? "blocked" : "active",
        isPending ? "pending-change" : "",
        hasRule ? "" : "visual-only",
      ].filter(Boolean).join(" ");
      button.setAttribute("aria-pressed", String(isBlocked));
      button.disabled = !hasRule;
      const stateLabel = isBlocked ? "Bypassed" : "Passing";
      const pendingLabel = isPending ? " - pending apply" : "";
      button.title = hasRule ? `${control.label} - ${stateLabel}${pendingLabel} - ${ruleText}` : `${control.label} - ${control.kind || "visual layout"}`;
      button.dataset.rule = ruleText;
      button.dataset.rules = rules.join(" ");
      button.append(controlFace(control));

      const label = document.createElement("div");
      label.className = "label";
      label.textContent = control.label;
      button.append(label);

      const ruleEl = document.createElement("div");
      ruleEl.className = "rule";
      ruleEl.textContent = hasRule ? ruleText : control.kind || control.type;
      button.append(ruleEl);

      if (hasRule) {
        button.addEventListener("click", () => toggleControlRules(rules, !isBlocked));
      }
      return button;
    }

    function controlsForRow(template, row) {
      return (template.controls || []).filter((control) => control.row === row);
    }

    function appendControlRow(parent, template, row, className = "") {
      const controls = controlsForRow(template, row);
      if (!controls.length) return;
      const element = document.createElement("div");
      element.className = `lcxl-row ${className}`.trim();
      element.dataset.label = row;
      for (const control of controls) {
        element.append(createControlButton(control));
      }
      parent.append(element);
    }

    function appendSideGroup(parent, template, row, className) {
      const controls = controlsForRow(template, row);
      if (!controls.length) return;
      const element = document.createElement("div");
      element.className = className;
      element.dataset.label = row;
      for (const control of controls) {
        element.append(createControlButton(control));
      }
      parent.append(element);
    }

    function renderLaunchControlXLBoard(template) {
      board.className = "board lcxl-board";

      const panel = document.createElement("div");
      panel.className = "lcxl-panel";

      const brand = document.createElement("div");
      brand.className = "lcxl-brand";
      brand.innerHTML = `<span class="novation">novation</span><span><span class="launch">LAUNCH</span> CONTROL<span class="model">XL</span></span>`;

      const main = document.createElement("div");
      main.className = "lcxl-main";
      appendControlRow(main, template, "Send A", "lcxl-knob-row");
      appendControlRow(main, template, "Send B", "lcxl-knob-row");
      appendControlRow(main, template, "Pan / Device", "lcxl-knob-row");
      appendControlRow(main, template, "Faders", "lcxl-fader-row");
      appendControlRow(main, template, "Track Focus", "lcxl-channel-row");
      appendControlRow(main, template, "Track Control", "lcxl-channel-row");

      const side = document.createElement("div");
      side.className = "lcxl-side";
      appendSideGroup(side, template, "Templates", "lcxl-side-pair");
      appendSideGroup(side, template, "Send Select", "lcxl-side-pair");
      appendSideGroup(side, template, "Track Select", "lcxl-side-pair");
      appendSideGroup(side, template, "Mode Buttons", "lcxl-side-stack");

      panel.append(brand, main, side);
      board.append(panel);
    }

    function appendApcControls(parent, controls, label, className) {
      if (!controls.length) return;
      const element = document.createElement("div");
      element.className = className;
      element.dataset.label = label;
      for (const control of controls) {
        element.append(createControlButton(control));
      }
      parent.append(element);
    }

    function appendApcRow(parent, template, row, className) {
      appendApcControls(parent, controlsForRow(template, row), row, className);
    }

    function controlsByIds(template, ids) {
      const controlsById = new Map((template.controls || []).map((control) => [control.id, control]));
      return ids.map((id) => controlsById.get(id)).filter(Boolean);
    }

    function appendLaunchpadControls(parent, controls, label, className) {
      if (!controls.length) return;
      const element = document.createElement("div");
      element.className = className;
      if (label) element.dataset.label = label;
      for (const control of controls) {
        element.append(createControlButton(control));
      }
      parent.append(element);
    }

    function renderLaunchpadXBoard(template) {
      board.className = "board lpx-board";

      const panel = document.createElement("div");
      panel.className = "lpx-panel";

      const brand = document.createElement("div");
      brand.className = "lpx-brand";
      brand.innerHTML = `<span><span class="novation">novation</span> Launchpad X</span><span class="model">64 RGB pads</span>`;

      const topShell = document.createElement("div");
      topShell.className = "lpx-top-shell";
      appendLaunchpadControls(topShell, controlsByIds(template, [
        "nav-up", "nav-down", "nav-left", "nav-right", "mode-session", "mode-note", "mode-custom", "capture-midi",
      ]), "Top Controls", "lpx-top-row");

      const logoTile = document.createElement("div");
      logoTile.className = "lpx-logo-tile";
      logoTile.innerHTML = `<span class="lpx-logo-mark" aria-hidden="true"></span>`;
      topShell.append(logoTile);

      const main = document.createElement("div");
      main.className = "lpx-main";

      const padGrid = document.createElement("div");
      padGrid.className = "lpx-pad-grid";
      for (const row of ["Grid 8", "Grid 7", "Grid 6", "Grid 5", "Grid 4", "Grid 3", "Grid 2", "Grid 1"]) {
        appendLaunchpadControls(padGrid, controlsForRow(template, row), row, "lpx-grid-row");
      }

      appendLaunchpadControls(main, controlsByIds(template, [
        "scene-volume", "scene-pan", "scene-send-a", "scene-send-b", "scene-stop-clip", "scene-mute", "scene-solo", "scene-record-arm",
      ]), "Scene Launch", "lpx-side");

      main.prepend(padGrid);
      panel.append(brand, topShell, main);
      board.append(panel);
    }

    function renderApc40Board(template) {
      board.className = "board apc-board";

      const panel = document.createElement("div");
      panel.className = "apc-panel";

      const left = document.createElement("div");
      left.className = "apc-left";
      appendApcRow(left, template, "Channel Controls", "apc-row apc-eight");

      for (const row of ["Clip Grid 5", "Clip Grid 4", "Clip Grid 3", "Clip Grid 2", "Clip Grid 1"]) {
        appendApcRow(left, template, row, "apc-grid-row apc-clip-row");
      }

      appendApcRow(left, template, "Clip Stop", "apc-row apc-nine");
      appendApcRow(left, template, "Track Select", "apc-row apc-nine");

      const trackButtons = document.createElement("div");
      trackButtons.className = "apc-track-buttons";
      appendApcRow(trackButtons, template, "Track Activator", "apc-row apc-eight");
      appendApcRow(trackButtons, template, "Crossfade Assign", "apc-row apc-eight");
      appendApcRow(trackButtons, template, "Solo", "apc-row apc-eight");
      appendApcRow(trackButtons, template, "Record Arm", "apc-row apc-eight");
      left.append(trackButtons);

      const channelFaders = controlsByIds(template, [
        "fader-1", "fader-2", "fader-3", "fader-4", "fader-5", "fader-6", "fader-7", "fader-8", "master-fader",
      ]);
      appendApcControls(left, channelFaders, "Track Volume", "apc-row apc-nine");

      const right = document.createElement("div");
      right.className = "apc-right";
      const brand = document.createElement("div");
      brand.className = "apc-brand";
      brand.innerHTML = `<span class="apc-logo">APC40<span class="apc-mkii">mkII</span></span><span>AKAI</span>`;
      right.append(brand);
      appendApcRow(right, template, "Transport", "apc-module apc-transport");
      appendApcRow(right, template, "Assignable Modes", "apc-module apc-modes");
      appendApcRow(right, template, "Device Controls", "apc-module apc-device-knobs");
      appendApcRow(right, template, "Device Buttons", "apc-module apc-device-buttons");

      const bottom = document.createElement("div");
      bottom.className = "apc-bottom-right";
      appendApcRow(bottom, template, "Bank Select", "apc-module apc-bank");
      const utility = document.createElement("div");
      utility.className = "apc-left";
      appendApcRow(utility, template, "Cue", "apc-module");
      appendApcControls(utility, controlsByIds(template, ["crossfader"]), "Crossfader", "apc-module");
      bottom.append(utility);
      right.append(bottom);

      panel.append(left, right);
      board.append(panel);
    }

    function renderBoard() {
      const template = selectedTemplate();
      board.className = "board";
      board.innerHTML = "";
      if (!template) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No controller templates found.";
        board.append(empty);
        return;
      }

      if (template.layout === "novation_launch_control_xl") {
        renderLaunchControlXLBoard(template);
        return;
      }

      if (template.layout === "akai_apc40_mkii") {
        renderApc40Board(template);
        return;
      }

      if (template.layout === "novation_launchpad_x") {
        renderLaunchpadXBoard(template);
        return;
      }

      for (const [row, controls] of groupControls(template.controls || [])) {
        const group = document.createElement("section");
        group.className = "group";
        const title = document.createElement("div");
        title.className = "group-title";
        title.textContent = row;

        const grid = document.createElement("div");
        grid.className = "controls";
        for (const control of controls) {
          grid.append(createControlButton(control));
        }

        group.append(title, grid);
        board.append(group);
      }
    }

    function stopActivityStream() {
      if (state.activitySource) {
        state.activitySource.close();
        state.activitySource = null;
      }
      if (state.activityAbort) {
        state.activityAbort.abort();
        state.activityAbort = null;
      }
    }

    function controlNameForRule(rule) {
      const template = selectedTemplate();
      const control = template?.controls?.find((item) => rulesForControl(item).includes(rule));
      return control ? `${control.label} ${rule}` : rule;
    }

    function activityLabel(inputName) {
      return inputName ? ` on ${inputName}` : "";
    }

    function markMoved(rule, inputName) {
      if (inputName) {
        state.lastActivityInput = inputName;
        if (state.lastActivityTimer) clearTimeout(state.lastActivityTimer);
        state.lastActivityTimer = setTimeout(() => {
          state.lastActivityInput = null;
          state.lastActivityTimer = null;
          renderMidiStatus();
        }, 2200);
        renderMidiStatus();
      }
      if (state.rules.has(rule)) return;
      const matchedControls = [...document.querySelectorAll(`.control[data-rules~="${CSS.escape(rule)}"]`)];
      if (!matchedControls.length) {
        setStatus(`MIDI activity${activityLabel(inputName)}: ${rule}`);
        return;
      }
      for (const control of matchedControls) {
        control.classList.add("moving");
      }
      setStatus(`MIDI activity${activityLabel(inputName)}: ${controlNameForRule(rule)}`);
      if (state.activityTimers.has(rule)) {
        clearTimeout(state.activityTimers.get(rule));
      }
      state.activityTimers.set(rule, setTimeout(() => {
        for (const control of matchedControls) {
          control.classList.remove("moving");
        }
        state.activityTimers.delete(rule);
      }, 320));
    }

    function startActivityStream() {
      stopActivityStream();
      const url = "/api/activity?input=__all__";
      if (!("EventSource" in window)) {
        startFetchActivityStream(url);
        return;
      }
      const source = new EventSource(url);
      state.activitySource = source;
      source.addEventListener("message", (event) => {
        const data = JSON.parse(event.data);
        if (data.rule) markMoved(data.rule, data.input);
      });
      source.addEventListener("ready", (event) => {
        const data = JSON.parse(event.data);
        if (Array.isArray(data.inputs)) {
          state.inputs = data.inputs;
          renderMidiStatus();
        }
      });
      source.addEventListener("heartbeat", (event) => {
        const data = JSON.parse(event.data);
        if (Array.isArray(data.inputs)) {
          state.inputs = data.inputs;
          renderMidiStatus();
        }
      });
      source.addEventListener("error", () => {
        if (state.activitySource === source) {
          setStatus("MIDI activity monitor reconnecting");
        }
      });
    }

    async function startFetchActivityStream(url) {
      const abort = new AbortController();
      state.activityAbort = abort;
      try {
        const response = await fetch(url, { signal: abort.signal });
        if (!response.ok || !response.body) {
          throw new Error("MIDI activity monitor unavailable");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!abort.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const events = buffer.split("\\n\\n");
          buffer = events.pop() || "";
          for (const eventText of events) {
            const dataLine = eventText.split("\\n").find((line) => line.startsWith("data: "));
            if (!dataLine) continue;
            const data = JSON.parse(dataLine.slice(6));
            if (data.rule) markMoved(data.rule, data.input);
            if (data.status && Array.isArray(data.inputs) && !state.activityTimers.size) {
              state.inputs = data.inputs;
              renderMidiStatus();
            }
            if (data.error) setStatus(data.error);
          }
        }
      } catch (error) {
        if (!abort.signal.aborted) {
          setStatus(error.message);
        }
      }
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || response.statusText);
      }
      return data;
    }

    async function loadState() {
      setStatus("Loading");
      const data = await fetchJson("/api/state");
      state.templates = data.templates;
      state.controllers = data.controllers || [];
      state.presets = data.presets || [];
      const detected = state.controllers.find((controller) => controller.template_id);
      state.selectedTemplateId = state.selectedTemplateId || detected?.template_id || data.templates[0]?.id || null;
      state.selectedInput = state.selectedInput || detected?.input || null;
      state.rules = new Set(data.blocked_rules);
      state.appliedRules = new Set(data.blocked_rules);
      state.inputs = Array.isArray(data.inputs) ? data.inputs : [];
      renderSelects();
      renderControllers();
      renderBoard();
      renderMidiStatus();
      startActivityStream();
      updateApplyState();
      renderPresets();
      setStatus(`${state.rules.size} blocked controls`);
    }

    function toggleControlRules(rules, blocked) {
      for (const rule of rules) {
        if (blocked) {
          state.rules.add(rule);
        } else {
          state.rules.delete(rule);
        }
      }
      renderBoard();
      updateApplyState();
      const label = rules.length === 1 ? rules[0] : `${rules.length} rules`;
      setStatus(`${label} ${blocked ? "marked for bypass" : "marked passing"}`);
    }

    async function applyRules() {
      applyButton.disabled = true;
      setStatus("Applying bypasses");
      const data = await fetchJson("/api/blocks/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ blocked_rules: [...state.rules] }),
      });
      state.rules = new Set(data.blocked_rules);
      state.appliedRules = new Set(data.blocked_rules);
      renderBoard();
      updateApplyState();
      setStatus(`${state.rules.size} bypassed controls applied`);
    }

    async function savePreset(name, id = null) {
      const data = await fetchJson("/api/presets/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentPresetPayload(name, id)),
      });
      state.presets = data.presets || [];
      state.selectedPresetId = data.preset.id;
      renderPresets();
      setStatus(`${data.preset.name} preset saved`);
    }

    async function saveSelectedPreset() {
      const preset = selectedPreset();
      if (!preset) {
        await savePresetAs();
        return;
      }
      await savePreset(preset.name, preset.id);
    }

    async function savePresetAs() {
      const template = selectedTemplate();
      const defaultName = selectedPreset()?.name || `${template?.name || "MIDI controller"} repair`;
      const name = window.prompt("Preset name", defaultName);
      if (!name || !name.trim()) return;
      await savePreset(name.trim());
    }

    async function loadSelectedPreset() {
      const preset = selectedPreset();
      if (!preset) return;
      if (presetHasUnsavedChanges() && !window.confirm("Load this preset and replace the current bypasses?")) {
        return;
      }
      const data = await fetchJson("/api/presets/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: preset.id }),
      });
      state.presets = data.presets || [];
      state.selectedPresetId = data.preset.id;
      if (state.templates.some((template) => template.id === data.preset.template_id)) {
        state.selectedTemplateId = data.preset.template_id;
      }
      state.selectedInput = data.preset.input_name || state.selectedInput;
      state.rules = new Set(data.blocked_rules);
      state.appliedRules = new Set(data.blocked_rules);
      renderSelects();
      renderControllers();
      renderBoard();
      startActivityStream();
      updateApplyState();
      setStatus(`${data.preset.name} preset loaded`);
    }

    async function deleteSelectedPreset() {
      const preset = selectedPreset();
      if (!preset) return;
      if (!window.confirm(`Delete "${preset.name}" preset?`)) return;
      const data = await fetchJson("/api/presets/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: preset.id }),
      });
      state.presets = data.presets || [];
      state.selectedPresetId = null;
      renderPresets();
      setStatus(`${preset.name} preset deleted`);
    }

    async function learnNextControl() {
      learnButton.disabled = true;
      setStatus("Listening for MIDI");
      try {
        const data = await fetchJson("/api/learn", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: inputSelect.value, block: false, timeout: 10 }),
        });
        state.rules.add(data.rule);
        renderBoard();
        updateApplyState();
        const label = data.match?.control?.label || data.rule;
        setStatus(`${label} marked for bypass`);
      } finally {
        learnButton.disabled = false;
      }
    }

    templateSelect.addEventListener("change", () => {
      state.selectedTemplateId = templateSelect.value;
      state.selectedInput = selectedTemplate()?.input_name || state.selectedInput;
      renderSelects();
      renderControllers();
      renderBoard();
      renderPresets();
      startActivityStream();
    });
    inputSelect.addEventListener("change", () => {
      state.selectedInput = inputSelect.value;
      renderControllers();
      renderPresets();
      startActivityStream();
    });
    presetSelect.addEventListener("change", () => {
      state.selectedPresetId = presetSelect.value || null;
      renderPresets();
    });
    refreshButton.addEventListener("click", () => loadState().catch((error) => setStatus(error.message)));
    learnButton.addEventListener("click", () => learnNextControl().catch((error) => setStatus(error.message)));
    applyButton.addEventListener("click", () => applyRules().catch((error) => {
      setStatus(error.message);
      updateApplyState();
    }));
    loadPresetButton.addEventListener("click", () => loadSelectedPreset().catch((error) => setStatus(error.message)));
    savePresetButton.addEventListener("click", () => saveSelectedPreset().catch((error) => setStatus(error.message)));
    saveAsPresetButton.addEventListener("click", () => savePresetAs().catch((error) => setStatus(error.message)));
    deletePresetButton.addEventListener("click", () => deleteSelectedPreset().catch((error) => setStatus(error.message)));
    midiStatusButton.addEventListener("click", () => {
      if (midiStatusPopover.hidden) {
        openMidiPopover();
      } else {
        closeMidiPopover();
      }
    });
    document.addEventListener("click", (event) => {
      if (!statusPanel.contains(event.target)) closeMidiPopover();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMidiPopover();
    });

    loadState().catch((error) => setStatus(error.message));
  </script>
</body>
</html>
"""


class MidiFixHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, format, *args):
        return

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_sse_headers(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def write_sse(self, payload, event=None):
        lines = []
        if event:
            lines.append(f"event: {event}")
        lines.append(f"data: {json.dumps(payload)}")
        body = ("\n".join(lines) + "\n\n").encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/":
                self.send_html(build_app_html())
            elif route == "/api/state":
                self.send_json(self.api_state())
            elif route == "/api/presets":
                self.send_json(self.api_presets())
            elif route == "/api/activity":
                self.api_activity(parse_qs(parsed.query))
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            if route == "/api/blocks":
                self.send_json(self.api_blocks(read_json(self)))
            elif route == "/api/blocks/apply":
                self.send_json(self.api_blocks_apply(read_json(self)))
            elif route == "/api/presets/save":
                self.send_json(self.api_preset_save(read_json(self)))
            elif route == "/api/presets/load":
                self.send_json(self.api_preset_load(read_json(self)))
            elif route == "/api/presets/delete":
                self.send_json(self.api_preset_delete(read_json(self)))
            elif route == "/api/learn":
                self.send_json(self.api_learn(read_json(self)))
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except TimeoutError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.REQUEST_TIMEOUT)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def api_state(self):
        rules = self.state.load_rules()
        templates = load_templates()
        monitor = ACTIVITY_MONITOR.snapshot()
        input_names = monitor["inputs"]
        output_names = monitor["outputs"]
        return {
            "app": "INFINIGHT MidiSurgeon",
            "block_file": str(self.state.block_file),
            "preset_dir": str(self.state.preset_dir),
            "blocked_rules": [tuple_to_rule_text(rule) for rule in sorted(rules)],
            "templates": templates,
            "presets": self.state.list_presets(),
            "controllers": discover_controllers(templates, input_names, output_names),
            "inputs": input_names,
            "input_error": "; ".join(monitor["errors"]) if monitor["errors"] else None,
            "outputs": output_names,
            "output_error": None,
        }

    def api_presets(self):
        return {"presets": self.state.list_presets()}

    def api_blocks(self, payload):
        rule, rules = self.state.set_blocked(payload["rule"], bool(payload["blocked"]))
        return {
            "rule": rule,
            "blocked": bool(payload["blocked"]),
            "blocked_rules": [tuple_to_rule_text(item) for item in sorted(rules)],
        }

    def api_blocks_apply(self, payload):
        rule_texts = payload.get("blocked_rules", [])
        if not isinstance(rule_texts, list):
            raise ValueError("blocked_rules must be a list")
        rules = self.state.set_rules(rule_texts)
        return {
            "blocked_rules": [tuple_to_rule_text(item) for item in sorted(rules)],
        }

    def api_preset_save(self, payload):
        preset = self.state.save_preset(payload)
        return {
            "preset": preset,
            "presets": self.state.list_presets(),
        }

    def api_preset_load(self, payload):
        preset = self.state.load_preset(payload.get("id"))
        rules = self.state.set_rules(preset["blocked_rules"])
        return {
            "preset": preset,
            "presets": self.state.list_presets(),
            "blocked_rules": [tuple_to_rule_text(item) for item in sorted(rules)],
        }

    def api_preset_delete(self, payload):
        self.state.delete_preset(payload.get("id"))
        return {"presets": self.state.list_presets()}

    def api_learn(self, payload):
        input_name = payload.get("input") or "Launch Control XL"
        timeout = float(payload.get("timeout", 10))
        should_block = bool(payload.get("block", True))
        templates = load_templates()
        ignored_rules = self.state.load_rules()
        port_name = midi_filter.find_port(input_name, mido.get_input_names())
        deadline = time.monotonic() + timeout

        with mido.open_input(port_name) as source:
            while time.monotonic() < deadline:
                for msg in source.iter_pending():
                    rule = message_to_rule(msg)
                    if not rule:
                        continue
                    if rule_text_to_tuple(rule) in ignored_rules:
                        continue
                    if should_block:
                        _rule, rules = self.state.set_blocked(rule, True)
                    else:
                        _rule = rule
                        rules = ignored_rules
                    return {
                        "rule": rule,
                        "blocked": should_block,
                        "match": match_template_control(templates, rule),
                        "blocked_rules": [
                            tuple_to_rule_text(item) for item in sorted(rules)
                        ],
                    }
                time.sleep(0.01)

        raise TimeoutError("No MIDI control was received.")

    def api_activity(self, query):
        after_id = int(query.get("after", ["0"])[0])
        self.send_sse_headers()
        self.write_sse(
            {
                "inputs": ACTIVITY_MONITOR.snapshot()["inputs"],
                "status": "Listening for MIDI",
            },
            event="ready",
        )

        last_heartbeat = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                snapshot = ACTIVITY_MONITOR.snapshot(after_id)
                ignored_rules = self.state.load_rules()
                for event in snapshot["events"]:
                    after_id = max(after_id, event["id"])
                    if rule_text_to_tuple(event["rule"]) in ignored_rules:
                        continue
                    self.write_sse(event)

                if now - last_heartbeat >= 10:
                    self.write_sse(
                        {
                            "inputs": snapshot["inputs"],
                            "status": "Listening for MIDI",
                        },
                        event="heartbeat",
                    )
                    last_heartbeat = now

                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return


class MidiFixServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(args):
    port = find_available_port(args.port)
    ACTIVITY_MONITOR.ensure_started()
    state = MidiFixState(args.block_file, args.preset_dir)
    handler = type("ConfiguredMidiFixHandler", (MidiFixHandler,), {"state": state})
    server = MidiFixServer((args.host, port), handler)
    url = f"http://{args.host}:{port}"
    print(f"INFINIGHT MidiSurgeon is running at {url}", flush=True)
    print(f"Blocklist: {Path(args.block_file).expanduser()}", flush=True)
    print(f"Presets: {state.preset_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--block-file",
        default=DEFAULT_BLOCK_FILE,
        help="path to the blocklist controlled by the UI",
    )
    parser.add_argument(
        "--preset-dir",
        default=DEFAULT_PRESET_DIR,
        help="directory for saved presets",
    )
    args = parser.parse_args(argv)
    serve(args)


if __name__ == "__main__":
    main()
