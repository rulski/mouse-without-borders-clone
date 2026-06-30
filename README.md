# Mouse Without Borders Clone

This is a small Python LAN keyboard and mouse sharing daemon inspired by Microsoft Mouse Without Borders. It runs one process per computer. Hosts share their keyboard and mouse, while clients can either listen directly or keep connecting back to the host.

It is an MVP, not a kernel-level input driver. Native input capture/injection is handled by `pynput`, which works best when the program is launched in the desktop OS session you want to control. If you are on Windows and this repo is inside WSL, run the installed command from Windows Python rather than inside WSL.

## Features

- Signed JSON-line protocol over TCP using a shared pairing secret.
- Always-looking client mode that reconnects to a host until it is available.
- Agent mode for receiving mouse, keyboard, click, and scroll events.
- Controller mode that switches to a peer when the pointer hits an edge.
- Browser controller mode for locked-down devices that can open a web page but cannot run an executable.
- Host layout editor for moving peer devices to the left, right, top, or bottom edge and controlling per-client features.
- Host-controlled keep-awake mode for selected clients.
- Text clipboard sync from always-looking clients back to the host.
- Pointer locking while remote control is active so relative motion can continue past the local screen edge.
- Optional local event suppression while controlling a peer, when supported by the OS/backend.
- Small local dashboard at `http://127.0.0.1:45446`.
- Null backend for safe dry runs and tests.

## Install

```bash
cd /home/rulski/Projects/mouse-without-borders-clone
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[input]"
```

For a dry run without native hooks:

```bash
pip install -e .
```

## Quick Start

Run this on every computer:

```bash
mwbc init --name DESKTOP
```

Copy the printed `pairing_secret` so both machines use the same value in `~/.mwbc/config.json`.

On the host machine whose mouse and keyboard you want to use, add each client by name and edge. The name must match the client's `machine_name` in its config.

```bash
mwbc add-peer --name LAPTOP --edge right
mwbc add-peer --name MACBOOK --edge left
mwbc add-peer --name WINDOWS2 --edge top
```

Start the host:

```bash
mwbc host
```

Start each client/target machine:

```bash
mwbc client --host 192.168.1.10
```

Replace `192.168.1.10` with the host machine's LAN IP or DNS name. The client keeps retrying until the host is reachable.

Move the host machine's pointer into a configured edge. For example, `--edge right` means the client is logically to the right of the host screen. Move left on the remote screen edge to return to the host.

When a connected client copies plain text, the client sends that text to the host clipboard. For example, text copied on `MACBOOK` can be pasted back on the Windows host.

Press `F12` on the host to toggle host lock. When host lock is on, edge switching is paused so the mouse and keyboard stay on the host. If you press `F12` while controlling a client, MWBC returns control to the host immediately. Press `F12` again to resume edge switching.

## Layout Editor

When the host starts with the dashboard enabled, it prints a URL like:

```text
Layout editor: http://127.0.0.1:45446/layout#token=...
```

Open that URL on the host. Drag devices to the edge where they physically sit, or use each device's edge selector. Changes are saved to `~/.mwbc/config.json` and the running host refreshes immediately.

The host UI is also where client features are controlled. For now, each client has a `Keep awake` checkbox and interval. When enabled, the host sends that setting to the connected client, and the client performs a tiny local mouse nudge at the configured interval so that device stays awake. The nudge pauses while the host is actively controlling that client.

For your layout:

```bash
mwbc add-peer --name MACBOOK --edge left
mwbc add-peer --name WINDOWS2 --edge top
mwbc host
```

Then open the printed layout URL and adjust the devices visually if needed.

## Direct Agent Mode

You can still use the older direct mode when the host can connect directly to a fixed client IP.

On the client/target:

```bash
mwbc agent
```

On the host/controller:

```bash
mwbc add-peer --name LAPTOP --host 192.168.1.25 --edge right
mwbc host
```

## Browser Controller Mode

