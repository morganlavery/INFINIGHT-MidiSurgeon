#!/usr/bin/env python3
"""Local web UI for turning noisy MIDI controls on and off."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
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
  <title>midifix</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17212b;
      --muted: #687789;
      --line: #d8e0e8;
      --panel: #f7f9fb;
      --surface: #ffffff;
      --accent: #24736f;
      --accent-weak: #dff2ee;
      --danger: #a23b3b;
      --danger-weak: #f8dfdc;
      --activity: #f0c14b;
      --activity-strong: #d08a00;
      --shadow: 0 16px 44px rgba(29, 45, 57, 0.12);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background: #edf2f6;
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
      padding: 18px 22px;
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }

    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1;
      font-weight: 760;
    }

    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 14px;
      text-align: right;
    }

    main {
      width: min(1180px, 100%);
      margin: 0 auto;
      padding: 20px;
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
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      padding: 0 12px;
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
      color: white;
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
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      padding: 10px 12px;
      cursor: pointer;
    }

    .controller-card.recognized {
      border-color: #91cfc4;
      background: var(--accent-weak);
    }

    .controller-card.selected {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(36, 115, 111, 0.2);
    }

    .controller-card.virtual {
      color: var(--muted);
      background: #f8fafc;
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
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      border-radius: 8px;
      padding: 18px;
    }

    .group {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 12px;
      align-items: center;
      padding: 14px 0;
      border-top: 1px solid var(--line);
    }

    .group:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .group:last-child {
      padding-bottom: 0;
    }

    .group-title {
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
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
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
    }

    .control.blocked {
      border-color: #e0a39d;
      background: var(--danger-weak);
      color: #5f2020;
    }

    .control.active {
      border-color: #91cfc4;
      background: var(--accent-weak);
    }

    .control.moving {
      border-color: var(--activity-strong);
      box-shadow:
        0 0 0 3px rgba(240, 193, 75, 0.72),
        0 0 22px rgba(208, 138, 0, 0.44);
      transform: translateY(-1px);
    }

    .control.moving .knob-face,
    .control.moving .fader-face::after {
      border-color: var(--activity-strong);
      background-color: var(--activity);
    }

    .knob-face {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      border: 8px solid #405261;
      background: radial-gradient(circle at 50% 50%, #f8fbfd 0 28%, #bdc8d1 29% 100%);
    }

    .fader-face {
      width: 34px;
      height: 44px;
      border-radius: 8px;
      background:
        linear-gradient(#405261, #405261) center/4px 100% no-repeat,
        linear-gradient(#f8fbfd, #c4ced7);
      border: 1px solid #a8b5c0;
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
      background: #405261;
    }

    .pad-face {
      width: 40px;
      height: 32px;
      border-radius: 7px;
      border: 2px solid #445565;
      background: linear-gradient(#617181, #394858);
      box-shadow: inset 0 0 0 5px rgba(255, 255, 255, 0.08);
    }

    .key-face {
      width: 24px;
      height: 44px;
      border-radius: 0 0 5px 5px;
      border: 1px solid #a8b5c0;
      background: linear-gradient(#ffffff, #dbe3ea);
    }

    .key-face.black {
      width: 18px;
      height: 36px;
      border-color: #17212b;
      background: linear-gradient(#334252, #121a22);
    }

    .wheel-face,
    .strip-face {
      width: 30px;
      height: 44px;
      border-radius: 14px;
      border: 1px solid #8e9ba6;
      background: linear-gradient(#344454, #17212b);
    }

    .strip-face {
      width: 20px;
      background: linear-gradient(#1f695f, #17212b);
    }

    .screen-face {
      width: 52px;
      height: 26px;
      border-radius: 5px;
      border: 1px solid #2d4951;
      background: linear-gradient(#8dd1c8, #1f695f);
      box-shadow: inset 0 0 8px rgba(255, 255, 255, 0.35);
    }

    .button-face {
      width: 34px;
      height: 24px;
      border-radius: 6px;
      border: 1px solid #a8b5c0;
      background: linear-gradient(#f8fbfd, #c4ced7);
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

      .toolbar {
        grid-template-columns: 1fr;
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
      <h1>midifix</h1>
      <div class="status" id="status"></div>
    </header>
    <main>
      <div class="toolbar">
        <select id="templateSelect" aria-label="Controller template"></select>
        <select id="inputSelect" aria-label="MIDI input"></select>
        <button class="button primary" id="learnButton" type="button">Learn next control</button>
        <button class="button" id="refreshButton" type="button">Refresh</button>
      </div>
      <section class="controllers" id="controllers"></section>
      <section class="board" id="board"></section>
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

    function renderBoard() {
      const template = selectedTemplate();
      board.innerHTML = "";
      if (!template) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No controller templates found.";
        board.append(empty);
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
          const rules = rulesForControl(control);
          const ruleText = primaryRule(control);
          const hasRule = rules.length > 0;
          const isBlocked = rules.some((item) => state.rules.has(item));
          const button = document.createElement("button");
          button.type = "button";
          button.className = `control ${isBlocked ? "blocked" : "active"} ${hasRule ? "" : "visual-only"}`;
          button.setAttribute("aria-pressed", String(isBlocked));
          button.disabled = !hasRule;
          button.title = hasRule ? (isBlocked ? "Blocked" : "Passing") : "Visual layout";
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
          grid.append(button);
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
      const control = template?.controls?.find((item) => item.rule === rule);
      return control ? `${control.label} ${rule}` : rule;
    }

    function markMoved(rule) {
      const control = document.querySelector(`.control[data-rules~="${CSS.escape(rule)}"]`);
      if (!control) {
        setStatus(`MIDI activity: ${rule}`);
        return;
      }
      control.classList.add("moving");
      setStatus(`MIDI activity: ${controlNameForRule(rule)}`);
      if (state.activityTimers.has(rule)) {
        clearTimeout(state.activityTimers.get(rule));
      }
      state.activityTimers.set(rule, setTimeout(() => {
        control.classList.remove("moving");
        state.activityTimers.delete(rule);
      }, 320));
    }

    function startActivityStream() {
      stopActivityStream();
      if (!inputSelect.value) return;
      const url = `/api/activity?input=${encodeURIComponent(inputSelect.value)}`;
      if (!("EventSource" in window)) {
        startFetchActivityStream(url);
        return;
      }
      const source = new EventSource(url);
      state.activitySource = source;
      source.addEventListener("message", (event) => {
        const data = JSON.parse(event.data);
        if (data.rule) markMoved(data.rule);
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
            if (data.rule) markMoved(data.rule);
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
        inputs = safe_port_names("input")
        outputs = safe_port_names("output")
        input_names = inputs.get("ports", []) if isinstance(inputs, dict) else inputs
        output_names = outputs.get("ports", []) if isinstance(outputs, dict) else outputs
        return {
            "app": "midifix",
            "block_file": str(self.state.block_file),
            "blocked_rules": [tuple_to_rule_text(rule) for rule in sorted(rules)],
            "templates": templates,
            "controllers": discover_controllers(templates, input_names, output_names),
            "inputs": input_names,
            "input_error": inputs.get("error") if isinstance(inputs, dict) else None,
            "outputs": output_names,
            "output_error": outputs.get("error") if isinstance(outputs, dict) else None,
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
        port_name = midi_filter.find_port(input_name, mido.get_input_names())
        deadline = time.monotonic() + timeout

        with mido.open_input(port_name) as source:
            while time.monotonic() < deadline:
                for msg in source.iter_pending():
                    rule = message_to_rule(msg)
                    if not rule:
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
        input_name = query.get("input", ["Launch Control XL"])[0]
        self.send_sse_headers()

        try:
            port_name = midi_filter.find_port(input_name, mido.get_input_names())
            self.write_sse({"input": port_name, "status": "listening"}, event="ready")
        except Exception as exc:
            self.write_sse({"error": str(exc)}, event="error")
            return

        last_sent = {}
        last_heartbeat = time.monotonic()
        try:
            with mido.open_input(port_name) as source:
                while True:
                    now = time.monotonic()
                    for msg in source.iter_pending():
                        rule = message_to_rule(msg)
                        if not rule:
                            continue
                        if now - last_sent.get(rule, 0) < 0.04:
                            continue
                        last_sent[rule] = now
                        self.write_sse(
                            {
                                "rule": rule,
                                "input": port_name,
                                "message": str(msg),
                            }
                        )

                    if now - last_heartbeat >= 10:
                        self.write_sse({"status": "listening"}, event="heartbeat")
                        last_heartbeat = now

                    time.sleep(0.01)
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
