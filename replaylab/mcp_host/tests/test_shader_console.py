from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "shader_console.py"
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "replaylab_shader_console", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


console = _load_module()


class ShaderConsoleFixture:
    AUTHORIZATION_ID = "a" * 32
    WORKSPACE_ID = "b" * 32
    LIVE_ID = "c" * 32
    WATCH_ID = "d" * 32
    CAPTURE_ID = "e" * 64
    RAW_SHA256 = "f" * 64

    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.source = self.workspace / "source" / "shader.hlsl"
        self.registry = root / "shader-live-edit-authorizations.v1.json"
        self.source.parent.mkdir(parents=True)
        (self.workspace / "watch").mkdir()
        (self.workspace / "live-edit").mkdir()
        (self.workspace / "ai").mkdir()
        self.source.write_text("float4 MainPS() : SV_Target\n{\n  return 1;\n}\n", encoding="utf-8")
        self._write(
            self.workspace / "manifest.json",
            {
                "schema": console.WORKSPACE_SCHEMA,
                "schemaVersion": 1,
                "workspaceId": self.WORKSPACE_ID,
                "capture": {"captureId": self.CAPTURE_ID},
                "shader": {
                    "rawSha256": self.RAW_SHA256,
                    "stage": "fragment",
                    "entryPoint": "MainPS",
                    "resourceId": 101,
                },
                "source": {
                    "path": "source/shader.hlsl",
                    "encoding": "utf-8",
                    "lane": "executable-editable",
                    "compileEligible": True,
                },
            },
        )
        self._write(
            self.registry,
            {
                "schema": console.REGISTRY_SCHEMA,
                "schemaVersion": 1,
                "entries": [
                    {
                        "authorizationId": self.AUTHORIZATION_ID,
                        "lastLiveEditId": self.LIVE_ID,
                        "workspacePath": str(self.workspace.resolve()),
                        "workspaceIdentitySha256": console._canonical_sha256(
                            {
                                "workspaceId": self.WORKSPACE_ID,
                                "captureId": self.CAPTURE_ID,
                                "capturePath": "C:/captures/test.rdc",
                                "shaderKey": "fragment:101:MainPS",
                                "shaderRawSha256": self.RAW_SHA256,
                                "stage": "fragment",
                                "entryPoint": "MainPS",
                                "resourceId": 101,
                            }
                        ),
                        "debounceMs": 100,
                        "authorizedAt": "2026-08-25T04:00:00Z",
                        "updatedAt": "2026-08-25T04:00:00Z",
                        "workspaceIdentity": {
                            "workspaceId": self.WORKSPACE_ID,
                            "captureId": self.CAPTURE_ID,
                            "capturePath": "C:/captures/test.rdc",
                            "shaderKey": "fragment:101:MainPS",
                            "shaderRawSha256": self.RAW_SHA256,
                            "stage": "fragment",
                            "entryPoint": "MainPS",
                            "resourceId": 101,
                        },
                    }
                ],
            },
        )
        self._write(
            self.workspace / "ai" / "organize-code-gate.json",
            {
                "schema": console.ORGANIZATION_GATE_SCHEMA,
                "schemaVersion": 1,
                "policyVersion": console.ORGANIZATION_POLICY_VERSION,
                "organizationId": "1" * 32,
                "reviewedAt": "2026-08-25T04:00:00Z",
                "requestedTransformation": "organize-only",
                "workspaceId": self.WORKSPACE_ID,
                "captureId": self.CAPTURE_ID,
                "stage": "fragment",
                "entryPoint": "MainPS",
                "baselineSourceSha256": console.hashlib.sha256(
                    self.source.read_bytes()
                ).hexdigest(),
                "proposalSourceSha256": console.hashlib.sha256(
                    self.source.read_bytes()
                ).hexdigest(),
                "baselineCodeSha256": "2" * 64,
                "organizedCodeSha256": "2" * 64,
                "decision": "accepted-executable-editable",
                "codeSha256Equal": True,
                "organizedSourcePromoted": True,
                "baselineCompiledAndAbiValidated": True,
                "organizedCompiledAndAbiValidated": True,
                "safeToStartLiveEdit": True,
                "replayApplied": False,
                "executableEditableSourcePath": "source/shader.hlsl",
                "finalSourceSha256": console.hashlib.sha256(
                    self.source.read_bytes()
                ).hexdigest(),
                "finalBinding": {
                    "complete": True,
                    "sourceSha256": console.hashlib.sha256(
                        self.source.read_bytes()
                    ).hexdigest(),
                    "revision": 7,
                    "candidateRawSha256": "3" * 64,
                    "codeSha256": "2" * 64,
                    "abiRequestId": "4" * 64,
                    "abiReportSha256": "5" * 64,
                },
                "finalRevision": 7,
                "finalCandidateRawSha256": "3" * 64,
                "finalCodeSha256": "2" * 64,
                "finalAbiRequestId": "4" * 64,
                "finalAbiReportSha256": "5" * 64,
            },
        )

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_watch(self, events=None, **changes):
        current_sha256 = console.hashlib.sha256(self.source.read_bytes()).hexdigest()
        gate = json.loads(
            (self.workspace / "ai" / "organize-code-gate.json").read_text(
                encoding="utf-8"
            )
        )
        value = {
            "schema": console.WATCH_SCHEMA,
            "schemaVersion": 1,
            "watchId": self.WATCH_ID,
            "workspacePath": str(self.workspace.resolve()),
            "sourcePath": str(self.source.resolve()),
            "state": "running",
            "compileInProgress": False,
            "pending": False,
            "caughtUp": True,
            "initialSourceSha256": gate["finalSourceSha256"],
            "observedSourceSha256": current_sha256,
            "lastAttemptedSourceSha256": current_sha256,
            "lastCompiledSourceSha256": current_sha256,
            "latestCandidateRevision": 2,
            "compileAttempts": 1,
            "compileSuccesses": 1,
            "compileFailures": 0,
            "events": events or [],
        }
        value.update(changes)
        self._write(self.workspace / "watch" / (self.WATCH_ID + ".json"), value)

    def write_live(self, events=None, **changes):
        value = {
            "schema": console.LIVE_EDIT_SCHEMA,
            "schemaVersion": 1,
            "liveEditId": self.LIVE_ID,
            "watchId": self.WATCH_ID,
            "workspacePath": str(self.workspace.resolve()),
            "state": "running",
            "applyAttempts": 1,
            "applySuccesses": 1,
            "applyRejections": 0,
            "applyFailures": 0,
            "activeReplacement": {"revision": 2},
            "events": events or [],
        }
        value.update(changes)
        self._write(self.workspace / "live-edit" / (self.LIVE_ID + ".json"), value)


