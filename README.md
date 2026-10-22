# Mouse Without Borders Clone

This is a small Python LAN keyboard and mouse sharing daemon inspired by Microsoft Mouse Without Borders. It runs one process per computer. Hosts share their keyboard and mouse, while clients can either listen directly or keep connecting back to the host.

It is an MVP, not a kernel-level input driver. Native input capture/injection is handled by `pynput`, which works best when the program is launched in the desktop OS session you want to control. If you are on Windows and this repo is inside WSL, run the installed command from Windows Python rather than inside WSL.

## Features

- Mutually authenticated TLS 1.2+ connections with explicitly approved device certificates.
- Session-bound signed messages with strict ordered sequences to reject replay.
- Always-looking client mode that reconnects to a host until it is available.
- Agent mode for receiving mouse, keyboard, click, and scroll events.
- Controller mode that switches to a peer when the pointer hits an edge.
- Browser controller mode for locked-down devices that can open a web page but cannot run an executable.
- Host layout editor for moving peer devices to the left, right, top, or bottom edge and controlling per-client features.
- Host-controlled keep-awake mode for selected clients.
- Bidirectional text and image clipboard sync between the host and always-looking clients.
- Pointer locking while remote control is active so relative motion can continue past the local screen edge.
- Optional local event suppression while controlling a peer, when supported by the OS/backend.
- Small local dashboard at `http://127.0.0.1:45446`.
- Optional tray/menu bar companion for service status, connected devices, host lock state, and quick controls.
- Null backend for safe dry runs and tests.

## Install

```bash
cd /home/rulski/Projects/mouse-without-borders-clone
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[input]"
```

To include the tray/menu bar companion:

```bash
pip install -e ".[input,tray]"
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

Before starting either machine, complete **Secure Pairing** below. Existing
plaintext clients are intentionally incompatible with protocol version 2.

On the host machine whose mouse and keyboard you want to use, add each client by name and edge. The name must match the client's `machine_name` in its config. Always-looking clients must be listed on the host before they are allowed to connect.

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

When a connected host or client copies text or a supported clipboard image, MWBC syncs that clipboard item to the other side. For example, text or screenshots copied on `MACBOOK` can be pasted on the Windows host, and clipboard items copied on the Windows host can be pasted on `MACBOOK`.

Images are sent as PNG payloads and capped by `clipboard_max_image_bytes` in `~/.mwbc/config.json`. The default cap is 8 MB.

Press `F12` on the host to toggle host lock. When host lock is on, edge switching is paused so the mouse and keyboard stay on the host. If you press `F12` while controlling a client, MWBC returns control to the host immediately. Press `F12` again to resume edge switching.

## Secure Pairing

For this local lab, the Windows host's public identity is available at
[`pairing/host-public.json`](pairing/host-public.json). After pulling the repo,
use that path instead of transferring `host-public.json` separately. This is
public certificate material only; private keys and tokens are not published.
Verify its fingerprint against the originating Windows host before approval.

Upgrade the package on both devices (`pip install -e ".[input,tray]"` in each
checkout). Keep native Windows/macOS processes in their desktop OS sessions.
Do not restart a working older installation until both sides are ready.

Create a unique identity once on each machine:

```bash
mwbc security init
mwbc security export
mwbc security export --output device-public.json
```

The export contains the public certificate, machine name, device ID, and SHA-256
fingerprint only. Transfer the host's public file to the client and the client's
public file to the host. Verify each fingerprint independently against the
originating machine's `security export` display before approving it. Trusting
a fingerprint supplied only with an unverified downloaded file is not verification.
Never transfer `private-key.pem`, the full config, or the management token.

On the host, approve the client certificate for the `client` role, and configure
its name as an edge peer as in Quick Start:

```bash
mwbc security trust --file client-public.json --fingerprint VERIFIED_CLIENT_SHA256 --role client
```

On the client, approve the host certificate for the `controller` role:

```bash
mwbc security trust --file host-public.json --fingerprint VERIFIED_HOST_SHA256 --role controller
```

For direct agent mode, the controller approves the target with `--role agent`;
the target approves the controller with `--role controller`. Roles are assigned
locally during approval, never trusted from a remote claim. Host mode does not
accept incoming input controllers, and a direct agent accepts only one native
controller connection at a time. Certificates, claimed device IDs, and names
must match the approved identity. Renames and identity changes require deliberate
re-pairing. The shared pairing secret remains an additional message credential;
possessing it alone cannot authorize a device or role.

Restart both sides after pairing. There is no plaintext fallback or automatic
trust-on-first-use. Missing identities fail startup with setup guidance. A fresh
server challenge binds every connection's signatures; sequence gaps, duplicates,
and frames from prior connections are rejected before input is applied.

Certificates expire after one year. To revoke a lost or changed remote identity:

```bash
mwbc security revoke --device-id REMOTE_DEVICE_ID
```

Restart MWBC immediately after revocation to close sessions and reload the TLS
trust store. To replace an expired identity, stop the device, preserve its old
certificate and key by moving them to a private backup (not leaving files at
their original paths), clear its `tls_certificate` and `tls_private_key` config fields,
then initialize a new certificate and explicitly revoke/re-approve it on peers.
Do not clone a device's private identity onto a different machine.

The `allow_insecure_loopback` setting exists only for isolated tests. It defaults
to false, cannot bind/connect beyond numeric loopback addresses, and does not
restore compatibility with the old session protocol. Leave it disabled on devices.

## Tray/Menu Bar Companion

Run the optional tray companion on the same machine as the MWBC host:

```bash
mwbc tray --smith-url http://localhost:3000/mwbc
```

The tray icon shows whether MWBC is offline, running, connected to devices, or locked to the host with `F12`. Its menu can open Smith Command Center, the MWBC dashboard, the layout editor, and the web controller. It can also start the host when MWBC is stopped, or stop/restart the running local daemon.

The tray is intentionally separate from the daemon. That means it can stay open and offer `Start Host` even when the main MWBC process is not running.

## Layout Editor

When the host starts with the dashboard enabled, it prints a URL like:

```text
Layout editor: http://127.0.0.1:45446/layout#token=...
Pairing secret: ...
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

