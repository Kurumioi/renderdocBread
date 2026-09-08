import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "renderdoc_mcp_bridge"
    / "shader_editor_client.py"
)
SPEC = importlib.util.spec_from_file_location(
    "replaylab_shader_editor_client_test", MODULE_PATH
)
client_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(client_module)


LIVE_EDIT_ID = "a" * 32
AUTHORIZATION_ID = "b" * 32
WORKSPACE_ID = "c" * 32
EXPECTED_SHA256 = "d" * 64
WATCH_ID = "e" * 32
HOST_INSTANCE_ID = "f" * 32


class _Transport:
    def __init__(self, status, value):
        self.status = status
        self.payload = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, separators=(",", ":")).encode("utf-8")
        )
        self.calls = []

    def __call__(self, port, token, body, timeout):
        self.calls.append((port, token, body, timeout))
        return self.status, self.payload


class _HealthTransport:
    def __init__(self, pid=None, version="1.13.0", status=200,
                 protocol_version=1, host_instance_id=HOST_INSTANCE_ID):
        self.pid = os.getpid() if pid is None else pid
        self.version = version
        self.status = status
        self.protocol_version = protocol_version
        self.host_instance_id = host_instance_id
        self.calls = []

    def __call__(self, port, timeout):
        self.calls.append((port, timeout))
        return self.status, json.dumps(
            {
                "service": "ReplayLabInternal",
                "version": self.version,
                "hostStateProtocolVersion": self.protocol_version,
                "transport": "internal-http",
                "pid": self.pid,
                "hostInstanceId": self.host_instance_id,
                "capabilities": [
                    "project-service", "project-restore",
                    "shader-source-save", "shader-live-edit", "asset-index"],
            },
            separators=(",", ":"),
        ).encode("utf-8")


def _success(text="float4 MainPS() : SV_Target { return 1; }\n", **overrides):
    value = {
        "schema": client_module.SAVE_SCHEMA,
        "schemaVersion": 1,
        "liveEditId": LIVE_EDIT_ID,
        "authorizationId": AUTHORIZATION_ID,
        "workspaceId": WORKSPACE_ID,
        "watchId": WATCH_ID,
        "savedRawSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size": len(text.encode("utf-8")),
        "modifiedNs": 123456789,
        "changed": True,
        "compileScheduled": True,
    }
    value.update(overrides)
    return value