Use this when the device with the physical mouse and keyboard cannot run local software, but it can open a browser. The receiving/target machine still needs to run MWBC because browsers cannot inject OS-level input into another machine by themselves.

On the target machine:

```bash
mwbc run --dashboard-host 0.0.0.0
```

The terminal prints a URL like:

```text
Web controller: http://192.168.1.50:45446/controller#token=...
```

Open that URL from the locked-down device's browser. Press `Start` on the page to enter pointer lock. Mouse movement, clicks, wheel events, and common keyboard events will be sent to the target machine.

Notes:

- The URL fragment after `#token=` is the pairing secret. Treat it like a password.
- Some browser or OS-reserved key combinations may not be capturable from a normal web page.
- Open TCP port `45446` to the target machine if a local firewall blocks the dashboard.
- For production use on an untrusted network, put the dashboard behind HTTPS and stronger session controls.

## Startup/Login Install

MWBC can register itself as a per-user startup app on Windows and macOS. This does not require admin rights and is the right first step for an installer.

For a target/client machine that should always look for the host:

```bash
mwbc startup install --mode client --host 192.168.1.10
```

For a host machine that should run at login:

```bash
mwbc startup install --mode host
```

For a controller-only machine:

```bash
mwbc startup install --mode controller
```

Check or remove the startup entry:

```bash
mwbc startup status
mwbc startup uninstall
```

On Windows, this writes a current-user Startup Apps entry under:

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MWBC
```

On macOS, this writes a per-user LaunchAgent:

```text
~/Library/LaunchAgents/com.localcodex.mwbc.plist
```

On macOS, native mouse/keyboard control usually requires granting the installed app Accessibility and Input Monitoring permissions. The app should be signed/notarized before broader distribution.

When we package this as a real installer, the installer can run `mwbc startup install --mode client --host HOSTNAME` during first setup for client/target devices, or expose it as a checkbox named something like "Start MWBC when I sign in."

## Config

Default config path:

```text
~/.mwbc/config.json
```

Example:

```json
{
  "machine_name": "DESKTOP",
  "pairing_secret": "paste-the-same-secret-on-both-machines",
  "listen_host": "0.0.0.0",
  "listen_port": 45445,
  "dashboard_host": "127.0.0.1",
  "dashboard_port": 45446,
  "backend": "auto",
  "clipboard_enabled": true,
  "clipboard_poll_seconds": 0.5,
  "clipboard_max_text_bytes": 262144,
  "suppress_local_events_when_remote": true,
  "edge_threshold_px": 2,
  "peers": [
    {
      "name": "LAPTOP",
      "edge": "right",
      "port": 45445,
      "keep_awake": false,
      "keep_awake_interval_seconds": 45.0
    }
  ]
}
```

For always-looking clients, `host` can be omitted. For direct agent mode, set `host` to the client's IP or DNS name. You can set `shared_secret` on a peer if you want a different secret for that specific machine. Per-peer feature settings, such as `keep_awake`, are owned by the host config and pushed to clients when they connect or when you save the host UI.

## Commands

```bash
mwbc init
mwbc secret
mwbc add-peer --name LAPTOP --edge right
mwbc add-peer --name LAPTOP --host 192.168.1.25 --edge right
mwbc add-peer --name MACBOOK --edge left
mwbc add-peer --name WINDOWS2 --edge top
mwbc host
mwbc client --host 192.168.1.10
mwbc run
mwbc run --dashboard-host 0.0.0.0
mwbc agent
mwbc controller
mwbc startup install --mode client --host 192.168.1.10
mwbc startup install --mode host
mwbc startup status
mwbc startup uninstall
```

Useful development commands:

```bash
python -m unittest discover -s tests
python -m compileall src
mwbc run --backend null
```

## Notes

- Open TCP port `45445` between machines.
- Open TCP port `45446` when using browser controller mode from another device.
- Keep the dashboard bound to `127.0.0.1` unless you intentionally want it reachable from the LAN.
- Clipboard sync, file drag/drop, auto-discovery, and per-monitor geometry are natural next additions.