## Local API for Smith Command Center

Smith Command Center should treat MWBC as a separate local daemon and talk to the dashboard API over localhost:

```text
MWBC_API_URL=http://127.0.0.1:45446
MWBC_API_TOKEN=<management token from the host's printed URL or api_token config field>
```

Unauthenticated read endpoints:

```text
GET /api/status
GET /api/layout
GET /api/capabilities
GET /api/service
GET /api/v1/status
GET /api/v1/service
```

The public service response intentionally omits secret-bearing controller and layout URLs. Send `X-MWBC-Token` with `GET /api/service` or `GET /api/v1/service` when Smith needs those private URLs.

Authenticated endpoints require the separate `api_token` in `X-MWBC-Token`.
Peer pairing secrets no longer authorize dashboard, browser input, or service
controls. Older configs receive a new independent management token on load;
update Smith's `MWBC_API_TOKEN` after upgrading.

```text
POST /api/layout
GET  /api/startup
GET  /api/logs?limit=100
POST /api/service/start
POST /api/service/stop
POST /api/service/restart
POST /api/startup/install
POST /api/startup/uninstall
POST /api/secret/regenerate
```

Example authenticated request:

```bash
curl -H "X-MWBC-Token: paste-secret-here" http://127.0.0.1:45446/api/logs
```

`POST /api/service/restart` and `POST /api/service/start` can accept a JSON body with `mode`, `backend`, `dashboard_host`, `dashboard_port`, and, for client mode, `host`, `port`, and `retry_seconds`. These endpoints are available only while the dashboard API is already running. If MWBC is stopped, Smith Command Center should launch the MWBC executable/process directly and then reconnect to the API.

`POST /api/secret/regenerate` rotates the peer `pairing_secret`, not the management
token. Update peer configurations before reconnecting; Smith's management token
remains unchanged. Certificate trust is separate from both secrets.

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
mwbc run --dashboard-host 127.0.0.1
```

The terminal prints a URL like:

```text
Web controller: http://127.0.0.1:45446/controller#token=...
```

For another device, publish this loopback service through a properly configured
HTTPS reverse proxy with trusted certificates and access controls, then open the
HTTPS controller URL with the management token fragment. Direct LAN HTTP binding
is rejected. Press `Start` to enter pointer lock. Browser input is a separate
management-authorized path, not an approved native TLS peer.

Notes:

- The URL fragment after `#token=` is the management token. Treat it like a password.
- Some browser or OS-reserved key combinations may not be capturable from a normal web page.
- Do not expose port `45446` directly to the LAN. Only expose the HTTPS proxy.

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

For a host machine where the tray icon should run at login and let you start/stop MWBC from the notification area:

```bash
mwbc startup install --mode tray --smith-url http://localhost:3000/mwbc --tray-start-backend pynput
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
mwbc run --dashboard-host 127.0.0.1
mwbc agent
mwbc controller
mwbc tray --smith-url http://localhost:3000/mwbc
mwbc startup install --mode client --host 192.168.1.10
mwbc startup install --mode host
mwbc startup install --mode tray --smith-url http://localhost:3000/mwbc --tray-start-backend pynput
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
- Use an HTTPS reverse proxy for remote browser controller access; port `45446` stays local.
- Keep the dashboard bound to `127.0.0.1` unless you intentionally want it reachable from the LAN.
- File drag/drop, auto-discovery, and per-monitor geometry are natural next additions.

## Source-checkout verification and recovery

The authoritative development checkout on the current workstation is
`/home/rulski/Projects/mouse-without-borders-clone` inside Ubuntu/WSL. The
source-only Windows folder at `C:\Users\rulski\Projects\mouse-without-borders-clone`
is an older copy, not the Git checkout. Update desktop installations from
committed GitHub revisions; do not assume that editing the WSL checkout updates
an existing Windows installation.

Input queue saturation now discards motion first. If ordered key/button
transitions still cannot fit, MWBC aborts remote control and locks to the host
until F12 resumes switching. Receivers release injected ordinary keys as well
as modifiers and mouse buttons during reset. Update both host and client for
the full recovery behavior.

Native connections now require mutual TLS and explicit device-role approval,
with session-bound sequence enforcement. Layout routing and clipboard
responsiveness remain the next stages. Native Windows/macOS two-device behavior
must still be validated after upgrading and pairing both devices.

The suite can run from this source checkout with the base package dependencies
installed, without native input hooks. Security tests generate temporary identities
and exercise actual TLS sockets:

```bash
pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests
```

Actual keyboard, pointer, clipboard, tray, and startup behavior still needs
testing in the desktop OS session. Pairing configuration is stored outside
this repository at `~/.mwbc/config.json`; keep it in an approved private
recovery location. GitHub contains committed code only. Preserve existing
uncommitted code and documentation changes in a separate source snapshot.
