from __future__ import annotations

import hmac
import html
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

WebInputHandler = Callable[[list[dict[str, Any]], dict[str, Any]], int]
LayoutProvider = Callable[[], dict[str, Any]]
LayoutUpdateHandler = Callable[[dict[str, Any]], dict[str, Any]]
ManagementHandler = Callable[[str, dict[str, Any]], dict[str, Any]]

MANAGEMENT_GET_ROUTES: dict[str, tuple[str, bool]] = {
    "/api/capabilities": ("capabilities", False),
    "/api/service": ("service.status", False),
    "/api/startup": ("startup.status", True),
    "/api/logs": ("logs", True),
}
MANAGEMENT_POST_ROUTES: dict[str, str] = {
    "/api/service/start": "service.start",
    "/api/service/stop": "service.stop",
    "/api/service/restart": "service.restart",
    "/api/startup/install": "startup.install",
    "/api/startup/uninstall": "startup.uninstall",
    "/api/secret/regenerate": "secret.regenerate",
}


class DashboardServer:
    def __init__(
        self,
        host: str,
        port: int,
        state_provider: Callable[[], dict],
        *,
        auth_tokens: list[str] | None = None,
        web_input_handler: WebInputHandler | None = None,
        layout_provider: LayoutProvider | None = None,
        layout_update_handler: LayoutUpdateHandler | None = None,
        management_handler: ManagementHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.state_provider = state_provider
        self.auth_tokens = auth_tokens or []
        self.web_input_handler = web_input_handler
        self.layout_provider = layout_provider
        self.layout_update_handler = layout_update_handler
        self.management_handler = management_handler
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        state_provider = self.state_provider
        auth_tokens = self.auth_tokens
        web_input_handler = self.web_input_handler
        layout_provider = self.layout_provider
        layout_update_handler = self.layout_update_handler
        management_handler = self.management_handler

        def current_layout() -> dict[str, Any]:
            if layout_provider is not None:
                return layout_provider()
            state = state_provider()
            return {
                "machine_name": state.get("machine_name"),
                "peers": state.get("peers", []),
            }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path in {"/api/status", "/status.json"}:
                    self._send_json(state_provider())
                    return
                if parsed.path == "/api/layout":
                    self._send_json(current_layout())
                    return
                if parsed.path in MANAGEMENT_GET_ROUTES:
                    self._handle_management_get(parsed)
                    return
                if parsed.path == "/controller":
                    self._send_html(render_web_controller(state_provider()))
                    return
                if parsed.path == "/layout":
                    self._send_html(render_layout_editor(current_layout()))
                    return
                if parsed.path == "/":
                    self._send_html(render_dashboard(state_provider()))
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/api/layout":
                    self._handle_layout_post(parsed)
                    return
                if parsed.path in MANAGEMENT_POST_ROUTES:
                    self._handle_management_post(parsed)
                    return
                if parsed.path != "/api/web-input":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if web_input_handler is None:
                    self._send_json({"ok": False, "error": "web input is disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return

                payload = self._read_json_body()
                if payload is None:
                    self._send_json({"ok": False, "error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                    return

                if not _token_matches(self._auth_token(payload, parsed.query), auth_tokens):
                    self._send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return

                events = payload.get("events", [])
                if isinstance(payload.get("event"), dict):
                    events = [payload["event"]]
                if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
                    self._send_json({"ok": False, "error": "events must be objects"}, HTTPStatus.BAD_REQUEST)
                    return

                client_info = {
                    "id": str(payload.get("client_id") or self.client_address[0]),
                    "peer": self.client_address[0],
                    "user_agent": self.headers.get("User-Agent", ""),
                    "screen_width": payload.get("screen_width"),
                    "screen_height": payload.get("screen_height"),
                    "last_seen": time.time(),
                }
                try:
                    accepted = web_input_handler(events, client_info)
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"ok": True, "accepted": accepted})

            def _handle_layout_post(self, parsed) -> None:
                if layout_update_handler is None:
                    self._send_json({"ok": False, "error": "layout editing is disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return

                payload = self._read_json_body()
                if payload is None:
                    self._send_json({"ok": False, "error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                    return

                if not _token_matches(self._auth_token(payload, parsed.query), auth_tokens):
                    self._send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return

                try:
                    result = dict(layout_update_handler(payload))
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                result.setdefault("ok", True)
                self._send_json(result)

            def _handle_management_get(self, parsed) -> None:
                if management_handler is None:
                    self._send_json({"ok": False, "error": "management API is disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return

                action, requires_auth = MANAGEMENT_GET_ROUTES[parsed.path]
                if requires_auth and not _token_matches(self._auth_token({}, parsed.query), auth_tokens):
                    self._send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return

                try:
                    result = dict(management_handler(action, self._query_payload(parsed.query)))
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                result.setdefault("ok", True)
                self._send_json(result)

            def _handle_management_post(self, parsed) -> None:
                if management_handler is None:
                    self._send_json({"ok": False, "error": "management API is disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return

                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    self._send_json({"ok": False, "error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
                    return

                if not _token_matches(self._auth_token(payload, parsed.query), auth_tokens):
                    self._send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return

                try:
                    result = dict(management_handler(MANAGEMENT_POST_ROUTES[parsed.path], payload))
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                result.setdefault("ok", True)
                self._send_json(result)

            def log_message(self, format: str, *args: object) -> None:
                return None

            def _auth_token(self, payload: dict[str, Any], query: str) -> str:
                token = self.headers.get("X-MWBC-Token") or str(payload.get("token", ""))
                query_token = parse_qs(query).get("token", [""])[0]
                return token or query_token

            def _query_payload(self, query: str) -> dict[str, Any]:
                return {key: values[-1] for key, values in parse_qs(query).items() if values}

            def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length <= 0:
                    return {} if allow_empty else None
                if length > 262_144:
                    return None
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                return data if isinstance(data, dict) else None

            def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, body_text: str) -> None:
                body = body_text.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mwbc-dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _token_matches(token: str, candidates: list[str]) -> bool:
    if not token or not candidates:
        return False
    return any(hmac.compare_digest(token, candidate) for candidate in candidates)


def _safe(value: object) -> str:
    return html.escape("" if value is None else str(value))


def render_dashboard(state: dict) -> str:
    peers = state.get("peers", [])
    incoming = state.get("incoming_clients", {})
    peer_rows = "\n".join(_render_peer(peer) for peer in peers) or "<tr><td colspan='6'>No configured peers</td></tr>"
    incoming_rows = "\n".join(_render_incoming(name, item) for name, item in incoming.items())
    if not incoming_rows:
        incoming_rows = "<tr><td colspan='4'>No incoming controller sessions</td></tr>"
    last_error = state.get("last_error")
    error_block = f"<div class='error'>{_safe(last_error)}</div>" if last_error else ""
    active_peer = state.get("active_peer") or "local"
    screen = f"{state.get('local_screen_width') or '?'} x {state.get('local_screen_height') or '?'}"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="3">
  <title>MWBC Dashboard</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f4;
      --panel: #ffffff;
      --ink: #20231f;
      --muted: #667063;
      --line: #d9ded4;
      --accent: #147d64;
      --warn: #a23b3b;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #151815;
        --panel: #202620;
        --ink: #edf1ea;
        --muted: #a8b2a4;
        --line: #343d34;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 28px; }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 18px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }}
    .meta {{ color: var(--muted); margin-top: 4px; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 4px 10px;
      background: var(--panel);
      color: var(--muted);
      white-space: nowrap;
      text-decoration: none;
    }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 88px;
    }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ font-size: 24px; margin-top: 6px; overflow-wrap: anywhere; }}
    section {{ margin-top: 20px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{ text-align: left; padding: 11px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    tr:last-child td {{ border-bottom: 0; }}
    .ok {{ color: var(--accent); font-weight: 650; }}
    .off {{ color: var(--muted); }}
    .error {{ border-left: 4px solid var(--warn); background: var(--panel); padding: 12px; margin-bottom: 16px; border-radius: 6px; }}
    @media (max-width: 760px) {{
      main {{ padding: 18px; }}
      header {{ display: block; }}
      .pill {{ margin-top: 12px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{_safe(state.get("machine_name"))}</h1>
        <div class="meta">Mouse Without Borders Clone</div>
      </div>
      <div class="actions">
        <a class="pill" href="/controller">Controller</a>
        <a class="pill" href="/layout">Layout</a>
        <div class="pill">Active: {_safe(active_peer)}</div>
      </div>
    </header>
    {error_block}
    <div class="grid">
      <div class="metric"><div class="label">Mode</div><div class="value">{_safe(state.get("mode"))}</div></div>
      <div class="metric"><div class="label">Backend</div><div class="value">{_safe(state.get("backend"))}</div></div>
      <div class="metric"><div class="label">Screen</div><div class="value">{_safe(screen)}</div></div>
      <div class="metric"><div class="label">Uptime</div><div class="value">{_safe(state.get("uptime_seconds"))}s</div></div>
    </div>
    <section>
      <h2>Configured Peers</h2>
      <table>
        <thead><tr><th>Name</th><th>Address</th><th>Edge</th><th>Status</th><th>Screen</th><th>Last error</th></tr></thead>
        <tbody>{peer_rows}</tbody>
      </table>
    </section>
    <section>
      <h2>Incoming Sessions</h2>
      <table>
        <thead><tr><th>Client</th><th>Address</th><th>Status</th><th>Screen</th></tr></thead>
        <tbody>{incoming_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def render_web_controller(state: dict) -> str:
    target_name = _safe(state.get("machine_name") or "target")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MWBC Controller</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #101411;
      --panel: #f7f8f4;
      --ink: #f4f7f1;
      --muted: #aeb8aa;
      --line: rgba(255,255,255,.18);
      --accent: #2ea987;
      --danger: #d56a6a;
      --surface: #172019;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #eef1ea;
        --panel: #ffffff;
        --ink: #1c221e;
        --muted: #657066;
        --line: rgba(28,34,30,.16);
        --surface: #ffffff;
      }}
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
      overflow: hidden;
      touch-action: none;
      user-select: none;
    }}
    main {{
      min-height: 100%;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 14px;
      padding: 18px;
    }}
    header, footer {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 44px;
    }}
    h1 {{ margin: 0; font-size: 18px; letter-spacing: 0; }}
    .status {{ color: var(--muted); overflow-wrap: anywhere; }}
    .pad {{
      position: relative;
      display: grid;
      place-items: center;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(90deg, transparent 31px, var(--line) 32px, transparent 33px),
        linear-gradient(0deg, transparent 31px, var(--line) 32px, transparent 33px),
        var(--surface);
      background-size: 64px 64px;
      cursor: crosshair;
      outline: none;
    }}
    .reticle {{
      width: min(240px, 48vw);
      aspect-ratio: 1;
      border: 1px solid var(--line);
      border-radius: 50%;
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: 14px;
      text-align: center;
    }}
    .reticle::before, .reticle::after {{
      content: "";
      position: absolute;
      background: var(--line);
    }}
    .reticle::before {{ width: min(320px, 62vw); height: 1px; }}
    .reticle::after {{ width: 1px; height: min(320px, 62vw); }}
    button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 42px;
      padding: 0 18px;
      font: inherit;
      color: var(--ink);
      background: var(--surface);
      cursor: pointer;
    }}
    button.primary {{ background: var(--accent); color: #07110d; border-color: transparent; font-weight: 700; }}
    button.danger {{ color: var(--danger); }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--muted);
      min-width: 90px;
      text-align: center;
    }}
    @media (max-width: 720px) {{
      main {{ padding: 12px; }}
      header, footer {{ align-items: stretch; flex-direction: column; }}
      .toolbar {{ width: 100%; }}
      button, .metric {{ flex: 1; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{target_name}</h1>
        <div id="status" class="status">Idle</div>
      </div>
      <div class="toolbar">
        <button id="start" class="primary" type="button">Start</button>
        <button id="stop" class="danger" type="button">Stop</button>
      </div>
    </header>
    <section id="pad" class="pad" tabindex="0" aria-label="Remote control surface">
      <div class="reticle"><span id="pad-label">Ready</span></div>
    </section>
    <footer>
      <div class="toolbar">
        <div class="metric"><span id="sent">0</span> sent</div>
        <div class="metric"><span id="queued">0</span> queued</div>
        <div class="metric" id="lock-state">unlocked</div>
      </div>
      <a class="status" href="/">Dashboard</a>
    </footer>
  </main>
  <script>
    const pad = document.getElementById("pad");
    const statusEl = document.getElementById("status");
    const sentEl = document.getElementById("sent");
    const queuedEl = document.getElementById("queued");
    const lockStateEl = document.getElementById("lock-state");
    const padLabel = document.getElementById("pad-label");
    const tokenParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const queryParams = new URLSearchParams(window.location.search);
    const token = tokenParams.get("token") || queryParams.get("token") || "";
    const clientIdKey = "mwbc-web-controller-id";
    let clientId = localStorage.getItem(clientIdKey);
    if (!clientId) {{
      clientId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" + Math.random();
      localStorage.setItem(clientIdKey, clientId);
    }}

    const specialKeys = {{
      "Alt": "alt",
      "AltGraph": "alt_gr",
      "Backspace": "backspace",
      "CapsLock": "caps_lock",
      "Control": "ctrl",
      "Delete": "delete",
      "End": "end",
      "Enter": "enter",
      "Escape": "esc",
      "Home": "home",
      "Insert": "insert",
      "Meta": "cmd",
      "PageDown": "page_down",
      "PageUp": "page_up",
      "Shift": "shift",
      "Tab": "tab",
      "ArrowDown": "down",
      "ArrowLeft": "left",
      "ArrowRight": "right",
      "ArrowUp": "up",
      " ": "space"
    }};
    const buttonNames = ["left", "middle", "right", "button3", "button4"];
    let queue = [];
    let sent = 0;
    let flushing = false;

    function locked() {{
      return document.pointerLockElement === pad;
    }}

    function setStatus(value) {{
      statusEl.textContent = value;
    }}

    function setLockUi() {{
      const isLocked = locked();
      lockStateEl.textContent = isLocked ? "locked" : "unlocked";
      padLabel.textContent = isLocked ? "Active" : "Ready";
      setStatus(token ? (isLocked ? "Active" : "Idle") : "Missing token");
    }}

    function keyToWire(event) {{
      if (event.key.length === 1 && event.key !== " ") {{
        return {{ kind: "char", value: event.key }};
      }}
      return {{ kind: "special", value: specialKeys[event.key] || event.key.toLowerCase() }};
    }}

    function normalizeWheel(value) {{
      if (value === 0) return 0;
      const scaled = Math.round(Math.abs(value) / 80) || 1;
      return value > 0 ? -scaled : scaled;
    }}

    function enqueue(event) {{
      if (!token) {{
        setStatus("Missing token");
        return;
      }}
      queue.push(event);
      queuedEl.textContent = String(queue.length);
    }}

    async function flush() {{
      if (flushing || queue.length === 0) return;
      flushing = true;
      const events = queue.splice(0, 96);
      queuedEl.textContent = String(queue.length);
      try {{
        const response = await fetch("/api/web-input", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "X-MWBC-Token": token
          }},
          body: JSON.stringify({{
            client_id: clientId,
            screen_width: screen.width,
            screen_height: screen.height,
            events
          }})
        }});
        const result = await response.json();
        if (!response.ok || !result.ok) {{
          throw new Error(result.error || "request failed");
        }}
        sent += result.accepted || events.length;
        sentEl.textContent = String(sent);
        setStatus(locked() ? "Active" : "Idle");
      }} catch (error) {{
        queue = events.concat(queue).slice(0, 300);
        queuedEl.textContent = String(queue.length);
        setStatus(error.message || "Send failed");
      }} finally {{
        flushing = false;
      }}
    }}

    async function start() {{
      pad.focus();
      try {{
        await pad.requestPointerLock({{ unadjustedMovement: true }});
      }} catch (_) {{
        try {{ await pad.requestPointerLock(); }} catch (error) {{ setStatus(error.message || "Lock failed"); }}
      }}
      if (navigator.keyboard && navigator.keyboard.lock) {{
        try {{ await navigator.keyboard.lock(); }} catch (_) {{}}
      }}
      setLockUi();
    }}

    function stop() {{
      if (document.exitPointerLock) document.exitPointerLock();
      if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
      setLockUi();
    }}

    document.getElementById("start").addEventListener("click", start);
    document.getElementById("stop").addEventListener("click", stop);
    pad.addEventListener("click", start);
    document.addEventListener("pointerlockchange", setLockUi);

    document.addEventListener("mousemove", (event) => {{
      if (!locked()) return;
      const dx = Math.trunc(event.movementX || 0);
      const dy = Math.trunc(event.movementY || 0);
      if (dx || dy) enqueue({{ action: "move_relative", dx, dy }});
    }}, {{ passive: true }});

    document.addEventListener("mousedown", (event) => {{
      if (!locked()) return;
      event.preventDefault();
      enqueue({{ action: "click", button: buttonNames[event.button] || "left", pressed: true }});
    }});

    document.addEventListener("mouseup", (event) => {{
      if (!locked()) return;
      event.preventDefault();
      enqueue({{ action: "click", button: buttonNames[event.button] || "left", pressed: false }});
    }});

    document.addEventListener("wheel", (event) => {{
      if (!locked()) return;
      event.preventDefault();
      enqueue({{ action: "scroll", dx: normalizeWheel(event.deltaX), dy: normalizeWheel(event.deltaY) }});
    }}, {{ passive: false }});

    document.addEventListener("keydown", (event) => {{
      if (!locked() || event.repeat) return;
      event.preventDefault();
      enqueue({{ action: "key_press", key: keyToWire(event) }});
    }});

    document.addEventListener("keyup", (event) => {{
      if (!locked()) return;
      event.preventDefault();
      enqueue({{ action: "key_release", key: keyToWire(event) }});
    }});

    setInterval(flush, 16);
    window.addEventListener("beforeunload", flush);
    setLockUi();
  </script>
</body>
</html>"""


def render_layout_editor(layout: dict) -> str:
    layout_json = json.dumps(layout).replace("</", "<\\/")
    host_name = _safe(layout.get("machine_name") or "Host")
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MWBC Layout</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f4f6f1;
      --panel: #ffffff;
      --ink: #1d241f;
      --muted: #667163;
      --line: #d7ddd1;
      --accent: #176f5a;
      --accent-ink: #f6fffb;
      --blue: #335d8f;
      --amber: #9a6718;
      --surface: #eef2ea;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111511;
        --panel: #1e251f;
        --ink: #eef3ea;
        --muted: #a8b3a4;
        --line: #354035;
        --surface: #171d18;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
    }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 18px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    .meta { color: var(--muted); margin-top: 4px; overflow-wrap: anywhere; }
    .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    a, button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 12px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
      text-decoration: none;
      cursor: pointer;
    }
    button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; font-weight: 700; }
    .status {
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 12px;
      color: var(--muted);
      background: var(--panel);
      overflow-wrap: anywhere;
    }
    .stage {
      display: grid;
      grid-template-columns: minmax(170px, 1fr) minmax(250px, 1.15fr) minmax(170px, 1fr);
      grid-template-rows: minmax(140px, auto) minmax(190px, auto) minmax(140px, auto);
      gap: 12px;
      align-items: stretch;
    }
    .zone, .host-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      min-height: 140px;
      padding: 12px;
    }
    .zone[data-edge="top"] { grid-column: 2; grid-row: 1; }
    .zone[data-edge="left"] { grid-column: 1; grid-row: 2; }
    .host-card { grid-column: 2; grid-row: 2; display: grid; place-items: center; text-align: center; }
    .zone[data-edge="right"] { grid-column: 3; grid-row: 2; }
    .zone[data-edge="bottom"] { grid-column: 2; grid-row: 3; }
    .zone.over { outline: 3px solid color-mix(in srgb, var(--accent), transparent 55%); }
    .zone-title {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 10px;
    }
    .host-name { font-size: 24px; font-weight: 750; overflow-wrap: anywhere; }
    .host-badge {
      display: inline-flex;
      margin-top: 8px;
      min-height: 28px;
      align-items: center;
      border-radius: 8px;
      padding: 3px 9px;
      color: var(--accent);
      background: var(--surface);
      border: 1px solid var(--line);
    }
    .device-list { display: grid; gap: 8px; min-height: 72px; }
    .device {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      min-height: 70px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--blue);
      border-radius: 8px;
      padding: 10px;
      background: var(--surface);
      cursor: grab;
    }
    .device:active { cursor: grabbing; }
    .device.connected { border-left-color: var(--accent); }
    .device.outbound { border-left-color: var(--amber); }
    .device-name { font-weight: 750; overflow-wrap: anywhere; }
    .device-detail { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .device-controls {
      grid-column: 1 / -1;
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }
    .feature-row {
      min-height: 34px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    input[type="checkbox"] { width: 18px; height: 18px; margin: 0; }
    input[type="number"] {
      width: 86px;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 4px 8px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }
    select {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 4px 8px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      min-height: 70px;
      display: grid;
      place-items: center;
      padding: 12px;
      text-align: center;
    }
    @media (max-width: 760px) {
      main { padding: 18px; }
      header { display: block; }
      .toolbar { justify-content: flex-start; margin-top: 12px; }
      .stage { display: grid; grid-template-columns: 1fr; grid-template-rows: auto; }
      .zone[data-edge], .host-card { grid-column: 1; grid-row: auto; }
      .device { grid-template-columns: 1fr; }
      select { width: 100%; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Device Layout</h1>
        <div class="meta">Host: __HOST__</div>
      </div>
      <div class="toolbar">
        <a href="/">Dashboard</a>
        <button id="save" class="primary" type="button">Save</button>
        <div id="status" class="status">Ready</div>
      </div>
    </header>
    <section class="stage" aria-label="Device layout">
      <section class="zone" data-edge="top">
        <div class="zone-title">Top</div>
        <div class="device-list" data-edge="top"></div>
      </section>
      <section class="zone" data-edge="left">
        <div class="zone-title">Left</div>
        <div class="device-list" data-edge="left"></div>
      </section>
      <section class="host-card">
        <div>
          <div class="host-name">__HOST__</div>
          <div class="host-badge">Host</div>
        </div>
      </section>
      <section class="zone" data-edge="right">
        <div class="zone-title">Right</div>
        <div class="device-list" data-edge="right"></div>
      </section>
      <section class="zone" data-edge="bottom">
        <div class="zone-title">Bottom</div>
        <div class="device-list" data-edge="bottom"></div>
      </section>
    </section>
  </main>
  <script>
    const initialLayout = __LAYOUT__;
    const edgeLabels = { left: "Left", top: "Top", right: "Right", bottom: "Bottom" };
    const validEdges = Object.keys(edgeLabels);
    const tokenParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const queryParams = new URLSearchParams(window.location.search);
    const token = tokenParams.get("token") || queryParams.get("token") || "";
    const statusEl = document.getElementById("status");
    const saveButton = document.getElementById("save");
    let draggedName = "";
    let peers = normalizePeers(initialLayout.peers);

    function normalizePeers(items) {
      return (Array.isArray(items) ? items : [])
        .map((peer) => ({
          name: String(peer.name || ""),
          host: String(peer.host || ""),
          edge: validEdges.includes(peer.edge) ? peer.edge : "right",
          connected: Boolean(peer.connected),
          keep_awake: Boolean(peer.keep_awake),
          keep_awake_interval_seconds: normalizeInterval(peer.keep_awake_interval_seconds)
        }))
        .filter((peer) => peer.name);
    }

    function normalizeInterval(value) {
      const interval = Number(value);
      if (!Number.isFinite(interval)) return 45;
      return Math.min(3600, Math.max(5, Math.round(interval)));
    }

    function setStatus(value) {
      statusEl.textContent = value;
    }

    function detailFor(peer) {
      if (peer.connected) return "connected";
      if (peer.host) return peer.host;
      return "outbound client";
    }

    function createDevice(peer) {
      const card = document.createElement("article");
      card.className = "device" + (peer.connected ? " connected" : "") + (!peer.host ? " outbound" : "");
      card.draggable = true;
      card.dataset.name = peer.name;

      const text = document.createElement("div");
      const name = document.createElement("div");
      name.className = "device-name";
      name.textContent = peer.name;
      const detail = document.createElement("div");
      detail.className = "device-detail";
      detail.textContent = detailFor(peer);
      text.append(name, detail);

      const select = document.createElement("select");
      select.setAttribute("aria-label", "Edge for " + peer.name);
      for (const edge of validEdges) {
        const option = document.createElement("option");
        option.value = edge;
        option.textContent = edgeLabels[edge];
        option.selected = peer.edge === edge;
        select.append(option);
      }
      select.addEventListener("change", () => setEdge(peer.name, select.value));

      const controls = document.createElement("div");
      controls.className = "device-controls";

      const keepAwakeLabel = document.createElement("label");
      keepAwakeLabel.className = "feature-row";
      const keepAwake = document.createElement("input");
      keepAwake.type = "checkbox";
      keepAwake.checked = peer.keep_awake;
      keepAwake.addEventListener("change", () => updateFeature(peer.name, {
        keep_awake: keepAwake.checked
      }));
      keepAwakeLabel.append(keepAwake, document.createTextNode("Keep awake"));

      const intervalLabel = document.createElement("label");
      intervalLabel.className = "feature-row";
      const interval = document.createElement("input");
      interval.type = "number";
      interval.min = "5";
      interval.max = "3600";
      interval.step = "5";
      interval.value = String(peer.keep_awake_interval_seconds);
      interval.addEventListener("change", () => updateFeature(peer.name, {
        keep_awake_interval_seconds: normalizeInterval(interval.value)
      }));
      intervalLabel.append(document.createTextNode("Every"), interval, document.createTextNode("sec"));

      controls.append(keepAwakeLabel, intervalLabel);

      card.append(text, select, controls);
      card.addEventListener("dragstart", (event) => {
        draggedName = peer.name;
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", peer.name);
      });
      return card;
    }

    function render() {
      document.querySelectorAll(".device-list").forEach((list) => list.replaceChildren());
      for (const peer of peers) {
        const list = document.querySelector('.device-list[data-edge="' + peer.edge + '"]');
        if (list) list.append(createDevice(peer));
      }
      for (const list of document.querySelectorAll(".device-list")) {
        if (list.children.length === 0) {
          const empty = document.createElement("div");
          empty.className = "empty";
          empty.textContent = "No devices";
          list.append(empty);
        }
      }
    }

    function setEdge(name, edge) {
      if (!validEdges.includes(edge)) return;
      const peer = peers.find((item) => item.name === name);
      if (!peer || peer.edge === edge) return;
      peer.edge = edge;
      render();
      saveLayout();
    }

    function updateFeature(name, updates) {
      const peer = peers.find((item) => item.name === name);
      if (!peer) return;
      Object.assign(peer, updates);
      peer.keep_awake_interval_seconds = normalizeInterval(peer.keep_awake_interval_seconds);
      render();
      saveLayout();
    }

    async function saveLayout() {
      if (!token) {
        setStatus("Missing token");
        return;
      }
      setStatus("Saving");
      try {
        const response = await fetch("/api/layout", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-MWBC-Token": token
          },
          body: JSON.stringify({
            peers: peers.map((peer) => ({
              name: peer.name,
              edge: peer.edge,
              keep_awake: peer.keep_awake,
              keep_awake_interval_seconds: peer.keep_awake_interval_seconds
            }))
          })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
          throw new Error(result.error || "save failed");
        }
        peers = normalizePeers(result.peers || peers);
        render();
        setStatus("Saved");
      } catch (error) {
        setStatus(error.message || "Save failed");
      }
    }

    document.querySelectorAll(".zone").forEach((zone) => {
      zone.addEventListener("dragover", (event) => {
        event.preventDefault();
        zone.classList.add("over");
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("over"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("over");
        const name = event.dataTransfer.getData("text/plain") || draggedName;
        setEdge(name, zone.dataset.edge);
      });
    });

    saveButton.addEventListener("click", saveLayout);
    if (!token) setStatus("Missing token");
    render();
  </script>
</body>
</html>"""
    return template.replace("__HOST__", host_name).replace("__LAYOUT__", layout_json)


def _render_peer(peer: dict) -> str:
    status = "<span class='ok'>connected</span>" if peer.get("connected") else "<span class='off'>offline</span>"
    screen = f"{peer.get('screen_width') or '?'} x {peer.get('screen_height') or '?'}"
    address = f"{peer.get('host')}:{peer.get('port')}" if peer.get("host") else "outbound client"
    return (
        "<tr>"
        f"<td>{_safe(peer.get('name'))}</td>"
        f"<td>{_safe(address)}</td>"
        f"<td>{_safe(peer.get('edge'))}</td>"
        f"<td>{status}</td>"
        f"<td>{_safe(screen)}</td>"
        f"<td>{_safe(peer.get('error'))}</td>"
        "</tr>"
    )


def _render_incoming(name: str, item: dict) -> str:
    status = "<span class='ok'>active</span>" if item.get("active") else "<span class='off'>connected</span>"
    screen = f"{item.get('screen_width') or '?'} x {item.get('screen_height') or '?'}"
    return (
        "<tr>"
        f"<td>{_safe(item.get('machine_name') or name)}</td>"
        f"<td>{_safe(item.get('peer'))}</td>"
        f"<td>{status}</td>"
        f"<td>{_safe(screen)}</td>"
        "</tr>"
    )
