import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "renderdoc_mcp_bridge"
    / "project_restore_client.py"
)
SPEC = importlib.util.spec_from_file_location(
    "replaylab_project_restore_client_test", MODULE_PATH
)
client_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(client_module)


class _Response:
    def __init__(self, value):
        self.payload = json.dumps(value).encode("utf-8")
        self.closed = False

    def read(self, maximum=-1):
        return self.payload if maximum < 0 else self.payload[:maximum]

    def close(self):
        self.closed = True


def _job(status, progress=None, **extra):
    value = {
        "schema": client_module.RESTORE_JOB_SCHEMA,
        "jobId": "a" * 32,
        "status": status,
        "latestSequence": max(
            [item["sequence"] for item in (progress or [])] or [0]
        ),
        "progress": list(progress or []),
    }
    value.update(extra)
    return value


class ReplayLabRestoreHostClientTests(unittest.TestCase):
    def _runtime(self, root):
        runtime = Path(root) / "runtime"
        runtime.mkdir()
        token_path = runtime / "host.token"
        token_path.write_text("T" * 48, encoding="ascii")
        (runtime / "host.json").write_text(
            json.dumps(
                {
                    "schema": "replaylab-host/v1",
                    "service": "ReplayLab",
                    "host": "127.0.0.1",
                    "port": 49643,
                    "tokenFile": str(token_path),
                }
            ),
            encoding="utf-8",
        )
        return runtime

    def test_restore_starts_authenticated_job_and_streams_ordered_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            capture = Path(temporary) / "frame.rdc"
            project = Path(temporary) / "project"
            capture.write_bytes(b"rdc")
            project.mkdir()
            replies = [
                _job("queued"),
                _job(
                    "running",
                    [
                        {
                            "sequence": 1,
                            "phase": "index",
                            "message": "Reading RDC index",
                            "current": 0,
                            "total": 0,
                        },
                        {
                            "sequence": 2,
                            "phase": "shader",
                            "message": "Applying shared Shader",
                            "current": 1,
                            "total": 2,
                        },
                    ],
                ),
                _job(
                    "completed",
                    [
                        {
                            "sequence": 3,
                            "phase": "finish",
                            "message": "Project restore completed",
                            "current": 1,
                            "total": 1,
                        }
                    ],
                    result={
                        "schema": "replaylab-project-restore/v1",
                        "completed": True,
                    },
                ),
            ]
            requests = []

            def opener(request, timeout):
                requests.append((request, timeout))
                return _Response(replies.pop(0))

            progress = []
            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), opener=opener, sleep=lambda unused: None
            )
            result = client.restore(
                str(project), str(capture), "b" * 32, progress.append
            )

            self.assertTrue(result["completed"])
            self.assertEqual(["index", "shader", "finish"], [
                value["phase"] for value in progress
            ])
            self.assertEqual("POST", requests[0][0].get_method())
            self.assertEqual("GET", requests[1][0].get_method())
            self.assertIn("after=0", requests[1][0].full_url)
            self.assertIn("after=2", requests[2][0].full_url)
            self.assertEqual(
                "Bearer " + "T" * 48,
                requests[0][0].get_header("Authorization"),
            )
            payload = json.loads(requests[0][0].data.decode("utf-8"))
            self.assertEqual(os.path.abspath(str(project)), payload["projectRoot"])
            self.assertEqual(os.path.abspath(str(capture)), payload["capturePath"])
            self.assertEqual("b" * 32, payload["captureSessionId"])

    def test_failed_job_surfaces_bounded_host_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            replies = [_job("queued"), _job("failed", error="ABI mismatch")]

            def opener(unused_request, timeout):
                del unused_request, timeout
                return _Response(replies.pop(0))

            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), opener=opener, sleep=lambda unused: None
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "ABI mismatch"
            ):
                client.restore(
                    str(Path(temporary) / "project"),
                    str(Path(temporary) / "frame.rdc"),
                    "c" * 32,
                )

    def test_non_monotonic_progress_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(temporary)
            replies = [
                _job("queued"),
                _job(
                    "running",
                    [
                        {
                            "sequence": 2,
                            "phase": "one",
                            "message": "one",
                            "current": 0,
                            "total": 0,
                        },
                        {
                            "sequence": 2,
                            "phase": "two",
                            "message": "two",
                            "current": 0,
                            "total": 0,
                        },
                    ],
                ),
            ]

            def opener(unused_request, timeout):
                del unused_request, timeout
                return _Response(replies.pop(0))

            client = client_module.ReplayLabRestoreHostClient(
                str(runtime), opener=opener, sleep=lambda unused: None
            )
            with self.assertRaisesRegex(
                client_module.ReplayLabRestoreClientError, "progress is malformed"
            ):
                client.restore(
                    str(Path(temporary) / "project"),
                    str(Path(temporary) / "frame.rdc"),
                    "d" * 32,
                )

    def test_runtime_root_matches_configured_and_local_appdata_contracts(self):
        self.assertEqual(
            os.path.abspath("C:/custom/replaylab-runtime"),
            client_module.runtime_root(
                {"REPLAYLAB_RUNTIME_DIR": "C:/custom/replaylab-runtime"}
            ),
        )
        self.assertEqual(
            os.path.abspath("C:/Users/test/AppData/Local/ReplayLab/runtime"),
            client_module.runtime_root(
                {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}
            ),
        )


if __name__ == "__main__":
    unittest.main()
