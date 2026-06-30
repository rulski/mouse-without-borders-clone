from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from mwbc.dashboard import DashboardServer
from mwbc.input_backend import NullBackend, apply_input_event


class DashboardTests(unittest.TestCase):
    def test_web_input_requires_token(self) -> None:
        backend = NullBackend()
        server = self._server(backend)
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post(server, {"events": [{"action": "move_relative", "dx": 3, "dy": 4}]}, token="bad")
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(backend.current_position(), (960, 540))
        finally:
            server.stop()

    def test_web_input_applies_relative_motion(self) -> None:
        backend = NullBackend()
        server = self._server(backend)
        try:
            response = self._post(
                server,
                {"events": [{"action": "move_relative", "dx": 3, "dy": 4}]},
                token="secret",
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["accepted"], 1)
            self.assertEqual(backend.current_position(), (963, 544))
        finally:
            server.stop()

    def test_controller_page_renders(self) -> None:
        backend = NullBackend()
        server = self._server(backend)
        try:
            body = urllib.request.urlopen(f"{self._base_url(server)}/controller", timeout=3).read().decode("utf-8")
            self.assertIn("MWBC Controller", body)
            self.assertIn("/api/web-input", body)
        finally:
            server.stop()

    def test_layout_page_renders(self) -> None:
        server = self._layout_server()
        try:
            body = urllib.request.urlopen(f"{self._base_url(server)}/layout", timeout=3).read().decode("utf-8")
            self.assertIn("Device Layout", body)
            self.assertIn("/api/layout", body)
            self.assertIn("MACBOOK", body)
            self.assertIn("Keep awake", body)
        finally:
            server.stop()

    def test_layout_api_requires_token(self) -> None:
        server = self._layout_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post(server, {"peers": [{"name": "MACBOOK", "edge": "top"}]}, token="bad", path="/api/layout")
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.stop()

    def test_layout_api_updates_edges(self) -> None:
        server = self._layout_server()
        try:
            response = self._post(
                server,
                {"peers": [{"name": "MACBOOK", "edge": "top"}]},
                token="secret",
                path="/api/layout",
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["peers"][0]["edge"], "top")
        finally:
            server.stop()

    def test_management_service_status_is_public(self) -> None:
        server = self._management_server()
        try:
            response = self._get(server, "/api/service")
            self.assertTrue(response["ok"])
            self.assertEqual(response["action"], "service.status")
        finally:
            server.stop()

    def test_management_logs_requires_token(self) -> None:
        server = self._management_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._get(server, "/api/logs", token="bad")
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.stop()

    def test_management_post_requires_token(self) -> None:
        server = self._management_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post(server, {}, token="bad", path="/api/service/stop")
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.stop()

    def test_management_post_allows_empty_body(self) -> None:
        server = self._management_server()
        try:
            request = urllib.request.Request(
                f"{self._base_url(server)}/api/service/stop",
                data=b"",
                headers={"X-MWBC-Token": "secret"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "service.stop")
        finally:
            server.stop()

    def _server(self, backend: NullBackend) -> DashboardServer:
        def handle(events: list[dict], _client_info: dict) -> int:
            for event in events:
                apply_input_event(backend, event)
            return len(events)

        server = DashboardServer(
            "127.0.0.1",
            0,
            lambda: {"machine_name": "test"},
            auth_tokens=["secret"],
            web_input_handler=handle,
        )
        server.start()
        return server

    def _layout_server(self) -> DashboardServer:
        layout = {"machine_name": "HOST", "peers": [{"name": "MACBOOK", "edge": "left", "host": ""}]}

        def update(payload: dict) -> dict:
            layout["peers"][0]["edge"] = payload["peers"][0]["edge"]
            return layout

        server = DashboardServer(
            "127.0.0.1",
            0,
            lambda: {"machine_name": "HOST"},
            auth_tokens=["secret"],
            layout_provider=lambda: layout,
            layout_update_handler=update,
        )
        server.start()
        return server

    def _management_server(self) -> DashboardServer:
        def handle(action: str, payload: dict) -> dict:
            return {"action": action, "payload": payload}

        server = DashboardServer(
            "127.0.0.1",
            0,
            lambda: {"machine_name": "HOST"},
            auth_tokens=["secret"],
            management_handler=handle,
        )
        server.start()
        return server

    def _base_url(self, server: DashboardServer) -> str:
        assert server._httpd is not None
        host, port = server._httpd.server_address
        return f"http://{host}:{port}"

    def _post(self, server: DashboardServer, payload: dict, *, token: str, path: str = "/api/web-input") -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url(server)}{path}",
            data=body,
            headers={"Content-Type": "application/json", "X-MWBC-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, server: DashboardServer, path: str, *, token: str | None = None) -> dict:
        headers = {}
        if token is not None:
            headers["X-MWBC-Token"] = token
        request = urllib.request.Request(f"{self._base_url(server)}{path}", headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
