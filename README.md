# midifix

`midifix` is a local MIDI repair app for noisy controllers. It filters broken
faders and knobs from a hardware input, forwards everything else to a clean
virtual MIDI port, and gives you a browser UI for turning individual controls on
or off. The interface uses a retro skill-game board style with red fault
lights for blocked or noisy controls.

The stuck message detected from the Launch Control XL was:

```text
control_change channel=0 control=77 value=56
```

In normal MIDI numbering, that is **CC 77 on MIDI channel 1**. The included
Launch Control XL template maps that to **Fader 1**.

## Start The App

Open the standalone macOS app:

```sh
open /Applications/midifix.app
```

Or start the same desktop app from Terminal:

```sh
./midifix_app.sh
```

For browser-based development, you can still run:

```sh
./midifix.sh
```

and open the printed local URL, usually:

```text
http://127.0.0.1:8765
```

## Build Installer DMG

Create a distributable DMG:

```sh
./build_dmg.sh
```

The output is:

```text
dist/midifix.dmg
```

Install the app by dragging `midifix.app` to Applications from the DMG, or copy
the built app directly:

```sh
ditto build/dmg/midifix.app /Applications/midifix.app
```

Click a fader or knob to block it. Click it again to let it pass. The running
filter reloads `blocked_controls.txt` every second, so UI changes take effect
without restarting the MIDI filter.

Move a fader or knob and its tile lights up briefly. If a control is stuck and
keeps sending MIDI, that tile will keep pulsing so you can spot it quickly.
Once you block that control, midifix ignores it in the activity monitor and in
learn mode, so another bad control can be detected next.

The connected MIDI area shows ports detected on the system. Known controllers
are matched to templates automatically, while unknown or virtual ports are still
shown so you can see what macOS is reporting.

Use **Learn next control** to listen for the next MIDI CC/note message from the
selected input and block it automatically. This is handy when a broken fader is
spamming values and you do not want to look up its CC number.

## Controller Templates

Templates live in `controller_templates/*.json`. Each template names the
controller and lists controls with block rules:

```json
{
  "id": "novation-launch-control-xl-factory",
  "name": "Novation Launch Control XL",
  "input_name": "Launch Control XL",
  "output_name": "Launch Control XL Filtered",
  "controls": [
    { "type": "fader", "row": "Faders", "label": "F1", "rule": "cc:1:77" }
  ]
}
```

Included profiles currently cover:

- Akai APC40 mkII
- Akai MPK Mini IV
- Arturia KeyLab Essential mk3 49
- Arturia MiniLab 3
- Novation Launch Control XL
- Novation Launchkey Mini 25 MK4
- Novation Launchpad X

Block rules use this format:

```text
cc:<midi-channel>:<cc-number>
note:<midi-channel>:<note-number>
```

## Important DAW Setup

Set your DAW/app MIDI input to `Launch Control XL Filtered` and disable the raw
`Launch Control XL` input there. macOS cannot force every app to ignore the raw
hardware port, so each music app still needs to listen to the filtered virtual
port.

## Install As Login App

Install the filter as a LaunchAgent so it starts when you log in:

```sh
./install_midifix.sh
```

Check status:

```sh
./status_midifix.sh
```

Uninstall the login app:

```sh
./uninstall_midifix.sh
```

After installation, the live app files are in:

```text
~/Library/Application Support/midifix
```

Logs are in:

```text
~/Library/Logs/midifix
```

## Command Line

Show MIDI ports:

```sh
.venv/bin/python midi_filter.py list
```

Show blocked controls:

```sh
./midi_blocker.sh blocks list
```

Add a blocked control:

```sh
./midi_blocker.sh blocks add cc:1:78
```

Remove a blocked control:

```sh
./midi_blocker.sh blocks remove cc:1:78
```

Start the filter manually:

```sh
.venv/bin/python midi_filter.py filter --input "Launch Control XL"
```