class ShaderConsoleLogicTests(unittest.TestCase):
    def test_module_remains_python_36_parseable(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))

    def test_parses_dxc_colon_and_d3d_parenthesised_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            job_input = fixture.workspace / "jobs" / "compile" / "input.hlsl"
            message = (
                f"{job_input}:12:7: error: use of undeclared identifier 'bad'\n"
                f"{fixture.source}(22,3): warning X3550: array reference cannot be unrolled"
            )
            values = console.parse_compiler_diagnostics(
                message, str(fixture.source), str(fixture.workspace)
            )
            self.assertEqual(len(values), 2)
            self.assertEqual(values[0]["severity"], "error")
            self.assertEqual((values[0]["line"], values[0]["column"]), (12, 7))
            self.assertEqual(Path(values[0]["sourceFile"]), fixture.source.resolve())
            self.assertEqual(values[1]["severity"], "warning")
            self.assertEqual((values[1]["line"], values[1]["column"]), (22, 3))

    def test_compiler_text_cannot_redirect_source_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            outside = Path(temporary) / "outside.hlsl"
            outside.write_text("bad", encoding="utf-8")
            values = console.parse_compiler_diagnostics(
                f"{outside}:9:2: error: malicious diagnostic path",
                str(fixture.source), str(fixture.workspace),
            )
            self.assertEqual(Path(values[0]["sourceFile"]), fixture.source.resolve())
            self.assertEqual(values[0]["line"], 9)
            self.assertEqual(Path(values[0]["reportedFile"]), outside)

    def test_compile_failure_becomes_red_locatable_console_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            watch = {
                "events": [
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "errorType": "ToolExecutionError",
                        "message": "input.hlsl:3:10: error: expected expression",
                    }
                ]
            }
            entries = console.console_entries_from_audits(
                str(fixture.workspace), str(fixture.source), watch=watch
            )
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["severity"], "error")
            self.assertEqual(entries[0]["phase"], "编译")
            self.assertEqual(entries[0]["line"], 3)
            self.assertEqual(entries[0]["column"], 10)
            self.assertEqual(Path(entries[0]["sourceFile"]), fixture.source.resolve())

    def test_structured_watcher_diagnostic_precedes_message_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            watch = {
                "events": [
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "message": "generic compiler failure without a location",
                        "diagnostics": [
                            {
                                "file": str(fixture.source),
                                "line": 4,
                                "column": 2,
                                "severity": "error",
                                "message": "expected '}'",
                            }
                        ],
                    }
                ]
            }
            # This helper normally receives already-sanitised events from the
            # loader; directly supply the equivalent structured projection.
            entries = console.console_entries_from_audits(
                str(fixture.workspace), str(fixture.source), watch=watch
            )
            self.assertEqual((entries[0]["line"], entries[0]["column"]), (4, 2))
            self.assertEqual(entries[0]["text"], "expected '}'")

    def test_actual_watcher_stop_requested_event_is_visible(self):
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            watch={
                "watchId": "d" * 32,
                "events": [{
                    "sequence": 7,
                    "at": "2026-08-25T04:00:00Z",
                    "type": "stopRequested",
                }],
            },
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["eventType"], "stopRequested")
        self.assertEqual(entries[0]["severity"], "warning")
        self.assertIn("正在停止", entries[0]["text"])

    def test_diagnostic_ids_are_stable_unique_and_do_not_borrow_sequences(self):
        watch = {
            "watchId": "d" * 32,
            "events": [
                {
                    "sequence": 1,
                    "at": "2026-08-25T04:00:00Z",
                    "type": "compileFailed",
                    "message": "compile failed",
                    "diagnostics": [
                        {
                            "file": "input.hlsl",
                            "line": 3,
                            "column": 2,
                            "severity": "error",
                            "message": "expected expression",
                        },
                        {
                            "file": "input.hlsl",
                            "line": 3,
                            "column": 2,
                            "severity": "error",
                            "message": "expected expression",
                        },
                    ],
                },
                {
                    "sequence": 100,
                    "at": "2026-08-25T04:00:01Z",
                    "type": "watchStopped",
                },
            ],
        }
        first = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl", watch=watch)
        second = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl", watch=watch)
        self.assertEqual([item["id"] for item in first],
                         [item["id"] for item in second])
        self.assertEqual(len({item["id"] for item in first}), 3)
        diagnostics = [item for item in first if item["eventType"] == "compileFailed"]
        self.assertEqual([item["sequence"] for item in diagnostics], [1, 1])
        self.assertEqual([item["cursorSequence"] for item in diagnostics], [1, 1])
        self.assertTrue(all(":diagnostic:" in item["id"] for item in diagnostics))
        stopped = next(item for item in first if item["eventType"] == "watchStopped")
        self.assertTrue(stopped["id"].endswith(":100"))

    def test_progress_distinguishes_detection_compile_apply_and_success(self):
        self.assertEqual(
            console.derive_console_status({"pending": True}, None)["phase"], "检测"
        )
        self.assertEqual(
            console.derive_console_status({"compileInProgress": True}, None)["phase"],
            "编译",
        )
        applying = {
            "state": "running",
            "events": [
                {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "applyStarted"}
            ],
        }
        self.assertEqual(
            console.derive_console_status({}, applying)["phase"], "ABI / 应用"
        )
        succeeded = dict(applying)
        succeeded["events"] = [
            {"sequence": 2, "at": "2026-08-25T04:00:01Z", "type": "applySucceeded"}
        ]
        succeeded["activeRevision"] = 4
        status = console.derive_console_status({}, succeeded)
        self.assertEqual(status["phase"], "完成")
        self.assertIn("revision 4", status["text"])

    def test_apply_reused_status_uses_processed_revision_not_replacement_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.write_watch(latestCandidateRevision=11)
            fixture.write_live(
                activeReplacement={"revision": 8},
                latestHandledRevision=11,
                applyReuses=1,
                events=[
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "applyReused",
                        "revision": 11,
                    }
                ],
            )
            snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(snapshot["errors"], [])
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["liveEdit"]["activeRevision"], 8)
            self.assertEqual(workspace["liveEdit"]["latestHandledRevision"], 11)
            status = workspace["status"]
            self.assertEqual(status["phase"], "完成")
            self.assertIn("revision 11", status["text"])
            self.assertNotIn("revision 8", status["text"])

    def test_superseded_candidate_apply_failures_are_warnings_only(self):
        messages = (
            "Shader source changed after the candidate compile; wait for a fresh compile before validation/apply",
            "Shader workspace no longer contains the expected saved candidate",
        )
        for index, message in enumerate(messages, 1):
            with self.subTest(message=message):
                event = {
                    "sequence": index,
                    "at": "2026-08-25T04:00:0%dZ" % index,
                    "type": "applyFailed",
                    "revision": 10 + index,
                    "message": message,
                }
                entries = console.console_entries_from_audits(
                    "C:/workspace", "C:/workspace/source/shader.hlsl",
                    live={"events": [event]},
                )
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["severity"], "warning")
                self.assertEqual(entries[0]["phase"], "应用队列")
                self.assertIn("新的保存取代", entries[0]["text"])

                status = console.derive_console_status(
                    {}, {"state": "running", "events": [event]}
                )
                self.assertEqual(status["severity"], "warning")
                self.assertEqual(status["phase"], "应用队列")

    def test_unrelated_apply_failure_remains_a_red_error(self):
        event = {
            "sequence": 1,
            "at": "2026-08-25T04:00:00Z",
            "type": "applyFailed",
            "revision": 12,
            "message": "D3D12 rejected replacement target creation",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["phase"], "ABI / 应用")

        status = console.derive_console_status(
            {}, {"state": "running", "events": [event]}
        )
        self.assertEqual(status["severity"], "error")
        self.assertEqual(status["phase"], "错误")

    def test_runtime_failure_without_compiler_location_is_not_fake_line_one(self):
        event = {
            "sequence": 1,
            "at": "2026-08-25T04:00:00Z",
            "type": "liveEditFailed",
            "message": "could not persist live-edit audit state: access denied",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["sourceFile"], "")
        self.assertEqual(entries[0]["line"], 0)
        self.assertEqual(entries[0]["column"], 0)

    def test_persistent_authorization_revoke_failure_is_visible_without_source(self):
        event = {
            "sequence": 9,
            "at": "2026-08-25T04:00:00Z",
            "type": "persistentAuthorizationRevokeFailed",
            "errorType": "PermissionError",
            "message": "authorization registry remained durable",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["phase"], "授权")
        self.assertIn("registry remained durable", entries[0]["text"])
        self.assertEqual(entries[0]["sourceFile"], "")
        self.assertEqual(entries[0]["line"], 0)

    def test_three_unity_severities_counts_filters_and_clear_watermark(self):
        entries = [
            {"streamKey": "a", "cursorSequence": 1, "severity": "progress"},
            {"streamKey": "a", "cursorSequence": 2, "severity": "warning"},
            {"streamKey": "b", "cursorSequence": 1, "severity": "error"},
            {"streamKey": "b", "cursorSequence": 2, "severity": "success"},
        ]
        self.assertEqual(
            console.console_severity_counts(entries),
            {"info": 2, "warning": 1, "error": 1},
        )
        filtered = console.filter_console_entries(
            entries, {"info": False, "warning": True, "error": True}
        )
        self.assertEqual(len(filtered), 2)
        cursors = console.build_console_clear_cursors(entries)
        self.assertEqual(console.console_entries_after_clear(entries, cursors), [])
        next_event = {
            "streamKey": "a", "cursorSequence": 3, "severity": "info"
        }
        self.assertEqual(
            console.console_entries_after_clear(entries + [next_event], cursors),
            [next_event],
        )

    def test_fixed_generic_journal_publishes_and_loads_without_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                ordinary = console.publish_console_event(
                    "project", "progress", "正在准备工程", phase="工程",
                    code="projectPreparing",
                    context={
                        "operation": "prepare",
                        "projectId": "project-1",
                        "sourcePath": "C:/must/not/be/trusted.hlsl",
                        "command": "notepad.exe",
                    },
                    process_role="host",
                )
                console.publish_console_event(
                    "capture", "warning", "捕获身份需要重新确认",
                    phase="捕获", process_role="host",
                )
                console.publish_console_event(
                    "material", "error", "材质更新失败",
                    phase="材质", process_role="host",
                )
                directory = console.default_console_journal_directory()
                loaded = console._load_generic_console_entries()

            self.assertEqual(ordinary["severity"], "info")
            self.assertNotIn("sourcePath", ordinary["context"])
            self.assertNotIn("command", ordinary["context"])
            self.assertEqual(loaded["errors"], [])
            self.assertEqual(
                console.console_severity_counts(loaded["entries"]),
                {"info": 1, "warning": 1, "error": 1},
            )
            self.assertEqual(
                [entry["module"] for entry in loaded["entries"]],
                ["project", "capture", "material"],
            )
            self.assertTrue(all(not entry["sourceFile"] for entry in loaded["entries"]))
            files = list(Path(directory).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            before = files[0].read_bytes()
            cursors = console.build_console_clear_cursors(loaded["entries"])
            self.assertEqual(
                console.console_entries_after_clear(loaded["entries"], cursors), []
            )
            self.assertEqual(files[0].read_bytes(), before)

    @staticmethod
    def _write_console_journal(directory, session, pid, events, mtime, segment=0):
        path = directory / (
            "events.host.%d.%s.%04d.jsonl" % (pid, session, segment))
        rows = []
        for sequence, unix_ms, message in events:
            rows.append(json.dumps({
                "schema": console.JOURNAL_EVENT_SCHEMA,
                "schemaVersion": 1,
                "sessionId": session,
                "sequence": sequence,
                "unixMs": unix_ms,
                "at": "2026-08-25T04:00:%02dZ" % (sequence % 60),
                "processRole": "host",
                "processId": pid,
                "module": "host",
                "severity": "info",
                "phase": "测试",
                "code": "boundedRead",
                "message": message,
                "context": {},
            }, ensure_ascii=False, separators=(",", ":")))
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        os.utime(str(path), (mtime, mtime))
        return path

    def test_journal_reader_prefers_latest_files_and_has_global_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                for index in range(6):
                    self._write_console_journal(
                        directory, ("%032x" % (index + 1)), 100 + index,
                        [(1, 1000 + index, "event-%d" % index)],
                        1000 + index,
                    )
                with mock.patch.object(console, "MAX_JOURNAL_READ_FILES", 3), \
                        mock.patch.object(console, "MAX_JOURNAL_READ_BYTES", 4096), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_READ_BYTES_PER_FILE", 4096):
                    loaded = console._load_generic_console_entries()
            self.assertEqual(loaded["errors"], [])
            self.assertEqual(
                {item["text"] for item in loaded["entries"]},
                {"event-3", "event-4", "event-5"},
            )
            self.assertLessEqual(len(loaded["watchedPaths"]), 4)

    def test_journal_reader_reads_complete_latest_tail_records_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                events = [
                    (index, 1000 + index, "event-%02d-%s" % (index, "x" * 80))
                    for index in range(1, 31)
                ]
                self._write_console_journal(
                    directory, "1" * 32, 101, events, 2000)
                with mock.patch.object(console, "MAX_JOURNAL_READ_BYTES", 1200), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_READ_BYTES_PER_FILE", 1200):
                    loaded = console._load_generic_console_entries()
            self.assertEqual(loaded["errors"], [])
            self.assertGreater(len(loaded["entries"]), 0)
            self.assertLess(len(loaded["entries"]), len(events))
            self.assertEqual(loaded["entries"][-1]["sequence"], 30)
            self.assertTrue(all(item["text"].startswith("event-")
                                for item in loaded["entries"]))

    def test_journal_retention_only_removes_strict_old_generic_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                paths = []
                for index in range(4):
                    paths.append(self._write_console_journal(
                        directory, ("%032x" % (index + 1)), 200 + index,
                        [(1, 2000 + index, "event-%d" % index)], 2000 + index))
                invalid = directory / "do-not-delete.txt"
                invalid.write_text("not a ReplayLab segment", encoding="utf-8")
                with mock.patch.object(console, "MAX_JOURNAL_RETENTION_FILES", 2), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_RETENTION_BYTES", 1024 * 1024):
                    removed = console._enforce_console_journal_retention(
                        str(directory), protected_path=str(paths[-1]))
                remaining = list(directory.glob("events.*.jsonl"))
                self.assertEqual(len(remaining), 2)
                self.assertTrue(paths[-1].exists())
                self.assertTrue(invalid.exists())
                self.assertEqual(removed, 2)

                outside = Path(temporary) / "outside"
                with self.assertRaises(console.ShaderConsoleError):
                    console._enforce_console_journal_retention(str(outside))

    def test_journal_reader_periodically_enforces_retention_for_host_only_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                for index in range(4):
                    self._write_console_journal(
                        directory, ("%032x" % (index + 20)), 300 + index,
                        [(1, 3000 + index, "event-%d" % index)], 3000 + index)
                with mock.patch.object(console, "MAX_JOURNAL_RETENTION_FILES", 2), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_RETENTION_BYTES", 1024 * 1024), \
                        mock.patch.object(
                            console, "_JOURNAL_LAST_RETENTION_MONOTONIC", 0.0):
                    loaded = console._load_generic_console_entries()
                self.assertEqual(loaded["errors"], [])
                self.assertEqual(len(list(directory.glob("events.*.jsonl"))), 2)

    def test_snapshot_aggregates_generic_and_authorized_shader_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = ShaderConsoleFixture(root)
            fixture.write_watch(events=[{
                "sequence": 1,
                "at": "2026-08-25T04:00:00Z",
                "type": "watchStarted",
            }])
            fixture.write_live(events=[])
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                console.publish_console_event(
                    "pass", "info", "Pass 树已生成", phase="Pass",
                    process_role="qrenderdoc",
                )
                snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(snapshot["errors"], [])
            self.assertEqual(len(snapshot["entries"]), 2)
            self.assertEqual(
                {entry["module"] for entry in snapshot["entries"]},
                {"pass", "shader"},
            )

    def test_loader_uses_authorized_workspace_and_exact_audit_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.write_watch(
                events=[
                    {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "watchStarted"},
                    {"sequence": 2, "at": "2026-08-25T04:00:01Z", "type": "compileStarted", "attempt": 1},
                    {"sequence": 3, "at": "2026-08-25T04:00:02Z", "type": "compileSucceeded", "revision": 2},
                ]
            )
            fixture.write_live(
                events=[
                    {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "liveEditStarted"},
                    {"sequence": 2, "at": "2026-08-25T04:00:03Z", "type": "applyStarted", "revision": 2},
                    {"sequence": 3, "at": "2026-08-25T04:00:04Z", "type": "applySucceeded", "revision": 2},
                ]
            )
            snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(snapshot["errors"], [])
            self.assertEqual(len(snapshot["workspaces"]), 1)
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["watch"]["watchId"], fixture.WATCH_ID)
            self.assertEqual(workspace["liveEdit"]["liveEditId"], fixture.LIVE_ID)
            self.assertEqual(workspace["status"]["phase"], "完成")
            self.assertEqual(
                [item["eventType"] for item in workspace["entries"]],
                [
                    "watchStarted", "liveEditStarted", "compileStarted",
                    "compileSucceeded", "applyStarted", "applySucceeded",
                ],
            )

    def test_loader_recovers_line_diagnostics_from_manifest_bound_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            source_sha256 = "1" * 64
            job = fixture.workspace / "jobs" / "compile-h01-r0001-test"
            job.mkdir(parents=True)
            stderr = job / "stderr.log"
            stderr.write_text(
                f"{job / 'input.hlsl'}:3:8: error: expected ';' after expression\n",
                encoding="utf-8",
            )
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["history"] = [
                {
                    "operation": "compile-route-chain",
                    "status": "failed",
                    "hops": [
                        {
                            "operation": "compile",
                            "status": "failed",
                            "inputSha256": source_sha256,
                            "stderrPath": str(stderr.relative_to(fixture.workspace)).replace("\\", "/"),
                        }
                    ],
                }
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            fixture.write_watch(
                events=[
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "errorType": "ToolExecutionError",
                        "message": "external tool exited with code 5; see authorized job",
                        "requestedSourceSha256": source_sha256,
                    }
                ],
                compileFailures=1,
                compileSuccesses=0,
                caughtUp=False,
            )
            fixture.write_live(events=[])
            snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(snapshot["errors"], [])
            error = snapshot["workspaces"][0]["entries"][0]
            self.assertEqual(error["severity"], "error")
            self.assertEqual((error["line"], error["column"]), (3, 8))
            self.assertIn("expected ';'", error["text"])
            self.assertEqual(Path(error["sourceFile"]), fixture.source.resolve())

    def test_loader_fails_closed_when_authorized_identity_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["shader"]["resourceId"] = 999
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(snapshot["workspaces"], [])
            self.assertTrue(any("identity" in error for error in snapshot["errors"]))

    def test_authorized_hlsl_document_loads_only_the_organized_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID
            )
            self.assertEqual(Path(opened["sourcePath"]), fixture.source.resolve())
            self.assertEqual(opened["entryPoint"], "MainPS")
            self.assertIn("return 1", opened["text"])

    def test_embedded_process_direct_save_path_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            before = fixture.source.read_bytes()
            with self.assertRaisesRegex(console.ShaderConsoleError, "running ReplayLab Host"):
                console.save_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID, console.hashlib.sha256(before).hexdigest(),
                    before.decode("utf-8") + "// edit\\n",
                )
            self.assertEqual(fixture.source.read_bytes(), before)

    def test_post_organization_source_requires_running_watcher_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.source.write_text(
                "float4 MainPS() : SV_Target { return 2; }\\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                console.ShaderConsoleError, "unavailable|not running"
            ):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                )
            fixture.write_watch()
            fixture.write_live()
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID,
            )
            self.assertIn("return 2", opened["text"])

    def test_unrelated_deleted_authorization_does_not_block_exact_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            registry = json.loads(fixture.registry.read_text(encoding="utf-8"))
            stale = dict(registry["entries"][0])
            stale["authorizationId"] = "6" * 32
            stale["workspacePath"] = str(fixture.root / "deleted-workspace")
            stale_identity = dict(stale["workspaceIdentity"])
            stale_identity["workspaceId"] = "7" * 32
            stale["workspaceIdentity"] = stale_identity
            stale["workspaceIdentitySha256"] = console._canonical_sha256(stale_identity)
            registry["entries"].insert(0, stale)
            fixture.registry.write_text(json.dumps(registry), encoding="utf-8")
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID,
            )
            self.assertEqual(opened["workspaceId"], fixture.WORKSPACE_ID)

    def test_exact_production_registry_identity_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            registry = json.loads(fixture.registry.read_text(encoding="utf-8"))
            registry["entries"][0]["workspaceIdentitySha256"] = "0" * 64
            fixture.registry.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "identity hash"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                )


    def test_code_workspace_rejects_non_hlsl_authorized_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            glsl_source = fixture.source.with_name("shader.glsl")
            fixture.source.replace(glsl_source)
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["path"] = "source/shader.glsl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "not HLSL"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID
                )

    def test_code_workspace_requires_the_current_strict_organization_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["safeToStartLiveEdit"] = False
            gate["decision"] = "awaiting-proposal-review"
            gate_path.write_text(json.dumps(gate), encoding="utf-8")

            snapshot = console.load_console_snapshot(str(fixture.registry))
            self.assertEqual(len(snapshot["workspaces"]), 1)
            self.assertFalse(snapshot["workspaces"][0]["editorEligible"])
            self.assertEqual(
                snapshot["workspaces"][0]["editorEligibility"],
                "organization-gate-unavailable",
            )
            with self.assertRaisesRegex(console.ShaderConsoleError, "organization gate"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                )

    def test_code_workspace_rejects_non_executable_source_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["lane"] = "explanatory-ir"
            manifest["source"]["compileEligible"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "executable editable"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                )

    def test_ui_projection_has_no_shell_or_process_execution_surface(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "subprocess", "os.system", "Popen(", "ShellExecute", "QProcess",
            "startDetached",
        ):
            self.assertNotIn(forbidden, source)

    def test_bridge_registers_console_without_forcing_process_power_policy(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        self.assertIn("_register_shader_console_ui(ctx)", source)
        self.assertIn("_unregister_shader_console_ui()", source)
        self.assertIn("ReplayLabConsolePanel", source)
        self.assertIn("[\"ReplayLab\", \"Console\"]", source)
        self.assertIn("[\"ReplayLab Console\"]", source)
        self.assertIn("[\"ReplayLab\", \"Shader Console\"]", source)
        self.assertIn("publish_console_event", source)
        self.assertNotIn(
            "_shader_console_protocol.disable_background_power_throttling()",
            source,
        )
        self.assertNotIn("disable_background_power_throttling", MODULE_PATH.read_text(
            encoding="utf-8"))

    def test_external_ipc_poll_is_bounded_and_not_frame_rate(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BRIDGE_PATH), feature_version=(3, 6))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "IPC_POLL_INTERVAL_MS"
                    for target in node.targets)
        )
        interval = ast.literal_eval(assignment.value)
        self.assertGreaterEqual(interval, 250)

    def test_bridge_quiets_fixed_background_polls_but_keeps_all_failures(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BRIDGE_PATH), feature_version=(3, 6))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "CONSOLE_QUIET_REQUEST_METHODS"
                    for target in node.targets)
        )
        quiet = set(ast.literal_eval(assignment.value.args[0]))
        self.assertIn("ping", quiet)
        self.assertIn("get_current_renderdoc_context", quiet)
        self.assertNotIn("claim_renderdoc_ui_intents", quiet)
        self.assertNotIn("apply_shader_candidate_shared_resource", quiet)
        self.assertIn("if lifecycle_visible:", source)
        self.assertIn('code="requestFailed"', source)
        self.assertIn('code="requestRejected"', source)

    def test_ui_filesystem_notifications_are_short_coalesced_not_immediate(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("FILESYSTEM_COALESCE_MS = 20", source)
        self.assertIn("self.change_timer.setSingleShot(True)", source)
        self.assertIn("self.change_timer.timeout.connect(self._flush_filesystem_changes)", source)
        self.assertIn("if not self.change_timer.isActive():", source)
        self.assertIn("self.timer.setInterval(self.POLL_INTERVAL_MS)", source)

    def test_console_windows_reader_requests_delete_sharing(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("CreateFileW", source)
        self.assertIn("0x1 | 0x2 | 0x4", source)


if __name__ == "__main__":
    unittest.main()
