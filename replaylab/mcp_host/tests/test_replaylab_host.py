import asyncio
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from src import internal_tool_service
from src import replaylab_host
from src import replaylab_tools
from src.shader_workspace import (
    ToolConfigurationError,
    WorkspaceWatchConflictError,
    WorkspaceWatchError,
)
from src.shader_workspace.adapters import d3dcompiler_adapter
from src.shader_workspace.adapters import dxil_assembler_adapter
from src.shader_workspace.toolchain_config import generate_official_toolchain_configs


def _wait_restore_job(
    registry: internal_tool_service.ProjectRestoreJobRegistry,
    job_id: str,
    timeout: float = 3.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = registry.get(job_id)
        if value is not None and value["status"] in ("completed", "failed"):
            return value
        time.sleep(0.005)
    raise AssertionError("project restore job did not finish")


class _ChunkedRequest:
    def __init__(self, body: bytes, *, content_length: str | None = None):
        self._body = body
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length

    async def stream(self):
        midpoint = len(self._body) // 2
        yield self._body[:midpoint]
        yield self._body[midpoint:]


class ReplayLabHostTests(unittest.TestCase):
    @staticmethod
    def _discovered(**overrides):
        value = {
            "dxc": None,
            "fxc": None,
            "d3dcompiler": None,
            "spirv_cross": None,
            "glslang": None,
            "spirv_dis": None,
            "spirv_as": None,
            "spirv_val": None,
            "hlsl_decompiler": None,
            "hlsl_decompiler_dependency": None,
            "dxil_spirv": None,
            "renodx_decompiler": None,
        }
        value.update(overrides)
        return value

    @staticmethod
    def _publish_host_identity(
        root: Path,
        *,
        port: int = 49643,
        pid: int | None = None,
        instance_id: str = "a" * 32,
        protocol_version: int = 2,
        version: str = "1.10.2",
    ) -> dict:
        process_id = os.getpid() if pid is None else pid
        replaylab_host._atomic_json(
            root / "host.json",
            {
                "schema": "replaylab-host/v1",
                "service": "ReplayLab",
                "version": version,
                "hostStateProtocolVersion": protocol_version,
                "pid": process_id,
                "hostInstanceId": instance_id,
                "host": "127.0.0.1",
                "port": port,
                "endpoint": "http://127.0.0.1:%d/mcp" % port,
                "heartbeatUnixMs": int(time.time() * 1000),
            },
        )
        return {
            "service": "ReplayLab",
            "version": version,
            "hostStateProtocolVersion": protocol_version,
            "transport": "streamable-http",
            "pid": process_id,
            "hostInstanceId": instance_id,
        }

    def test_background_execution_policy_is_noop_off_windows(self):
        report = replaylab_host.disable_windows_execution_speed_throttling(
            platform_name="posix"
        )
        self.assertFalse(report["supported"])
        self.assertFalse(report["executionSpeedThrottlingDisabled"])
        self.assertEqual(report["reason"], "not-windows")

    def test_background_execution_policy_clears_windows_execution_speed_bit(self):
        kernel32 = mock.Mock()
        kernel32.GetCurrentProcess = mock.Mock(return_value=123)
        kernel32.SetProcessInformation = mock.Mock(return_value=1)

        report = replaylab_host.disable_windows_execution_speed_throttling(
            kernel32, platform_name="nt"
        )

        self.assertTrue(report["supported"])
        self.assertTrue(report["executionSpeedThrottlingDisabled"])
        kernel32.GetCurrentProcess.assert_called_once_with()
        call = kernel32.SetProcessInformation.call_args
        self.assertEqual(
            call.args[1], replaylab_host.PROCESS_INFORMATION_CLASS_POWER_THROTTLING
        )
        state = call.args[2]._obj
        self.assertEqual(
            state.Version,
            replaylab_host.PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        )
        self.assertEqual(
            state.ControlMask,
            replaylab_host.PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
        )
        self.assertEqual(state.StateMask, 0)
        self.assertEqual(call.args[3], replaylab_host.ctypes.sizeof(state))

    def test_startup_discovery_rejects_bundled_tool_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable_dir = root / "host"
            tools = executable_dir / "tools"
            tools.mkdir(parents=True)
            target = root / "outside-dxc.exe"
            target.write_bytes(b"outside-tool")
            link = tools / "dxc.exe"
            path_type = type(link)
            original_is_symlink = path_type.is_symlink

            def pretend_final_component_is_a_link(path):
                if path == link:
                    return True
                return original_is_symlink(path)

            # Do not require Windows' Create Symbolic Links privilege. The
            # important regression is that _regular checks the caller-visible
            # path before Path.resolve() can turn it into the target.
            with mock.patch.object(
                path_type, "is_symlink", autospec=True,
                side_effect=pretend_final_component_is_a_link,
            ), mock.patch.object(path_type, "resolve", autospec=True) as resolve:
                self.assertIsNone(replaylab_tools._regular(link))
                resolve.assert_not_called()

    def test_startup_discovery_never_trusts_arbitrary_path_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable_dir = root / "host"
            executable_dir.mkdir()
            path_dir = root / "untrusted-path"
            path_dir.mkdir()
            for name in (
                "dxc.exe",
                "fxc.exe",
                "d3dcompiler_47.dll",
                "spirv-cross.exe",
                "dxil-spirv.exe",
                "decomp.exe",
            ):
                (path_dir / name).write_bytes(b"untrusted-path-tool")
            environment = {"PATH": str(path_dir)}
            with mock.patch.dict(os.environ, {"PATH": str(path_dir)}, clear=False):
                discovered = replaylab_tools.discover(executable_dir, environment)
            self.assertTrue(all(value is None for value in discovered.values()))

    def test_d3dcompiler_discovery_accepts_only_bundled_fixed_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable_dir = root / "host"
            tools = executable_dir / "tools"
            tools.mkdir(parents=True)
            bundled = tools / "d3dcompiler_47.dll"
            bundled.write_bytes(b"explicit bundled compiler")
            found = replaylab_tools.discover(executable_dir, {})
            self.assertEqual(bundled.resolve(), found["d3dcompiler"])

            bundled.rename(tools / "renamed-compiler.dll")
            found = replaylab_tools.discover(
                executable_dir,
                {
                    "PATH": str(tools),
                    # A DLL with the correct name outside ReplayLab/tools is
                    # intentionally irrelevant to this discovery function.
                    "SystemRoot": str(root / "fake-windows"),
                },
            )
            self.assertIsNone(found["d3dcompiler"])

    def test_community_decompiler_requires_and_pins_colocated_d3dcompiler46(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable_dir = root / "host"
            community = executable_dir / "tools" / "community-hlsl-decompiler"
            community.mkdir(parents=True)
            decompiler = community / "cmd_Decompiler.exe"
            dependency = community / "D3DCompiler_46.dll"
            decompiler.write_bytes(b"community-decompiler")

            missing = replaylab_tools.discover(executable_dir, {})
            self.assertIsNone(missing["hlsl_decompiler"])
            self.assertIsNone(missing["hlsl_decompiler_dependency"])

            dependency.write_bytes(b"pinned-side-by-side-dependency")
            found = replaylab_tools.discover(executable_dir, {})
            self.assertEqual(decompiler.resolve(), found["hlsl_decompiler"])
            self.assertEqual(dependency.resolve(), found["hlsl_decompiler_dependency"])

            kwargs = replaylab_tools._generation_kwargs(found)
            self.assertNotIn("hlsl_decompiler_dependency", kwargs)
            self.assertEqual(
                [dependency.resolve()],
                kwargs["extra_fixed_files"]["hlsl-decompiler"],
            )
            tool_path = root / "community-tools.json"
            route_path = root / "community-routes.json"
            generate_official_toolchain_configs(tool_path, route_path, **kwargs)
            registry = json.loads(tool_path.read_text(encoding="utf-8"))
            for tool_id in (
                "community-hlsl-decompiler",
                "community-dxbc-assembly",
            ):
                fixed = registry["tools"][tool_id]["fixedFileHashes"]
                self.assertEqual(
                    replaylab_tools._sha256_file(dependency),
                    fixed[str(dependency.resolve())],
                )

            dependency.unlink()
            removed = replaylab_tools.discover(executable_dir, {})
            self.assertIsNone(removed["hlsl_decompiler"])

    def test_renodx_decompiler_requires_and_pins_colocated_dxcompiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable_dir = root / "host"
            tools_dir = executable_dir / "tools"
            tools_dir.mkdir(parents=True)
            decompiler = tools_dir / "decomp.exe"
            dependency = tools_dir / "dxcompiler.dll"
            decompiler.write_bytes(b"community-renodx-sm6-decompiler")

            missing = replaylab_tools.discover(executable_dir, {})
            self.assertIn("renodx_decompiler", missing)
            self.assertIsNone(missing["renodx_decompiler"])

            dependency.write_bytes(b"side-by-side-dxc-runtime")
            found = replaylab_tools.discover(executable_dir, {})
            self.assertEqual(decompiler.resolve(), found["renodx_decompiler"])

            kwargs = replaylab_tools._generation_kwargs(found)
            tool_path = root / "renodx-tools.json"
            route_path = root / "renodx-routes.json"
            generate_official_toolchain_configs(tool_path, route_path, **kwargs)
            registry = json.loads(tool_path.read_text(encoding="utf-8"))
            record = registry["tools"]["community-renodx-dxil-decompiler"]
            self.assertEqual(str(Path(sys.executable).resolve()), record["executable"])
            arguments = record["decompile"]["args"]
            self.assertTrue(arguments[0].endswith("renodx_decompiler_adapter.py"))
            self.assertEqual(
                str(decompiler.resolve()), arguments[arguments.index("--tool") + 1]
            )
            self.assertEqual(
                replaylab_tools._sha256_file(decompiler),
                arguments[arguments.index("--tool-sha256") + 1],
            )
            self.assertEqual(
                replaylab_tools._sha256_file(decompiler),
                record["fixedFileHashes"][str(decompiler.resolve())],
            )
            self.assertEqual(
                replaylab_tools._sha256_file(dependency),
                record["fixedFileHashes"][str(dependency.resolve())],
            )

            dependency.unlink()
            removed = replaylab_tools.discover(executable_dir, {})
            self.assertIsNone(removed["renodx_decompiler"])

    def test_token_is_random_persistent_and_never_in_codex_stdio_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = replaylab_host._load_or_create_token(root)
            second = replaylab_host._load_or_create_token(root)
            self.assertEqual(first, second)
            self.assertRegex(first, replaylab_host.TOKEN_PATTERN)
            snippet = replaylab_host.codex_toml(root / "ReplayLabMCP.exe")
            self.assertIn("mcp_servers.replaylab", snippet)
            self.assertIn("args = ['stdio']", snippet)
            self.assertNotIn(first, snippet)
            self.assertNotIn("mcp_servers.renderdoc", snippet)

    def test_external_cli_rejects_private_project_commands(self):
        parser = replaylab_host._parser()
        for command in (
            "project-list",
            "project-create",
            "project-register",
            "project-unregister",
            "project-activate",
            "project-configure",
            "project-add-capture",
            "project-remove-capture",
            "project-match-capture",
        ):
            with self.subTest(command=command), mock.patch(
                "sys.stderr", io.StringIO()
            ), self.assertRaises(SystemExit):
                parser.parse_args([command])

    def test_owner_registry_prunes_dead_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replaylab_host.register_owner(root, 111)
            replaylab_host.register_owner(root, 222)
            with mock.patch.object(
                replaylab_host, "_process_alive", side_effect=lambda pid: pid == 222
            ):
                self.assertEqual([222], replaylab_host.live_owners(root))
            self.assertFalse((root / "owners" / "111.json").exists())
            self.assertTrue(replaylab_host.release_owner(root, 222))
            self.assertFalse(replaylab_host.release_owner(root, 222))

    def test_ensure_attaches_owner_only_after_state_and_health_identity_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = self._publish_host_identity(root)
            with mock.patch.object(
                replaylab_host, "health", return_value=current
            ), mock.patch.object(replaylab_host, "_spawn_detached") as spawn:
                result = replaylab_host.ensure(root, os.getpid())

            spawn.assert_not_called()
            self.assertFalse(result["started"])
            self.assertEqual(2, result["hostStateProtocolVersion"])
            self.assertEqual("a" * 32, result["hostInstanceId"])
            owner = json.loads(
                (root / "owners" / ("%d.json" % os.getpid())).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(os.getpid(), owner["pid"])

    def test_ensure_rejects_incompatible_live_identity_without_new_owner(self):
        cases = (
            ("health protocol", {"health_protocol": 1}),
            ("health instance", {"health_instance": "b" * 32}),
            ("state protocol", {"state_protocol": 1}),
        )
        for label, overrides in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                current = self._publish_host_identity(
                    root,
                    protocol_version=overrides.get("state_protocol", 2),
                )
                current["hostStateProtocolVersion"] = overrides.get(
                    "health_protocol", 2
                )
                current["hostInstanceId"] = overrides.get(
                    "health_instance", "a" * 32
                )
                with mock.patch.object(
                    replaylab_host, "health", return_value=current
                ), mock.patch.object(replaylab_host, "_spawn_detached") as spawn:
                    with self.assertRaisesRegex(
                        RuntimeError, "incompatible identity contract"
                    ):
                        replaylab_host.ensure(root, os.getpid())

                spawn.assert_not_called()
                self.assertFalse(
                    (root / "owners" / ("%d.json" % os.getpid())).exists()
                )

    def test_ensure_fails_fast_on_incompatible_fresh_live_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._publish_host_identity(root, protocol_version=1)
            with mock.patch.object(
                replaylab_host, "health", return_value=None
            ), mock.patch.object(replaylab_host, "_spawn_detached") as spawn:
                with self.assertRaisesRegex(
                    RuntimeError, "incompatible identity contract"
                ):
                    replaylab_host.ensure(root, os.getpid())

            spawn.assert_not_called()
            self.assertFalse(
                (root / "owners" / ("%d.json" % os.getpid())).exists()
            )

    def test_ensure_spawns_ownerless_until_new_host_identity_is_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = self._publish_host_identity(root)
            # Remove the synthetic state until the detached child is launched.
            (root / "host.json").unlink()

            def launch(command):
                self._publish_host_identity(root)
                self.assertNotIn("--owner-pid", command)

            with mock.patch.object(
                replaylab_host, "health", side_effect=(None, current)
            ), mock.patch.object(
                replaylab_host, "_spawn_detached", side_effect=launch
            ) as spawn:
                result = replaylab_host.ensure(root, os.getpid(), 49643)

            spawn.assert_called_once()
            self.assertTrue(result["started"])
            self.assertEqual("a" * 32, result["hostInstanceId"])
            self.assertTrue(
                (root / "owners" / ("%d.json" % os.getpid())).is_file()
            )

    def test_concurrent_ensure_spawns_only_one_external_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launches = []
            results = []
            errors = []

            def current_health(unused_port):
                state = replaylab_host._load_json(root / "host.json")
                if state is None:
                    return None
                return {
                    "service": "ReplayLab",
                    "version": replaylab_host.SERVICE_VERSION,
                    "hostStateProtocolVersion":
                        replaylab_host.HOST_STATE_PROTOCOL_VERSION,
                    "hostInstanceId": state["hostInstanceId"],
                    "transport": "streamable-http",
                    "pid": state["pid"],
                }

            def launch(command):
                launches.append(list(command))
                self._publish_host_identity(root)

            def attach():
                try:
                    results.append(
                        replaylab_host.ensure(root, os.getpid(), 49643))
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(
                replaylab_host, "health", side_effect=current_health
            ), mock.patch.object(
                replaylab_host, "_spawn_detached", side_effect=launch
            ):
                workers = [threading.Thread(target=attach) for unused in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(5)

            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual(1, len(launches))
            self.assertEqual(1, sum(1 for value in results if value["started"]))

    def test_external_host_exposes_no_qrenderdoc_private_http_routes(self):
        source = Path(replaylab_host.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=replaylab_host.__file__)
        serve = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_serve"
        )
        route_literals = {
            node.value for node in ast.walk(serve)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/")
        }
        serve_source = ast.get_source_segment(source, serve)
        self.assertEqual({"/health"}, route_literals)
        self.assertIn("mcp.streamable_http_app()", serve_source)
        self.assertNotIn("resume_persistent", serve_source)
        self.assertNotIn("/replaylab/project-restore", serve_source)
        self.assertNotIn("/replaylab/shader-source/save", serve_source)
        self.assertNotIn("/replaylab/internal/project-operation", serve_source)

    def test_state_status_never_discloses_bearer_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = replaylab_host._load_or_create_token(root)
            replaylab_host._atomic_json(
                root / "host.json",
                {"service": "ReplayLab", "port": 49643, "pid": 42},
            )
            with mock.patch.object(replaylab_host, "health", return_value=None):
                value = replaylab_host.status(root)
            self.assertNotIn(token, json.dumps(value))
            self.assertEqual(str(root / "host.token"), value["tokenFile"])

    def test_atomic_host_json_is_ascii_wire_and_round_trips_chinese_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "host.json"
            expected = {
                "capturePath": r"D:\抓帧\白银之城\4.rdc",
                "error": "读取失败：路径不存在",
            }

            replaylab_host._atomic_json(path, expected)

            wire = path.read_bytes()
            self.assertTrue(wire.isascii())
            self.assertIn(b"\\u6293\\u5e27", wire)
            self.assertEqual(expected, json.loads(wire.decode("ascii")))

    def test_project_restore_request_is_exact_and_capture_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            capture = root / "frame.rdc"
            capture.write_bytes(b"rdc")
            request = {
                "projectRoot": str(project.resolve()),
                "capturePath": str(capture.resolve()),
                "captureSessionId": "a" * 32,
            }

            parsed = internal_tool_service.parse_project_restore_request(
                json.dumps(request).encode("utf-8")
            )
            self.assertEqual(str(project.resolve()), parsed["projectRoot"])
            self.assertEqual(str(capture.resolve()), parsed["capturePath"])

            with self.assertRaisesRegex(
                internal_tool_service.ProjectRestoreRequestError, "fields"
            ):
                internal_tool_service.parse_project_restore_request(
                    json.dumps({**request, "extra": True}).encode("utf-8")
                )
            with self.assertRaisesRegex(
                internal_tool_service.ProjectRestoreRequestError, "duplicate JSON key"
            ):
                duplicate_body = (
                    (
                        '{"projectRoot":%s,"projectRoot":%s,'
                        '"capturePath":%s,"captureSessionId":"%s"}'
                    )
                    % (
                        json.dumps(str(project.resolve())),
                        json.dumps(str(project.resolve())),
                        json.dumps(str(capture.resolve())),
                        "a" * 32,
                    )
                ).encode("utf-8")
                internal_tool_service.parse_project_restore_request(
                    duplicate_body
                )
            invalid_session = dict(request)
            invalid_session["captureSessionId"] = "A" * 32
            with self.assertRaisesRegex(
                internal_tool_service.ProjectRestoreRequestError, "lowercase hexadecimal"
            ):
                internal_tool_service.parse_project_restore_request(
                    json.dumps(invalid_session).encode("utf-8")
                )
            relative = dict(request)
            relative["capturePath"] = "frame.rdc"
            with self.assertRaisesRegex(
                internal_tool_service.ProjectRestoreRequestError, "absolute"
            ):
                internal_tool_service.parse_project_restore_request(
                    json.dumps(relative).encode("utf-8")
                )

    def test_project_restore_registry_reports_paginated_progress_and_result(self):
        calls = []

        def runner(project_root, capture_path, capture_session_id, *, progress_callback):
            calls.append((project_root, capture_path, capture_session_id))
            for current in (1, 2):
                progress_callback(
                    {
                        "schema": "replaylab-project-restore-progress/v1",
                        "schemaVersion": 1,
                        "operationId": "operation",
                        "phase": "restore-shaders",
                        "current": current,
                        "total": 2,
                        "message": "Restoring %d/2" % current,
                        "detail": {},
                    }
                )
            return {"completed": True, "capture": {"sessionId": capture_session_id}}

        registry = internal_tool_service.ProjectRestoreJobRegistry(runner=runner)
        try:
            request = {
                "projectRoot": "D:/project",
                "capturePath": "D:/capture.rdc",
                "captureSessionId": "a" * 32,
            }
            submitted, deduplicated = registry.submit(request)
            self.assertFalse(deduplicated)
            self.assertEqual(
                internal_tool_service.PROJECT_RESTORE_JOB_SCHEMA, submitted["schema"]
            )
            self.assertRegex(
                submitted["jobId"], internal_tool_service.PROJECT_RESTORE_JOB_ID_PATTERN
            )

            completed = _wait_restore_job(registry, submitted["jobId"])
            self.assertEqual("completed", completed["status"])
            self.assertEqual(2, completed["latestSequence"])
            self.assertEqual([1, 2], [item["sequence"] for item in completed["progress"]])
            self.assertTrue(completed["result"]["completed"])
            self.assertEqual(
                {"sequence", "phase", "message", "current", "total"},
                set(completed["progress"][0]),
            )
            after_first = registry.get(submitted["jobId"], after=1)
            assert after_first is not None
            self.assertEqual([2], [item["sequence"] for item in after_first["progress"]])
            self.assertEqual(
                [("D:/project", "D:/capture.rdc", "a" * 32)], calls
            )
        finally:
            registry.shutdown()

    def test_project_restore_registry_deduplicates_active_key_and_uses_one_worker(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def runner(project_root, capture_path, capture_session_id, *, progress_callback):
            calls.append(capture_session_id)
            started.set()
            self.assertTrue(release.wait(3.0))
            return {"completed": True}

        registry = internal_tool_service.ProjectRestoreJobRegistry(
            runner=runner, max_active_jobs=2
        )
        try:
            request = {
                "projectRoot": "D:/project",
                "capturePath": "D:/capture.rdc",
                "captureSessionId": "a" * 32,
            }
            first, first_deduplicated = registry.submit(request)
            self.assertFalse(first_deduplicated)
            self.assertTrue(started.wait(1.0))
            duplicate, duplicate_deduplicated = registry.submit(dict(request))
            self.assertTrue(duplicate_deduplicated)
            self.assertEqual(first["jobId"], duplicate["jobId"])

            second_request = {**request, "captureSessionId": "b" * 32}
            second, second_deduplicated = registry.submit(second_request)
            self.assertFalse(second_deduplicated)
            time.sleep(0.02)
            self.assertEqual(["a" * 32], calls)
            self.assertEqual("queued", registry.get(second["jobId"])["status"])

            release.set()
            self.assertEqual("completed", _wait_restore_job(registry, first["jobId"])["status"])
            self.assertEqual("completed", _wait_restore_job(registry, second["jobId"])["status"])
            self.assertEqual(["a" * 32, "b" * 32], calls)
        finally:
            release.set()
            registry.shutdown()

    def test_project_restore_registry_bounds_active_history_and_progress(self):
        started = threading.Event()
        release = threading.Event()

        def blocking_runner(*_args, progress_callback):
            started.set()
            self.assertTrue(release.wait(3.0))
            return {"completed": True}

        registry = internal_tool_service.ProjectRestoreJobRegistry(
            runner=blocking_runner, max_active_jobs=1
        )
        try:
            request = {
                "projectRoot": "D:/project",
                "capturePath": "D:/capture.rdc",
                "captureSessionId": "a" * 32,
            }
            first, _unused = registry.submit(request)
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(internal_tool_service.ProjectRestoreCapacityError):
                registry.submit({**request, "captureSessionId": "b" * 32})
            release.set()
            _wait_restore_job(registry, first["jobId"])
        finally:
            release.set()
            registry.shutdown()

        def verbose_runner(*_args, progress_callback):
            for current in range(1, 6):
                progress_callback(
                    {
                        "phase": "index",
                        "message": str(current),
                        "current": current,
                        "total": 5,
                    }
                )
            return {"completed": True}

        registry = internal_tool_service.ProjectRestoreJobRegistry(
            runner=verbose_runner,
            max_progress_events=3,
            progress_page_size=2,
        )
        try:
            submitted, _unused = registry.submit(request)
            completed = _wait_restore_job(registry, submitted["jobId"])
            self.assertEqual(5, completed["latestSequence"])
            self.assertEqual([3, 4], [item["sequence"] for item in completed["progress"]])
            self.assertTrue(completed["progressTruncated"])
            tail = registry.get(submitted["jobId"], after=4)
            assert tail is not None
            self.assertEqual([5], [item["sequence"] for item in tail["progress"]])
            self.assertFalse(tail["progressTruncated"])
        finally:
            registry.shutdown()

    def test_project_restore_registry_bounds_failure_and_expires_history(self):
        clock = [10.0]

        def failing_runner(*_args, progress_callback):
            raise RuntimeError("x" * (internal_tool_service.MAX_PROJECT_RESTORE_ERROR_BYTES * 2))

        registry = internal_tool_service.ProjectRestoreJobRegistry(
            runner=failing_runner,
            history_ttl_seconds=5.0,
            monotonic=lambda: clock[0],
        )
        try:
            request = {
                "projectRoot": "D:/project",
                "capturePath": "D:/capture.rdc",
                "captureSessionId": "a" * 32,
            }
            submitted, _unused = registry.submit(request)
            failed = _wait_restore_job(registry, submitted["jobId"])
            self.assertEqual("failed", failed["status"])
            self.assertLessEqual(
                len(failed["error"].encode("utf-8")),
                internal_tool_service.MAX_PROJECT_RESTORE_ERROR_BYTES,
            )
            clock[0] += 6.0
            self.assertIsNone(registry.get(submitted["jobId"]))
        finally:
            registry.shutdown()

        registry = internal_tool_service.ProjectRestoreJobRegistry(
            runner=lambda *_args, progress_callback: {"completed": True},
            max_history_jobs=1,
        )
        try:
            first_request = {
                "projectRoot": "D:/project",
                "capturePath": "D:/capture.rdc",
                "captureSessionId": "b" * 32,
            }
            first, _unused = registry.submit(first_request)
            _wait_restore_job(registry, first["jobId"])
            second, _unused = registry.submit(
                {**first_request, "captureSessionId": "c" * 32}
            )
            _wait_restore_job(registry, second["jobId"])
            self.assertIsNone(registry.get(first["jobId"]))
            self.assertIsNotNone(registry.get(second["jobId"]))
        finally:
            registry.shutdown()

        with mock.patch.object(
                internal_tool_service, "MAX_PROJECT_RESTORE_RESULT_BYTES", 128
        ):
            registry = internal_tool_service.ProjectRestoreJobRegistry(
                runner=lambda *_args, progress_callback: {
                    "completed": True,
                    "oversized": "x" * 256,
                }
            )
            try:
                oversized, _unused = registry.submit(first_request)
                failed = _wait_restore_job(registry, oversized["jobId"])
                self.assertEqual("failed", failed["status"])
                self.assertIn("byte limit", failed["error"])
            finally:
                registry.shutdown()

    def test_project_restore_endpoint_helpers_return_bounded_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            capture = root / "frame.rdc"
            capture.write_bytes(b"rdc")
            body = json.dumps(
                {
                    "projectRoot": str(project.resolve()),
                    "capturePath": str(capture.resolve()),
                    "captureSessionId": "c" * 32,
                }
            ).encode("utf-8")
            registry = internal_tool_service.ProjectRestoreJobRegistry(
                runner=lambda *_args, progress_callback: {"completed": True}
            )
            try:
                status_code, submitted = asyncio.run(
                    internal_tool_service.project_restore_post_response(
                        _ChunkedRequest(body, content_length=str(len(body))), registry
                    )
                )
                self.assertEqual(202, status_code)
                _wait_restore_job(registry, submitted["jobId"])
                status_code, completed = internal_tool_service.project_restore_get_response(
                    submitted["jobId"], "0", registry
                )
                self.assertEqual(200, status_code)
                self.assertEqual("completed", completed["status"])

                bad_status, bad = internal_tool_service.project_restore_get_response(
                    submitted["jobId"], "-1", registry
                )
                self.assertEqual(400, bad_status)
                self.assertIn("after", bad["error"])
                missing_status, _missing = internal_tool_service.project_restore_get_response(
                    "d" * 32, "0", registry
                )
                self.assertEqual(404, missing_status)

                oversized_status, _oversized = asyncio.run(
                    internal_tool_service.project_restore_post_response(
                        _ChunkedRequest(
                            b"{}",
                            content_length=str(
                                internal_tool_service.MAX_PROJECT_RESTORE_REQUEST_BYTES + 1
                            ),
                        ),
                        registry,
                    )
                )
                self.assertEqual(413, oversized_status)
            finally:
                registry.shutdown()

    def test_shader_editor_save_request_is_exact_bounded_utf8_json(self):
        valid = {
            "liveEditId": "a" * 32,
            "authorizationId": "b" * 32,
            "workspaceId": "c" * 32,
            "expectedRawSha256": "d" * 64,
            "text": "float4 MainPS() : SV_Target { return 1; }\n",
        }

        parsed = internal_tool_service.parse_shader_editor_save_request(
            memoryview(json.dumps(valid).encode("utf-8"))
        )
        self.assertEqual(valid, parsed)

        invalid_requests = [
            (b"", 400),
            (b"[]", 400),
            (b"\xff", 400),
            (b'{"value":NaN}', 400),
            (
                (
                    '{"liveEditId":"%s","liveEditId":"%s",'
                    '"authorizationId":"%s","workspaceId":"%s",'
                    '"expectedRawSha256":"%s","text":"x"}'
                    % ("a" * 32, "a" * 32, "b" * 32, "c" * 32, "d" * 64)
                ).encode("utf-8"),
                400,
            ),
            (
                json.dumps({**valid, "sourcePath": "D:/untrusted/shader.hlsl"}).encode(
                    "utf-8"
                ),
                400,
            ),
            (
                json.dumps({**valid, "liveEditId": "A" * 32}).encode("utf-8"),
                400,
            ),
            (
                json.dumps({**valid, "expectedRawSha256": "D" * 64}).encode(
                    "utf-8"
                ),
                400,
            ),
            (json.dumps({**valid, "text": ""}).encode("utf-8"), 400),
            (json.dumps({**valid, "text": "x\x00y"}).encode("utf-8"), 400),
            (
                json.dumps(
                    {
                        **valid,
                        "text": "x" * (internal_tool_service.MAX_SHADER_EDITOR_SOURCE_BYTES + 1),
                    }
                ).encode("utf-8"),
                413,
            ),
            (
                b"x" * (internal_tool_service.MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES + 1),
                413,
            ),
        ]
        manager = mock.Mock()
        for body, expected_status in invalid_requests:
            with self.subTest(expected_status=expected_status, prefix=body[:32]):
                status, result = internal_tool_service.shader_editor_save_response(
                    body, manager
                )
                self.assertEqual(expected_status, status)
                self.assertEqual({"error"}, set(result))
        manager.save_authorized_source.assert_not_called()

    def test_shader_editor_save_response_binds_manager_result_and_statuses(self):
        request = {
            "liveEditId": "a" * 32,
            "authorizationId": "b" * 32,
            "workspaceId": "c" * 32,
            "expectedRawSha256": "d" * 64,
            "text": "float4 MainPS() : SV_Target { return 2; }\n",
        }
        payload = json.dumps(request).encode("utf-8")
        saved_sha256 = hashlib.sha256(
            request["text"].encode("utf-8")
        ).hexdigest()
        manager = mock.Mock()
        manager.save_authorized_source.return_value = {
            "watchId": "e" * 32,
            "rawSha256": saved_sha256,
            "size": len(request["text"].encode("utf-8")),
            "modifiedNs": 123456789,
            "changed": True,
            "compileScheduled": True,
        }

        status, response = internal_tool_service.shader_editor_save_response(
            payload, manager
        )

        self.assertEqual(200, status)
        manager.save_authorized_source.assert_called_once_with(
            request["liveEditId"],
            request["authorizationId"],
            request["workspaceId"],
            request["expectedRawSha256"],
            request["text"],
        )
        self.assertEqual(
            {
                "schema",
                "schemaVersion",
                "liveEditId",
                "authorizationId",
                "workspaceId",
                "watchId",
                "savedRawSha256",
                "size",
                "modifiedNs",
                "changed",
                "compileScheduled",
            },
            set(response),
        )
        self.assertEqual(
            "replaylab-shader-source-save/v1", response["schema"]
        )
        self.assertEqual(saved_sha256, response["savedRawSha256"])

        cases = [
            (
                WorkspaceWatchConflictError("source changed on disk"),
                409,
                True,
            ),
            (WorkspaceWatchError("watcher is not running"), 409, False),
            (RuntimeError("audit persistence failed"), 503, False),
        ]
        for error, expected_status, expected_conflict in cases:
            with self.subTest(error=type(error).__name__):
                rejected = mock.Mock()
                rejected.save_authorized_source.side_effect = error
                status, response = internal_tool_service.shader_editor_save_response(
                    payload, rejected
                )
                self.assertEqual(expected_status, status)
                self.assertIs(expected_conflict, response.get("conflict", False))
                self.assertIn(str(error), response["error"])

        invalid_result = mock.Mock()
        invalid_result.save_authorized_source.return_value = {
            **manager.save_authorized_source.return_value,
            "changed": 1,
        }
        status, response = internal_tool_service.shader_editor_save_response(
            payload, invalid_result
        )
        self.assertEqual(503, status)
        self.assertIn("invalid result", response["error"])

    def test_shader_editor_body_reader_rejects_declared_or_streamed_oversize(self):
        with self.assertRaises(internal_tool_service.ShaderEditorSaveRequestError) as caught:
            asyncio.run(
                internal_tool_service._read_shader_editor_save_body(
                    _ChunkedRequest(
                        b"{}",
                        content_length=str(
                            internal_tool_service.MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES + 1
                        ),
                    )
                )
            )
        self.assertEqual(413, caught.exception.status_code)

        with self.assertRaises(internal_tool_service.ShaderEditorSaveRequestError) as caught:
            asyncio.run(
                internal_tool_service._read_shader_editor_save_body(
                    _ChunkedRequest(
                        b"x"
                        * (internal_tool_service.MAX_SHADER_EDITOR_SAVE_REQUEST_BYTES + 1)
                    )
                )
            )
        self.assertEqual(413, caught.exception.status_code)

    def test_bearer_gate_rejects_missing_token_and_allows_health(self):
        calls = []

        async def downstream(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def invoke(path, headers):
            output = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                output.append(message)

            app = replaylab_host.BearerAuthApp(downstream, "secret-token-value-12345678901234567890")
            await app(
                {"type": "http", "path": path, "headers": headers}, receive, send
            )
            return output

        denied = asyncio.run(invoke("/mcp", []))
        self.assertEqual(401, denied[0]["status"])
        self.assertEqual([], calls)
        denied_restore = asyncio.run(invoke("/replaylab/project-restore", []))
        self.assertEqual(401, denied_restore[0]["status"])
        self.assertEqual([], calls)
        denied_shader_save = asyncio.run(
            invoke("/replaylab/shader-source/save", [])
        )
        self.assertEqual(401, denied_shader_save[0]["status"])
        self.assertEqual([], calls)
        health = asyncio.run(invoke("/health", []))
        self.assertEqual(200, health[0]["status"])
        self.assertEqual(["/health"], calls)

    def test_packaged_adapter_host_is_hash_pinned_and_dispatches_fixed_subcommand(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "ReplayLabMCP.exe"
            decompiler = root / "cmd_Decompiler.exe"
            host.write_bytes(b"packaged-host")
            decompiler.write_bytes(b"community-decompiler")
            tools = root / "tools.json"
            routes = root / "routes.json"
            generate_official_toolchain_configs(
                tools,
                routes,
                hlsl_decompiler=decompiler,
                adapter_host=host,
            )
            registry = json.loads(tools.read_text(encoding="utf-8"))
            record = registry["tools"]["community-hlsl-decompiler"]
            self.assertEqual(str(host.resolve()), record["executable"])
            self.assertEqual("adapter-hlsl", record["decompile"]["args"][0])
            self.assertIn(str(decompiler.resolve()), record["fixedFileHashes"])
            assembly = registry["tools"]["community-dxbc-assembly"]
            self.assertEqual(str(host.resolve()), assembly["executable"])
            self.assertEqual(
                "adapter-dxbc-assembly", assembly["decompile"]["args"][0]
            )
            self.assertEqual(
                "adapter-dxbc-assembly", assembly["compile"]["args"][0]
            )
            self.assertIn("{baseline}", assembly["compile"]["args"])
            self.assertIn(str(decompiler.resolve()), assembly["fixedFileHashes"])

    def test_packaged_d3dcompile_adapter_is_pinned_and_host_dispatches_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "ReplayLabMCP.exe"
            dll = root / "d3dcompiler_47.dll"
            host.write_bytes(b"packaged-host")
            dll.write_bytes(b"packaged-d3dcompiler")
            tool_path = root / "tools.json"
            route_path = root / "routes.json"
            generate_official_toolchain_configs(
                tool_path,
                route_path,
                d3dcompiler=dll,
                adapter_host=host,
            )
            registry = json.loads(tool_path.read_text(encoding="utf-8"))
            record = registry["tools"]["d3dcompiler47-exact-profile"]
            arguments = record["compile"]["args"]
            self.assertEqual(str(host.resolve()), record["executable"])
            self.assertEqual("adapter-d3dcompile", arguments[0])
            self.assertIn(str(dll.resolve()), record["fixedFileHashes"])
            self.assertEqual(
                record["fixedFileHashes"][str(dll.resolve())],
                arguments[arguments.index("--dll-sha256") + 1],
            )

            with mock.patch.object(d3dcompiler_adapter, "main", return_value=23) as main:
                self.assertEqual(
                    23,
                    replaylab_host.main(
                        ["adapter-d3dcompile", "--input", "fixed-input"]
                    ),
                )
            main.assert_called_once_with(["--input", "fixed-input"])

    def test_packaged_dxil_assembler_is_pinned_and_host_dispatches_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "ReplayLabMCP.exe"
            dxc = root / "dxc.exe"
            dll = root / "dxcompiler.dll"
            host.write_bytes(b"packaged-host")
            dxc.write_bytes(b"packaged-dxc-cli")
            dll.write_bytes(b"packaged-dxcompiler")
            tool_path = root / "tools.json"
            route_path = root / "routes.json"
            path_type = type(host)
            original_is_file = path_type.is_file

            def hide_external_dxil_adapter(path):
                if path.name == "dxil_assembler_adapter.py":
                    return False
                return original_is_file(path)

            with mock.patch.object(
                path_type,
                "is_file",
                autospec=True,
                side_effect=hide_external_dxil_adapter,
            ):
                generate_official_toolchain_configs(
                    tool_path,
                    route_path,
                    dxc=dxc,
                    adapter_host=host,
                )
            registry = json.loads(tool_path.read_text(encoding="utf-8"))
            record = registry["tools"]["dxc-official-dxil-assembler"]
            arguments = record["compile"]["args"]
            self.assertEqual(str(host.resolve()), record["executable"])
            self.assertEqual("adapter-dxil-assemble", arguments[0])
            self.assertEqual(
                replaylab_tools._sha256_file(dll),
                record["fixedFileHashes"][str(dll.resolve())],
            )
            self.assertEqual(
                replaylab_tools._sha256_file(dll),
                arguments[arguments.index("--dll-sha256") + 1],
            )
            self.assertEqual("{input}", arguments[arguments.index("--input") + 1])
            self.assertEqual("{output}", arguments[arguments.index("--output") + 1])
            self.assertFalse(any("profile" in value for value in arguments))
            self.assertFalse(any("entry" in value for value in arguments))

            with mock.patch.object(dxil_assembler_adapter, "main", return_value=29) as main:
                self.assertEqual(
                    29,
                    replaylab_host.main(
                        ["adapter-dxil-assemble", "--input", "fixed-input"]
                    ),
                )
            main.assert_called_once_with(["--input", "fixed-input"])

    def test_packaged_host_never_requires_external_adapter_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "ReplayLabMCP.exe"
            dll = root / "d3dcompiler_47.dll"
            spirv_cross = root / "spirv-cross.exe"
            decompiler = root / "cmd_Decompiler.exe"
            host.write_bytes(b"frozen-host-with-internal-adapters")
            dll.write_bytes(b"packaged-d3dcompiler")
            spirv_cross.write_bytes(b"packaged-spirv-cross")
            decompiler.write_bytes(b"packaged-community-decompiler")

            path_type = type(host)
            original_is_file = path_type.is_file
            adapter_names = {
                "d3dcompiler_adapter.py",
                "spirv_cross_adapter.py",
                "hlsl_decompiler_adapter.py",
                "dxbc_assembly_adapter.py",
            }

            def hide_external_adapter_files(path):
                if path.name in adapter_names and path.parent.name == "adapters":
                    return False
                return original_is_file(path)

            with mock.patch.object(
                path_type,
                "is_file",
                autospec=True,
                side_effect=hide_external_adapter_files,
            ):
                tool_path = root / "packaged-tools.json"
                route_path = root / "packaged-routes.json"
                generate_official_toolchain_configs(
                    tool_path,
                    route_path,
                    d3dcompiler=dll,
                    spirv_cross=spirv_cross,
                    hlsl_decompiler=decompiler,
                    adapter_host=host,
                )
                registry = json.loads(tool_path.read_text(encoding="utf-8"))
                host_hash = replaylab_tools._sha256_file(host)
                adapter_tools = {
                    "d3dcompiler47-exact-profile",
                    "spirv-cross-hlsl",
                    "spirv-cross-vulkan-glsl",
                    "community-hlsl-decompiler",
                    "community-dxbc-assembly",
                }
                self.assertTrue(adapter_tools.issubset(registry["tools"]))
                for tool_id in adapter_tools:
                    record = registry["tools"][tool_id]
                    self.assertEqual(str(host.resolve()), record["executable"])
                    self.assertEqual(host_hash, record["executableSha256"])
                    self.assertFalse(
                        any(
                            path.casefold().endswith(".py")
                            for path in record["fixedFileHashes"]
                        )
                    )

                # The same missing files still fail closed in normal source
                # mode, where the Python interpreter launches adapter scripts.
                with self.assertRaisesRegex(
                    ToolConfigurationError, "D3DCompiler adapter is missing"
                ):
                    generate_official_toolchain_configs(
                        root / "source-tools.json",
                        root / "source-routes.json",
                        d3dcompiler=dll,
                    )

    def test_startup_discovery_sets_both_config_paths_without_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_fxc = root / "fxc.exe"
            fake_fxc.write_bytes(b"fake-fxc")
            environment = {
                "REPLAYLAB_CONFIG_DIR": str(root / "config"),
                "LOCALAPPDATA": str(root / "local"),
            }
            discovered = self._discovered(fxc=fake_fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=discovered):
                report = replaylab_tools.configure_environment(root, environment)
            self.assertTrue(report["generated"])
            self.assertTrue(report["binaryPathsWereDiscovered"])
            self.assertFalse(report["downloadsPerformed"])
            self.assertTrue(Path(environment[replaylab_tools.TOOLS_ENV]).is_file())
            self.assertTrue(Path(environment[replaylab_tools.ROUTES_ENV]).is_file())
            self.assertEqual(replaylab_tools.DISCOVERY_SCHEMA, report["schema"])
            managed = report["managedConfig"]
            self.assertEqual(
                replaylab_tools._sha256_file(Path(environment[replaylab_tools.TOOLS_ENV])),
                managed["toolConfigSha256"],
            )
            self.assertEqual(
                replaylab_tools._sha256_file(Path(environment[replaylab_tools.ROUTES_ENV])),
                managed["routeConfigSha256"],
            )

    def test_managed_config_preserves_exact_catalog_for_byte_identical_relocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portable = root / "portable" / "fxc.exe"
            installed = root / "installed" / "fxc.exe"
            portable.parent.mkdir()
            installed.parent.mkdir()
            portable.write_bytes(b"same-pinned-fxc")
            installed.write_bytes(portable.read_bytes())
            config = root / "config"

            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=portable),
            ):
                first = replaylab_tools.configure_environment(
                    portable.parent, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            original_pair = (
                (config / "shader-tools.v1.json").read_bytes(),
                (config / "shader-toolchains.v1.json").read_bytes(),
            )

            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=installed),
            ):
                relocated = replaylab_tools.configure_environment(
                    installed.parent, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )

            self.assertEqual(
                "existing-managed-config-relocated-copy", relocated["source"]
            )
            self.assertFalse(relocated["generated"])
            self.assertTrue(relocated["relocationEvidence"]["equivalent"])
            self.assertEqual(
                first["managedConfig"], relocated["managedConfig"]
            )
            self.assertEqual(
                original_pair,
                (
                    (config / "shader-tools.v1.json").read_bytes(),
                    (config / "shader-toolchains.v1.json").read_bytes(),
                ),
            )
            registry = json.loads(original_pair[0].decode("utf-8"))
            self.assertEqual(
                str(portable.resolve()),
                registry["tools"]["fxc-exact-profile"]["executable"],
            )

    def test_missing_old_relocated_tool_refreshes_instead_of_weakening_pins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portable = root / "portable" / "fxc.exe"
            installed = root / "installed" / "fxc.exe"
            portable.parent.mkdir()
            installed.parent.mkdir()
            portable.write_bytes(b"same-pinned-fxc")
            installed.write_bytes(portable.read_bytes())
            config = root / "config"

            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=portable),
            ):
                replaylab_tools.configure_environment(
                    portable.parent, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            portable.unlink()

            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=installed),
            ):
                refreshed = replaylab_tools.configure_environment(
                    installed.parent, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )

            self.assertEqual("startup-refresh", refreshed["source"])
            self.assertTrue(refreshed["generated"])
            self.assertFalse(refreshed["relocationEvidence"]["equivalent"])
            registry = json.loads(
                (config / "shader-tools.v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                str(installed.resolve()),
                registry["tools"]["fxc-exact-profile"]["executable"],
            )

    def test_managed_config_refresh_adds_newly_bundled_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            tools = host / "tools"
            tools.mkdir(parents=True)
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"sdk-fxc")
            config = root / "config"

            with mock.patch.object(
                replaylab_tools, "discover", return_value=self._discovered(fxc=fxc)
            ):
                first = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            first_tool_hash = first["managedConfig"]["toolConfigSha256"]

            spirv_cross = tools / "spirv-cross.exe"
            spirv_cross.write_bytes(b"bundled-spirv-cross")
            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=fxc, spirv_cross=spirv_cross),
            ):
                refreshed = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )

            self.assertEqual("startup-refresh", refreshed["source"])
            self.assertTrue(refreshed["generated"])
            self.assertTrue(refreshed["binaryPathsWereDiscovered"])
            self.assertNotEqual(
                first_tool_hash, refreshed["managedConfig"]["toolConfigSha256"]
            )
            registry = json.loads((config / "shader-tools.v1.json").read_text("utf-8"))
            fixed_paths = {
                path
                for record in registry["tools"].values()
                for path in record.get("fixedFileHashes", {})
            }
            self.assertIn(str(spirv_cross.resolve()), fixed_paths)

    def test_managed_config_refreshes_updated_tool_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc-v1")
            config = root / "config"
            found = self._discovered(fxc=fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                first = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            old_registry_hash = first["managedConfig"]["toolConfigSha256"]

            fxc.write_bytes(b"fxc-v2")
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                refreshed = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            self.assertEqual("startup-refresh", refreshed["source"])
            self.assertNotEqual(
                old_registry_hash, refreshed["managedConfig"]["toolConfigSha256"]
            )
            self.assertEqual(
                replaylab_tools._sha256_file(config / "shader-tools.v1.json"),
                refreshed["managedConfig"]["toolConfigSha256"],
            )

    def test_modified_default_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc-v1")
            config = root / "config"
            found = self._discovered(fxc=fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            tool_path = config / "shader-tools.v1.json"
            tool_path.write_bytes(tool_path.read_bytes() + b"\n")
            modified = tool_path.read_bytes()
            fxc.write_bytes(b"fxc-v2")

            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                report = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            self.assertEqual("existing-unmanaged-config", report["source"])
            self.assertIn("differs", report["refreshBlockedReason"])
            self.assertEqual(modified, tool_path.read_bytes())
            self.assertIn("managedConfig", report)

    def test_legacy_status_without_dual_hashes_never_authorizes_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            config = root / "config"
            config.mkdir()
            tool_path = config / "shader-tools.v1.json"
            route_path = config / "shader-toolchains.v1.json"
            status_path = config / "shader-toolchain-status.json"
            tool_path.write_bytes(b"user-tool-config")
            route_path.write_bytes(b"user-route-config")
            status_path.write_text(
                json.dumps(
                    {
                        "schema": "replaylab-shader-tool-discovery/v1",
                        "source": "startup-discovery",
                        "generated": True,
                        "toolRegistrySha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc")
            with mock.patch.object(
                replaylab_tools,
                "discover",
                return_value=self._discovered(fxc=fxc),
            ):
                report = replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            self.assertEqual("existing-unmanaged-config", report["source"])
            self.assertIn("predates", report["refreshBlockedReason"])
            self.assertEqual(b"user-tool-config", tool_path.read_bytes())
            self.assertEqual(b"user-route-config", route_path.read_bytes())

    def test_explicit_process_owner_registry_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool_path = root / "owner-tools.json"
            route_path = root / "owner-routes.json"
            tool_path.write_bytes(b"owner tools")
            route_path.write_bytes(b"owner routes")
            environment = {
                replaylab_tools.TOOLS_ENV: str(tool_path),
                replaylab_tools.ROUTES_ENV: str(route_path),
                "REPLAYLAB_CONFIG_DIR": str(root / "default-config"),
            }
            with mock.patch.object(replaylab_tools, "discover") as discover:
                report = replaylab_tools.configure_environment(root, environment)
            discover.assert_not_called()
            self.assertEqual("process-owner", report["source"])
            self.assertEqual(b"owner tools", tool_path.read_bytes())
            self.assertEqual(b"owner routes", route_path.read_bytes())
            self.assertFalse((root / "default-config").exists())

    def test_refresh_route_write_failure_rolls_back_old_pair_and_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            config = root / "config"
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc-v1")
            found = self._discovered(fxc=fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            paths = (
                config / "shader-tools.v1.json",
                config / "shader-toolchains.v1.json",
                config / "shader-toolchain-status.json",
            )
            previous = tuple(path.read_bytes() for path in paths)
            fxc.write_bytes(b"fxc-v2")
            real_atomic_write = replaylab_tools.atomic_write_bytes
            failed = False

            def fail_first_route_write(path, value):
                nonlocal failed
                if Path(path) == paths[1] and not failed:
                    failed = True
                    raise OSError("simulated route replace failure")
                return real_atomic_write(path, value)

            with mock.patch.object(replaylab_tools, "discover", return_value=found), mock.patch.object(
                replaylab_tools, "atomic_write_bytes", side_effect=fail_first_route_write
            ):
                with self.assertRaisesRegex(OSError, "simulated route"):
                    replaylab_tools.configure_environment(
                        host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                    )
            self.assertEqual(previous, tuple(path.read_bytes() for path in paths))

    def test_refresh_status_write_failure_rolls_back_old_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            config = root / "config"
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc-v1")
            found = self._discovered(fxc=fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            paths = (
                config / "shader-tools.v1.json",
                config / "shader-toolchains.v1.json",
                config / "shader-toolchain-status.json",
            )
            previous = tuple(path.read_bytes() for path in paths)
            fxc.write_bytes(b"fxc-v2")
            with mock.patch.object(replaylab_tools, "discover", return_value=found), mock.patch.object(
                replaylab_tools,
                "atomic_write_json",
                side_effect=OSError("simulated status replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated status"):
                    replaylab_tools.configure_environment(
                        host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                    )
            self.assertEqual(previous, tuple(path.read_bytes() for path in paths))

    def test_refresh_generation_failure_never_touches_live_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host"
            host.mkdir()
            config = root / "config"
            fxc = root / "fxc.exe"
            fxc.write_bytes(b"fxc-v1")
            found = self._discovered(fxc=fxc)
            with mock.patch.object(replaylab_tools, "discover", return_value=found):
                replaylab_tools.configure_environment(
                    host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                )
            paths = (
                config / "shader-tools.v1.json",
                config / "shader-toolchains.v1.json",
                config / "shader-toolchain-status.json",
            )
            previous = tuple(path.read_bytes() for path in paths)
            fxc.write_bytes(b"fxc-v2")
            with mock.patch.object(replaylab_tools, "discover", return_value=found), mock.patch.object(
                replaylab_tools,
                "generate_official_toolchain_configs",
                side_effect=RuntimeError("simulated staging failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated staging"):
                    replaylab_tools.configure_environment(
                        host, {"REPLAYLAB_CONFIG_DIR": str(config)}
                    )
            self.assertEqual(previous, tuple(path.read_bytes() for path in paths))


if __name__ == "__main__":
    unittest.main()
