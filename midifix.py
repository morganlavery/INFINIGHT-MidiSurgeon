#!/usr/bin/env python3
"""Local web UI for turning noisy MIDI controls on and off."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
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
    def __init__(self, block_file):
        self.block_file = Path(block_file).expanduser()

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
  <title>midi-FIX</title>
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
      --danger: #bd111b;
      --danger-weak: #f9c9bd;
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
      font-size: 28px;
      line-height: 1;
      font-weight: 760;
    }

    .status {
      min-height: 24px;
      color: #fff3a6;
      font-size: 14px;
      text-align: right;
      font-weight: 700;
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
      letter-spacing: 1px;
    }

    .vertical-logo {
      color: #c91822;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(56px, 7vw, 88px);
      line-height: 0.82;
      font-weight: 900;
      letter-spacing: -2px;
      writing-mode: vertical-rl;
      transform: rotate(180deg);
      text-shadow: 2px 2px 0 #ffed70;
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
      font-size: clamp(14px, 1.8vw, 19px);
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
      grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto auto;
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

    .control.active {
      border-color: #8f0b15;
      background: #ffd8c5;
    }

    .control.moving {
      border-color: var(--activity-strong);
      box-shadow:
        0 0 0 4px rgba(255, 255, 255, 0.9),
        0 0 24px rgba(224, 0, 19, 0.75);
      transform: translateY(-1px);
    }

    .control.moving .knob-face,
    .control.moving .fader-face::after,
    .control.moving .pad-face,
    .control.moving .button-face {
      border-color: var(--activity-strong);
      background-color: var(--activity);
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

    .lcxl-board .control.active {
      background: #34383a;
      border-color: #151719;
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

      .toolbar {
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
        writing-mode: horizontal-tb;
        transform: none;
        font-size: 54px;
        line-height: 1;
        letter-spacing: -1px;
      }

      .playfield {
        padding-top: 78px;
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
        font-size: 42px;
      }

      .toolbar {
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
      <h1>midi-FIX</h1>
      <div class="status" id="status"></div>
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
          <div class="vertical-logo">midi-FIX</div>
        </aside>
        <section class="playfield">
          <div class="toolbar">
            <select id="templateSelect" aria-label="Controller template"></select>
            <select id="inputSelect" aria-label="MIDI input"></select>
            <button class="button primary" id="learnButton" type="button">Learn next control</button>
            <button class="button" id="refreshButton" type="button">Refresh</button>
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
      inputs: [],
      controllers: [],
      activitySource: null,
      activityAbort: null,
      activityTimers: new Map(),
    };

    const templateSelect = document.querySelector("#templateSelect");
    const inputSelect = document.querySelector("#inputSelect");
    const controllers = document.querySelector("#controllers");
    const board = document.querySelector("#board");
    const status = document.querySelector("#status");
    const learnButton = document.querySelector("#learnButton");
    const refreshButton = document.querySelector("#refreshButton");

    function setStatus(text) {
      status.textContent = text;
    }

    function selectedTemplate() {
      return state.templates.find((template) => template.id === state.selectedTemplateId) || state.templates[0];
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
      if (state.selectedTemplateId) {
        templateSelect.value = state.selectedTemplateId;
      }

      inputSelect.innerHTML = "";
      const template = selectedTemplate();
      const preferredInput = template ? template.input_name : "";
      const inputNames = state.inputs.length ? state.inputs : [preferredInput].filter(Boolean);
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
      const button = document.createElement("button");
      button.type = "button";
      button.className = [
        "control",
        `type-${control.type || "knob"}`,
        `control-${control.id || "unknown"}`,
        isBlocked ? "blocked" : "active",
        hasRule ? "" : "visual-only",
      ].filter(Boolean).join(" ");
      button.setAttribute("aria-pressed", String(isBlocked));
      button.disabled = !hasRule;
      button.title = hasRule ? `${control.label} - ${isBlocked ? "Blocked" : "Passing"} - ${ruleText}` : `${control.label} - ${control.kind || "visual layout"}`;
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
        button.addEventListener("click", () => toggleRule(ruleText, !isBlocked));
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
        if (data.status) setStatus(`${data.status}: ${data.inputs.join(", ")}`);
      });
      source.addEventListener("heartbeat", (event) => {
        const data = JSON.parse(event.data);
        if (data.status && !state.activityTimers.size) setStatus(`${data.status}: ${data.inputs.join(", ")}`);
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
              setStatus(`${data.status}: ${data.inputs.join(", ")}`);
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
      const detected = state.controllers.find((controller) => controller.template_id);
      state.selectedTemplateId = state.selectedTemplateId || detected?.template_id || data.templates[0]?.id || null;
      state.selectedInput = state.selectedInput || detected?.input || null;
      state.rules = new Set(data.blocked_rules);
      state.inputs = Array.isArray(data.inputs) ? data.inputs : [];
      renderSelects();
      renderControllers();
      renderBoard();
      startActivityStream();
      setStatus(`${state.rules.size} blocked controls`);
    }

    async function toggleRule(rule, blocked) {
      setStatus(blocked ? `Blocking ${rule}` : `Passing ${rule}`);
      const data = await fetchJson("/api/blocks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule, blocked }),
      });
      state.rules = new Set(data.blocked_rules);
      renderBoard();
      setStatus(`${rule} ${blocked ? "blocked" : "passing"}`);
    }

    async function learnNextControl() {
      learnButton.disabled = true;
      setStatus("Listening for MIDI");
      try {
        const data = await fetchJson("/api/learn", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: inputSelect.value, block: true, timeout: 10 }),
        });
        state.rules = new Set(data.blocked_rules);
        renderBoard();
        const label = data.match?.control?.label || data.rule;
        setStatus(`${label} blocked`);
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
      startActivityStream();
    });
    inputSelect.addEventListener("change", () => {
      state.selectedInput = inputSelect.value;
      renderControllers();
      startActivityStream();
    });
    refreshButton.addEventListener("click", () => loadState().catch((error) => setStatus(error.message)));
    learnButton.addEventListener("click", () => learnNextControl().catch((error) => setStatus(error.message)));

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
            "app": "midifix",
            "block_file": str(self.state.block_file),
            "blocked_rules": [tuple_to_rule_text(rule) for rule in sorted(rules)],
            "templates": templates,
            "controllers": discover_controllers(templates, input_names, output_names),
            "inputs": input_names,
            "input_error": "; ".join(monitor["errors"]) if monitor["errors"] else None,
            "outputs": output_names,
            "output_error": None,
        }

    def api_blocks(self, payload):
        rule, rules = self.state.set_blocked(payload["rule"], bool(payload["blocked"]))
        return {
            "rule": rule,
            "blocked": bool(payload["blocked"]),
            "blocked_rules": [tuple_to_rule_text(item) for item in sorted(rules)],
        }

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
                    _rule, rules = self.state.set_blocked(rule, should_block)
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
    state = MidiFixState(args.block_file)
    handler = type("ConfiguredMidiFixHandler", (MidiFixHandler,), {"state": state})
    server = MidiFixServer((args.host, port), handler)
    url = f"http://{args.host}:{port}"
    print(f"midifix is running at {url}", flush=True)
    print(f"Blocklist: {Path(args.block_file).expanduser()}", flush=True)
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
    args = parser.parse_args(argv)
    serve(args)


if __name__ == "__main__":
    main()
