# claude-desktop-buddy

> **Fork note:** This is a fork of
> [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
> that adds a **Claude Code (CLI)-only bridge** — no desktop app
> required. Driven by Claude Code's hook system. The firmware and BLE
> wire protocol are unchanged from upstream, so a stick paired with the
> desktop app still works here, and vice versa. See
> [Claude Code support](#claude-code-support-this-fork) below.

Claude for macOS and Windows can connect Claude Cowork and Claude Code to
maker devices over BLE, so developers and makers can build hardware that
displays permission prompts, recent messages, and other interactions. We've
been impressed by the creativity of the maker community around Claude -
providing a lightweight, opt-in API is our way of making it easier to build
fun little hardware devices that integrate with Claude.

> **Building your own device?** You don't need any of the code here. See
> **[REFERENCE.md](REFERENCE.md)** for the wire protocol: Nordic UART
> Service UUIDs, JSON schemas, and the folder push transport.

As an example, we built a desk pet on ESP32 that lives off permission
approvals and interaction with Claude. It sleeps when nothing's happening,
wakes when sessions start, gets visibly impatient when an approval prompt is
waiting, and lets you approve or deny right from the device.

<p align="center">
  <img src="docs/device.jpg" alt="M5StickC Plus running the buddy firmware" width="500">
</p>

## Hardware

The firmware targets ESP32 with the Arduino framework. As written, it
depends on the M5StickCPlus library for its display, IMU, and button
drivers—so you'll need that board, or a fork that swaps those drivers for
your own pin layout.

## Flashing

Install
[PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/),
then:

```bash
pio run -t upload
```

If you're starting from a previously-flashed device, wipe it first:

```bash
pio run -t erase && pio run -t upload
```

Once running, you can also wipe everything from the device itself: **hold A
→ settings → reset → factory reset → tap twice**.

## Pairing

To pair your device with Claude, first enable developer mode (**Help →
Troubleshooting → Enable Developer Mode**). Then, open the Hardware Buddy
window in **Developer → Open Hardware Buddy…**, click **Connect**, and pick
your device from the list. macOS will prompt for Bluetooth permission on
first connect; grant it.

<p align="center">
  <img src="docs/menu.png" alt="Developer → Open Hardware Buddy… menu item" width="420">
  <img src="docs/hardware-buddy-window.png" alt="Hardware Buddy window with Connect button and folder drop target" width="420">
</p>

Once paired, the bridge auto-reconnects whenever both sides are awake.

If discovery isn't finding the stick:

- Make sure it's awake (any button press)
- Check the stick's settings menu → bluetooth is on

## Claude Code support (this fork)

The upstream bridge runs inside the Claude desktop apps. This fork adds
an alternate bridge that runs as a small daemon alongside Claude Code,
fed by Claude Code's hook system. Same buddy, same display, same
A=approve / B=deny — driven by the CLI instead of the GUI.

```
Claude Code session            cc_buddy_daemon              M5StickC
┌──────────────────┐ ──hook──► ┌──────────────┐ ──serial/BLE──► ┌──┐
│   Stop hook      │           │ owns the     │                 │  │
│   PreToolUse hook│ ◄──reply─ │ transport    │ ◄──button──────│  │
└──────────────────┘           └──────────────┘                 └──┘
```

The pieces:

- **`tools/cc_buddy_daemon.py`** — long-running process. Owns the USB
  serial or BLE link, sends a 10s keepalive so the stick stays
  "connected", aggregates `output_tokens` across sessions, and
  serializes permission requests so the firmware sees one prompt at a
  time.
- **`tools/cc_stop_hook.py`** — fires after every assistant turn. Reads
  the session's transcript JSONL, sums tokens (deduped by message id),
  pulls recent tool calls and user prompts into a heartbeat, sends to
  the daemon.
- **`tools/cc_pretool_hook.py`** — fires before every tool call. Asks
  the daemon for an approval, blocks for the device's response, returns
  Claude Code's `permissionDecision` JSON. On daemon-down, timeout, or
  bypass-mode sessions, exits silently so Claude Code's normal terminal
  prompt takes over — the buddy is additive, never a hard dependency.
- **`tools/install.sh`** — idempotent installer.

### Install

```bash
git clone https://github.com/patrickvossler18/claude-desktop-buddy
cd claude-desktop-buddy
./tools/install.sh
```

The installer:

1. Creates a Python venv at `~/.local/share/cc-buddy/venv`.
2. Installs `pyserial` and `bleak` into it.
3. Generates wrapper scripts in `~/.local/share/cc-buddy/bin/` that
   invoke the venv python with the hook scripts.
4. Adds `Stop` and `PreToolUse` hook entries to
   `~/.claude/settings.json` (merging — won't clobber other hooks or
   settings).

Override the install location with `CC_BUDDY_HOME=/some/path`. Override
the Python interpreter used to create the venv with
`CC_BUDDY_PYTHON=/path/to/python3` (defaults to whatever `python3` is on
PATH; needs 3.10+).

### Use

Start the daemon (leave it running in a terminal):

```bash
~/.local/share/cc-buddy/bin/cc_buddy_daemon
```

To run over BLE instead of USB (unplug the cable from the stick first):

```bash
BUDDY_TRANSPORT=ble ~/.local/share/cc-buddy/bin/cc_buddy_daemon
```

#### One-time BLE pairing on macOS

The firmware's NUS characteristics are encrypted-only (LE Secure
Connections), so the very first connection from a given laptop has to
pair. The daemon will trigger a pairing request on first run, and
*usually* macOS surfaces the passkey dialog automatically — but for
CLI-launched processes it sometimes doesn't (the dialog is normally
triggered by foreground signed GUI apps). If `tail -f /tmp/cc_buddy.log`
shows the daemon stuck repeating `[ble] error: Encryption is
insufficient`, follow these one-time steps:

1. **Stop the daemon** (Ctrl-C). The hung connection blocks the
   workaround app.
2. Install **LightBlue** (free, App Store — Punch Through's BLE
   explorer) or any other foreground macOS BLE tool.
3. Open it, find **`Claude-XXXX`** in the device list, tap **Connect**.
4. macOS pops the passkey dialog — type the 6-digit number on the
   stick.
5. Once it shows "Connected", **quit the app** (it holds the BLE link
   exclusively).
6. Restart the daemon. The OS-level bond is now stored, and the daemon
   reconnects silently from here on.

Subsequent connects (sleep, reboot, daemon restart) all reuse the
stored bond — no dialog, no app. You only repeat this dance per
laptop, or if you factory-reset the stick.

If the macOS Bluetooth pane (System Settings → Bluetooth) doesn't show
the stick, that's normal — BLE-only peripherals without a standard
profile don't appear there even when paired.

Then start a `claude` session as usual. The next assistant turn ticks
the stick (heartbeat, token count, recent activity). The next tool call
shows a permission prompt on the screen — **A** allows it, **B** denies.
Timeout is 30 seconds, after which Claude Code falls back to its normal
terminal prompt.

Tail the unified log to see what's happening:

```bash
tail -f /tmp/cc_buddy.log
```

### Configuration

All of these are optional environment variables:

| Variable           | Default                  | Purpose                                              |
| ------------------ | ------------------------ | ---------------------------------------------------- |
| `BUDDY_TRANSPORT`  | auto (serial → BLE)      | Force `serial` or `ble`.                             |
| `BUDDY_PORT`       | first `/dev/cu.usbserial-*` | Explicit serial device path.                      |
| `BUDDY_BLE_NAME`   | `Claude`                 | BLE advertising-name prefix to match.                |
| `BUDDY_SOCK`       | `/tmp/cc_buddy.sock`     | Unix socket the hooks talk to the daemon on.        |
| `BUDDY_LOG`        | `/tmp/cc_buddy.log`      | Unified daemon + hook log path.                     |

### Multi-laptop

Hooks fire in the Claude Code process on whichever machine you're using,
so each laptop needs its own `./tools/install.sh`. The stick itself is
portable — pair it once per laptop over BLE and it'll roam between them.

### Permission-mode handling

The PreToolUse hook respects Claude Code's permission modes: if a
session is running with `bypassPermissions` or `acceptEdits`, the hook
exits immediately without prompting the device. The buddy is for
sessions where you've explicitly opted *in* to per-tool approvals.