class ReplayLabShaderSaveClientTests(unittest.TestCase):
    def _runtime(self, root):
        runtime = Path(root) / "runtime"
        runtime.mkdir()
        token_path = runtime / "internal-host.token"
        token_path.write_text("T" * 48, encoding="ascii")
        (runtime / "internal-host.json").write_text(
            json.dumps(
                {
                    "schema": "replaylab-internal-host/v1",
                    "service": "ReplayLabInternal",
                    "version": "1.13.0",
                    "hostStateProtocolVersion": 1,
                    "host": "127.0.0.1",
                    "port": 49643,
                    "endpoint": "http://127.0.0.1:49643/replaylab/internal",
                    "pid": os.getpid(),
                    "hostInstanceId": HOST_INSTANCE_ID,
                    "heartbeatUnixMs": int(time.time() * 1000),
                    "tokenFile": str(token_path),
                }
            ),
            encoding="utf-8",
        )
        return runtime

    def _client(self, runtime, status, value):
        transport = _Transport(status, value)
        health = _HealthTransport()
        return (
            client_module.ReplayLabShaderSaveClient(
                str(runtime), startup_timeout_seconds=0.1,
                transport=transport, health_transport=health
            ),
            transport,
            health,
        )

    def test_module_parses_with_embedded_python_36_grammar(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertTrue(imported.isdisjoint(
            {"http", "http.client", "urllib", "urllib.request", "socket", "_socket"}
        ))
        self.assertIn("WinHttpOpen", source)
        self.assertIn('"127.0.0.1"', source)
        self.assertIn("WINHTTP_DISABLE_REDIRECTS", source)
        self.assertIn("DISABLE_AUTHENTICATION", source)
        self.assertIn("WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH", source)

    def test_save_uses_host_port_bearer_and_bounded_transport(self):
        text = "float4 MainPS() : SV_Target { return 1; }\n"
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, 200, _success(text))

            result = client.save(
                LIVE_EDIT_ID,
                AUTHORIZATION_ID,
                WORKSPACE_ID,
                EXPECTED_SHA256,
                text,
            )

            self.assertEqual(1, len(transport.calls))
            self.assertEqual([(49643, 2.0)], health.calls)
            port, token, body, timeout = transport.calls[0]
            self.assertEqual(49643, port)
            self.assertEqual("T" * 48, token)
            self.assertEqual(5.0, timeout)
            request = json.loads(body.decode("utf-8"))
            self.assertEqual(
                {
                    "liveEditId",
                    "authorizationId",
                    "workspaceId",
                    "expectedRawSha256",
                    "text",
                },
                set(request),
            )
            self.assertNotIn(("T" * 48).encode("ascii"), body)
            self.assertEqual(text, request["text"])
            self.assertEqual(_success(text)["savedRawSha256"], result["rawSha256"])
            self.assertTrue(result["compileScheduled"])

    def test_http_redirect_status_is_rejected_by_save_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, _health = self._client(
                runtime, 307, {"error": "redirects are forbidden"})

            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveError, "HTTP 307"
            ):
                client.save(
                    LIVE_EDIT_ID,
                    AUTHORIZATION_ID,
                    WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 2; }\n",
                )

            self.assertEqual(1, len(transport.calls))

    def test_conflict_response_has_a_distinct_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, _transport, _health = self._client(
                runtime, 409,
                {"error": "source changed on disk", "conflict": True})

            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveConflict,
                "source changed on disk",
            ):
                client.save(
                    LIVE_EDIT_ID,
                    AUTHORIZATION_ID,
                    WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 3; }\n",
                )

    def test_non_conflict_409_is_a_generic_save_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, _transport, _health = self._client(
                runtime, 409, {"error": "live edit is stopped"})

            with self.assertRaises(client_module.ReplayLabShaderSaveError) as caught:
                client.save(
                    LIVE_EDIT_ID,
                    AUTHORIZATION_ID,
                    WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 4; }\n",
                )
            self.assertNotIsInstance(
                caught.exception, client_module.ReplayLabShaderSaveConflict
            )

    def test_success_response_is_exact_and_identity_bound(self):
        text = "float4 MainPS() : SV_Target { return 5; }\n"
        invalid_values = [
            _success(text, extra="not allowed"),
            _success(text, schema="replaylab-shader-source-save/v2"),
            _success(text, liveEditId="f" * 32),
            _success(text, workspaceId="f" * 32),
            _success(text, watchId="not-an-id"),
            _success(text, savedRawSha256="not-a-hash"),
            _success(text, savedRawSha256="f" * 64),
            _success(text, changed=1),
            _success(text, changed=False),
            _success(text, compileScheduled=1),
            _success(text, size=True),
            _success(text, size=len(text.encode("utf-8")) + 1),
            _success(text, modifiedNs=0),
            _success(text, modifiedNs=0x8000000000000000),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            for value in invalid_values:
                with self.subTest(value=value):
                    client, _transport, _health = self._client(
                        runtime, 200, value)
                    with self.assertRaisesRegex(
                        client_module.ReplayLabShaderSaveError,
                        "invalid Shader save",
                    ):
                        client.save(
                            LIVE_EDIT_ID,
                            AUTHORIZATION_ID,
                            WORKSPACE_ID,
                            EXPECTED_SHA256,
                            text,
                        )

    def test_duplicate_or_non_object_json_response_is_rejected(self):
        invalid_payloads = [
            b'[{"schema":"replaylab-shader-source-save/v1"}]',
            b'{"schema":"one","schema":"two"}',
            b'{"size":NaN}',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    client, _transport, _health = self._client(
                        runtime, 200, payload)
                    with self.assertRaises(client_module.ReplayLabShaderSaveError):
                        client.save(
                            LIVE_EDIT_ID,
                            AUTHORIZATION_ID,
                            WORKSPACE_ID,
                            EXPECTED_SHA256,
                            "float4 MainPS() : SV_Target { return 6; }\n",
                        )

    def test_dead_or_future_host_state_never_reaches_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            state_path = runtime / "internal-host.json"
            for overrides in (
                {"pid": 2147483647},
                {"heartbeatUnixMs": int(time.time() * 1000) + 60000},
            ):
                with self.subTest(overrides=overrides):
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state.update(overrides)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    client, transport, health = self._client(
                        runtime, 200, _success())
                    with self.assertRaisesRegex(
                        client_module.ReplayLabShaderSaveError,
                        "internal Host is unavailable",
                    ):
                        client.save(
                            LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                            EXPECTED_SHA256,
                            "float4 MainPS() : SV_Target { return 7; }\n",
                        )
                    self.assertEqual([], health.calls)
                    self.assertEqual([], transport.calls)
                    if "pid" in overrides:
                        state["pid"] = os.getpid()
                        state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_incompatible_state_protocol_or_instance_never_reaches_health(self):
        invalid_overrides = (
            {"hostStateProtocolVersion": 2},
            {"hostStateProtocolVersion": 2.0},
            {"hostStateProtocolVersion": True},
            {"hostInstanceId": "not-an-instance"},
            {"hostInstanceId": "F" * 32},
        )
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            state_path = runtime / "internal-host.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))
            for overrides in invalid_overrides:
                with self.subTest(overrides=overrides):
                    state = dict(original)
                    state.update(overrides)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    client, transport, health = self._client(
                        runtime, 200, _success()
                    )
                    with self.assertRaisesRegex(
                        client_module.ReplayLabShaderSaveError,
                        "internal Host is unavailable",
                    ):
                        client.save(
                            LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                            EXPECTED_SHA256,
                            "float4 MainPS() : SV_Target { return 8; }\n",
                        )
                    self.assertEqual([], health.calls)
                    self.assertEqual([], transport.calls)

    def test_incompatible_state_version_never_reaches_health_or_bearer(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            state_path = runtime / "internal-host.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["version"] = "1.10.1"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            client, transport, health = self._client(
                runtime, 200, _success()
            )

            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveError,
                "internal Host is unavailable",
            ):
                client.save(
                    LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 8; }\n",
                )

            self.assertEqual([], health.calls)
            self.assertEqual([], transport.calls)

    def test_health_pid_mismatch_never_sends_bearer_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, 200, _success())
            health.pid = os.getpid() + 1
            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveError,
                "health identity is invalid",
            ):
                client.save(
                    LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 8; }\n",
                )
            self.assertEqual(1, len(health.calls))
            self.assertEqual([], transport.calls)

    def test_health_protocol_or_instance_mismatch_never_sends_bearer_request(self):
        mismatches = (
            ("protocol_version", 2),
            ("protocol_version", 2.0),
            ("protocol_version", True),
            ("host_instance_id", "0" * 32),
            ("host_instance_id", "not-an-instance"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            for attribute, value in mismatches:
                with self.subTest(attribute=attribute, value=value):
                    client, transport, health = self._client(
                        runtime, 200, _success()
                    )
                    setattr(health, attribute, value)
                    with self.assertRaisesRegex(
                        client_module.ReplayLabShaderSaveError,
                        "health identity is invalid",
                    ):
                        client.save(
                            LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                            EXPECTED_SHA256,
                            "float4 MainPS() : SV_Target { return 9; }\n",
                        )
                    self.assertEqual(1, len(health.calls))
                    self.assertEqual([], transport.calls)

    def test_incompatible_health_version_never_sends_bearer_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            client, transport, health = self._client(
                runtime, 200, _success()
            )
            health.version = "1.10.1"

            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveError,
                "health identity is invalid",
            ):
                client.save(
                    LIVE_EDIT_ID, AUTHORIZATION_ID, WORKSPACE_ID,
                    EXPECTED_SHA256,
                    "float4 MainPS() : SV_Target { return 9; }\n",
                )

            self.assertEqual(1, len(health.calls))
            self.assertEqual([], transport.calls)

    def test_unstarted_reserved_transport_slot_cannot_be_overwritten(self):
        reserved = threading.Thread(target=lambda: None)
        with client_module._ACTIVE_REQUEST_LOCK:
            previous = client_module._ACTIVE_REQUEST_THREAD
            client_module._ACTIVE_REQUEST_THREAD = reserved
        try:
            self.assertTrue(client_module.ReplayLabShaderSaveClient.request_pending())
            with self.assertRaisesRegex(
                client_module.ReplayLabShaderSaveError,
                "previous ReplayLab Host request is still pending",
            ):
                client_module._bounded_winhttp_request(
                    "GET", "/health", 49643, "", b"", 0.1)
            self.assertIs(
                reserved, client_module._ACTIVE_REQUEST_THREAD)
        finally:
            with client_module._ACTIVE_REQUEST_LOCK:
                client_module._ACTIVE_REQUEST_THREAD = previous
        if previous is None:
            self.assertFalse(
                client_module.ReplayLabShaderSaveClient.request_pending())

    @unittest.skipUnless(os.name == "nt", "WinHTTP is Windows-only")
    def test_real_winhttp_slow_peer_has_a_wall_clock_deadline(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def withhold_response():
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.recv(64 * 1024)
                    time.sleep(2.0)
            finally:
                listener.close()

        server = threading.Thread(target=withhold_response)
        server.daemon = True
        server.start()
        started = time.monotonic()
        with self.assertRaisesRegex(
            client_module.ReplayLabShaderSaveUncertain, "timed out"
        ):
            client_module._winhttp_post(
                port, "T" * 48, b"{}", 0.15)
        self.assertLess(time.monotonic() - started, 0.75)

    @unittest.skipUnless(os.name == "nt", "WinHTTP is Windows-only")
    def test_real_winhttp_negotiate_challenge_cannot_trigger_auto_auth(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        requests = []
        finished = threading.Event()

        def challenge_once():
            try:
                listener.settimeout(2.0)
                connection, _address = listener.accept()
                with connection:
                    request = connection.recv(64 * 1024)
                    requests.append(request)
                    connection.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\n"
                        b"WWW-Authenticate: Negotiate\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                listener.settimeout(0.6)
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    return
                with connection:
                    requests.append(connection.recv(64 * 1024))
            finally:
                listener.close()
                finished.set()

        server = threading.Thread(target=challenge_once)
        server.daemon = True
        server.start()
        status, body = client_module._winhttp_post(
            port, "T" * 48, b"{}", 1.5)
        self.assertEqual(401, status)
        self.assertEqual(b"", body)
        self.assertTrue(finished.wait(1.5))
        self.assertEqual(1, len(requests))
        self.assertIn(b"Authorization: Bearer " + b"T" * 48, requests[0])
        self.assertNotIn(b"Authorization: Negotiate", requests[0])


if __name__ == "__main__":
    unittest.main()