### Firmware

Identical to upstream. If you've already flashed your stick for the
desktop app, you don't need to reflash. If you're starting fresh, follow
the **Hardware** and **Flashing** sections above.

## Controls

|                         | Normal               | Pet         | Info        | Approval    |
| ----------------------- | -------------------- | ----------- | ----------- | ----------- |
| **A** (front)           | next screen          | next screen | next screen | **approve** |
| **B** (right)           | scroll transcript    | next page   | next page   | **deny**    |
| **Hold A**              | menu                 | menu        | menu        | menu        |
| **Power** (left, short) | toggle screen off    |             |             |             |
| **Power** (left, ~6s)   | hard power off       |             |             |             |
| **Shake**               | dizzy                |             |             | —           |
| **Face-down**           | nap (energy refills) |             |             |             |

The screen auto-powers-off after 30s of no interaction (kept on while an
approval prompt is up). Any button press wakes it.

## ASCII pets

Eighteen pets, each with seven animations (sleep, idle, busy, attention,
celebrate, dizzy, heart). Menu → "next pet" cycles them with a counter.
Choice persists to NVS.

## GIF pets

If you want a custom GIF character instead of an ASCII buddy, drag a
character pack folder onto the drop target in the Hardware Buddy window. The
app streams it over BLE and the stick switches to GIF mode live. **Settings
→ delete char** reverts to ASCII mode.

A character pack is a folder with `manifest.json` and 96px-wide GIFs:

```json
{
  "name": "bufo",
  "colors": {
    "body": "#6B8E23",
    "bg": "#000000",
    "text": "#FFFFFF",
    "textDim": "#808080",
    "ink": "#000000"
  },
  "states": {
    "sleep": "sleep.gif",
    "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"],
    "busy": "busy.gif",
    "attention": "attention.gif",
    "celebrate": "celebrate.gif",
    "dizzy": "dizzy.gif",
    "heart": "heart.gif"
  }
}
```

State values can be a single filename or an array. Arrays rotate: each
loop-end advances to the next GIF, useful for an idle activity carousel so
the home screen doesn't loop one clip forever.

GIFs are 96px wide; height up to ~140px stays on a 135×240 portrait screen.
Crop tight to the character — transparent margins waste screen and shrink
the sprite. `tools/prep_character.py` handles the resize: feed it source
GIFs at any sizes and it produces a 96px-wide set where the character is the
same scale in every state.

The whole folder must fit under 1.8MB —
`gifsicle --lossy=80 -O3 --colors 64` typically cuts 40–60%.

See `characters/bufo/` for a working example.

If you're iterating on a character and would rather skip the BLE round-trip,
`tools/flash_character.py characters/bufo` stages it into `data/` and runs
`pio run -t uploadfs` directly over USB.

## The seven states

| State       | Trigger                     | Feel                        |
| ----------- | --------------------------- | --------------------------- |
| `sleep`     | bridge not connected        | eyes closed, slow breathing |
| `idle`      | connected, nothing urgent   | blinking, looking around    |
| `busy`      | sessions actively running   | sweating, working           |
| `attention` | approval pending            | alert, **LED blinks**       |
| `celebrate` | level up (every 50K tokens) | confetti, bouncing          |
| `dizzy`     | you shook the stick         | spiral eyes, wobbling       |
| `heart`     | approved in under 5s        | floating hearts             |

## Project layout

```
src/
  main.cpp       — loop, state machine, UI screens
  buddy.cpp      — ASCII species dispatch + render helpers
  buddies/       — one file per species, seven anim functions each
  ble_bridge.cpp — Nordic UART service, line-buffered TX/RX
  character.cpp  — GIF decode + render
  data.h         — wire protocol, JSON parse
  xfer.h         — folder push receiver
  stats.h        — NVS-backed stats, settings, owner, species choice
characters/      — example GIF character packs
tools/           — generators and converters
```

## Availability

The BLE API is only available when the desktop apps are in developer mode
(**Help → Troubleshooting → Enable Developer Mode**). It's intended for
makers and developers and isn't an officially supported product feature.
