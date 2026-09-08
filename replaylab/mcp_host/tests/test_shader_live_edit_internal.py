from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import threading
import time
from unittest import mock

import pytest

from src import internal_host, internal_tool_service as service_module, server
from src import shader_live_edit_client as client
from src.ipc_client import _CALL_BINDING, bind_direct_tool_call


def binding(root):
    return {"instanceId": "qrenderdoc-test", "captureSessionId": "b" * 32,
            "capturePath": str(root / "frame.rdc")}


def request_body(root, operation="start", parameters=None):
    if parameters is None:
        parameters = {"workspace_path": str(root), "debounce_ms": 100}
    return json.dumps({"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                       "requestId": "a" * 32, "operation": operation,
                       "parameters": parameters, "binding": binding(root)}).encode()


class Manager:
    def __init__(self):
        self.running = False
        self.calls = []

    def start(self, workspace_path, debounce_ms):
        self.calls.append(("start", _CALL_BINDING.get()))
        self.running = True
        return {"liveEditId": "1" * 32, "workspacePath": workspace_path}

    def list(self):
        return {"sessions": []}

    def poll(self, live_edit_id):
        return {"liveEditId": live_edit_id, "state": "running"}

    def stop(self, live_edit_id):
        self.running = False
        return {"liveEditId": live_edit_id, "state": "stopped"}

    def migrate_authorization(self, **parameters):
        return {"authorizationMigrated": True}

    def save_authorized_source(self, live, authorization, workspace, expected, text):
        assert self.running and live == "1" * 32
        self.calls.append(("save", live, text))
        return {"watchId": "2" * 32, "rawSha256": "3" * 64, "changed": True,
                "compileScheduled": True, "size": len(text), "modifiedNs": 123}


def context(root):
    return {"capture": {"path": binding(root)["capturePath"], "captureSessionId": "b" * 32}}


def test_singleton_start_and_editor_save_share_one_in_memory_session(tmp_path):
    manager = Manager()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, started = service.shader_live_edit_operation(request_body(tmp_path))
    assert status == 200 and started["result"]["liveEditId"] == "1" * 32
    assert manager.calls[0] == ("start", ("qrenderdoc-test", "b" * 32))
    save = {"liveEditId": "1" * 32, "authorizationId": "4" * 32, "workspaceId": "5" * 32,
            "expectedRawSha256": "6" * 64, "text": "float4 MainPS() { return 0; }"}
    status, saved = service.save_shader_source(json.dumps(save).encode())
    assert status == 200 and saved["compileScheduled"]
    assert manager.calls[-1] == ("save", "1" * 32, save["text"])
    assert _CALL_BINDING.get() is None


def test_wrong_capture_session_is_rejected_before_start(tmp_path):
    manager = Manager()
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    changed = context(tmp_path)
    changed["capture"]["captureSessionId"] = "c" * 32
    with mock.patch.object(server, "_active_live_context", return_value=changed):
        status, result = service.shader_live_edit_operation(request_body(tmp_path))
    assert status == 409 and "capture session changed" in result["error"]
    assert not manager.running


@pytest.mark.parametrize("operation,parameters", [
    ("list", {}), ("poll", {"live_edit_id": "1" * 32}), ("stop", {"live_edit_id": "1" * 32}),
])
def test_closed_lifecycle_operations_are_dispatched(tmp_path, operation, parameters):
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, result = service.shader_live_edit_operation(request_body(tmp_path, operation, parameters))
    assert status == 200 and isinstance(result["result"], dict)


def test_migration_is_one_explicit_fixed_operation(tmp_path):
    params = {"source_workspace_path": str(tmp_path / "old"), "workspace_path": str(tmp_path / "new"),
              "expected_source_sha256": "d" * 64}
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=Manager())
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        status, result = service.shader_live_edit_operation(request_body(tmp_path, "migrate", params))
    assert status == 200 and result["result"]["authorizationMigrated"]


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(operation="tools/call"),
    lambda v: v["parameters"].update(command="arbitrary"),
    lambda v: v["binding"].update(instanceId="../foreign-instance"),
    lambda v: v["parameters"].update(debounce_ms=True),
])
def test_parser_rejects_generic_calls_extra_fields_and_bad_bindings(tmp_path, mutation):
    value = json.loads(request_body(tmp_path))
    mutation(value)
    with pytest.raises(ValueError):
        service_module.parse_shader_live_edit_operation(json.dumps(value).encode())


def test_worker_reenters_frozen_capture_binding_on_its_own_thread(tmp_path):
    key = os.path.normcase(str(tmp_path.resolve()))
    seen = []
    def apply(*args):
        seen.append(_CALL_BINDING.get())
        return {"applied": True}
    frozen = service_module._freeze_shader_live_session("qrenderdoc-test", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc"))
    with mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        with mock.patch.object(server, "_shader_live_edit_apply_candidate", side_effect=apply):
            # Rebinding the same workspace's next job cannot retarget this one.
            with mock.patch.dict(service_module._SHADER_LIVE_EDIT_BINDINGS, {key: object()}, clear=True):
                worker = threading.Thread(target=frozen.apply_candidate, args=(str(tmp_path), 2, "c" * 64))
                worker.start()
                worker.join(5)
    assert seen == [("qrenderdoc-test", "b" * 32)] and _CALL_BINDING.get() is None


@pytest.mark.parametrize("provide_status", [False, True])
def test_durable_identity_projection_reuses_only_the_supplied_workspace_status(tmp_path, provide_status):
    status = {"workspaceId": "a" * 32,
              "capture": {"captureId": "b" * 64, "capturePath": str(tmp_path / "frame.rdc")},
              "shader": {"shaderKey": "77:fragment:MainPS:" + "c" * 64,
                         "rawSha256": "c" * 64, "resourceId": 77},
              "stage": "fragment", "entryPoint": "MainPS"}
    workspace_service = mock.Mock()
    workspace_service.status.return_value = status
    with mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service):
        identity = server._shader_live_edit_workspace_identity(
            str(tmp_path), **({"status": status} if provide_status else {}))
    assert identity == {"workspaceId": "a" * 32, "captureId": "b" * 64,
                        "capturePath": str(tmp_path / "frame.rdc"),
                        "shaderKey": "77:fragment:MainPS:" + "c" * 64,
                        "shaderRawSha256": "c" * 64, "resourceId": 77,
                        "stage": "fragment", "entryPoint": "MainPS"}
    if provide_status:
        workspace_service.status.assert_not_called()
    else:
        workspace_service.status.assert_called_once_with(str(tmp_path))


def test_start_binding_checks_catalog_in_the_discovered_scope_before_publication(tmp_path):
    from src import ipc_client
    (tmp_path / "qrenderdoc-test").mkdir()
    identity = {"capturePath": str(tmp_path / "frame.rdc"), "captureId": "c" * 64}
    manifest = {"captureLoaded": True, "captureSessionId": "b" * 32, "capturePath": identity["capturePath"],
                "pid": os.getpid(), "instanceId": "qrenderdoc-test"}
    (tmp_path / "qrenderdoc-test" / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    status = {"fixture": "one immutable status"}
    workspace_service = mock.Mock()
    workspace_service.status.return_value = status

    def validate(workspace, *, status):
        assert workspace == str(tmp_path)
        assert status is workspace_service.status.return_value
        assert _CALL_BINDING.get() == ("qrenderdoc-test", "b" * 32)
        assert service_module._SHADER_LIVE_EDIT_BINDINGS == {}
        return {"capture": {"path": identity["capturePath"],
                            "captureId": identity["captureId"], "captureSessionId": "b" * 32}}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", return_value=manifest), \
            mock.patch.object(server, "_shader_live_edit_workspace_identity", return_value=identity), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_workspace_active_capture_context", side_effect=validate) as verified, \
            mock.patch.dict(service_module._SHADER_LIVE_EDIT_BINDINGS, {}, clear=True):
        assert service_module._bound_shader_workspace_identity(str(tmp_path)) == identity
        frozen = service_module._bound_shader_session(str(tmp_path))
        assert frozen.binding == binding(tmp_path)
        verified.assert_called_once_with(str(tmp_path), status=status)
    assert _CALL_BINDING.get() is None
    workspace_service.status.assert_called_once_with(str(tmp_path))


@pytest.mark.parametrize("mutation,expected", [
    ({"heartbeatUnixMs": 1}, "current"),  # A busy UI is not a dead owner.
    ({"captureSessionId": "c" * 32}, "expired"),
    ({"capturePath": "other.rdc"}, "expired"),
    ({"captureLoaded": False}, "expired"),
    ({"stopped": True}, "expired"),
    ({"captureSessionId": "bad"}, "unavailable"),
    ({"instanceId": "different"}, "unavailable"),
])
def test_capture_session_state_uses_process_and_session_not_heartbeat(tmp_path, mutation, expected):
    from src import ipc_client

    scope = binding(tmp_path)
    mailbox = tmp_path / scope["instanceId"]
    mailbox.mkdir()
    manifest = {**scope, "pid": os.getpid(), "captureLoaded": True, "heartbeatUnixMs": 1}
    manifest.update(mutation)
    (mailbox / "instance.json").write_text(json.dumps(manifest), encoding="utf-8")
    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)):
        assert service_module._shader_capture_session_state(scope, os.getpid()) == expected


def test_unreadable_mailbox_is_unknown_until_owner_process_has_exited(tmp_path):
    from src import ipc_client

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)):
        assert service_module._shader_capture_session_state(binding(tmp_path), os.getpid()) == "unavailable"
        with mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=False):
            assert service_module._shader_capture_session_state(binding(tmp_path), 9876) == "expired"


def test_expired_frozen_worker_never_enters_apply(tmp_path):
    frozen = service_module._freeze_shader_live_session("qrenderdoc-test", "b" * 32, os.getpid(), str(tmp_path / "frame.rdc"))
    with mock.patch.object(service_module, "_shader_capture_session_state", return_value="expired"):
        with mock.patch.object(server, "_shader_live_edit_apply_candidate") as apply:
            with pytest.raises(RuntimeError, match="no longer current"):
                frozen.apply_candidate(str(tmp_path), 2, "c" * 64)
    apply.assert_not_called()


@pytest.mark.parametrize("same_gui_process", [False, True])
def test_existing_owner_tick_resumes_new_capture_without_any_mcp_list(tmp_path, same_gui_process):
    from src import ipc_client
    from src.shader_live_edit import ShaderLiveEditManager
    from tests.test_shader_live_edit import _wait_until
    from tests.test_shader_live_edit_lifecycle import _PersistentWatchManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capture_path = str(tmp_path / "frame.rdc")
    identity = {"workspaceId": "a" * 32, "captureId": "b" * 64, "capturePath": capture_path,
                "shaderKey": "shader", "shaderRawSha256": "c" * 64, "resourceId": 77,
                "stage": "fragment", "entryPoint": "MainPS"}
    mailboxes = tmp_path / "instances"
    mailboxes.mkdir()
    alive = {101}

    def publish(instance, pid, session):
        directory = mailboxes / instance
        directory.mkdir(exist_ok=True)
        record = {"instanceId": instance, "pid": pid, "captureSessionId": session,
                  "capturePath": capture_path, "captureLoaded": True,
                  "heartbeatUnixMs": int(time.time() * 1000)}
        (directory / "instance.json").write_text(json.dumps(record), encoding="utf-8")
        return {key: record[key] for key in ("instanceId", "captureSessionId", "capturePath")}

    old_scope = publish("old-gui", 101, "1" * 32)
    watch = _PersistentWatchManager()
    manager = ShaderLiveEditManager(
        watch, lambda *_: pytest.fail("unbound apply"), registry_path=tmp_path / "registry.json",
        workspace_identity=service_module._bound_shader_workspace_identity,
        bind_session=service_module._bound_shader_session, poll_seconds=0.005, join_seconds=0.2)
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    workspace_service = mock.Mock()

    def validate_catalog(_workspace, *, status):
        assert status is workspace_service.status.return_value
        scope = _CALL_BINDING.get()
        assert scope is not None
        return {"capture": {"captureId": identity["captureId"],
                            "path": capture_path, "captureSessionId": scope[1]}}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(mailboxes)), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", side_effect=lambda pid: pid in alive), \
            mock.patch.object(server, "_shader_live_edit_workspace_identity", return_value=identity), \
            mock.patch.object(server, "_shader_workspace_service", return_value=workspace_service), \
            mock.patch.object(server, "_workspace_active_capture_context", side_effect=validate_catalog) as preflight, \
            mock.patch.object(server, "_shader_live_edit_apply_candidate", side_effect=AssertionError("no replay allowed")):
        try:
            with bind_direct_tool_call(old_scope["instanceId"], old_scope["captureSessionId"]):
                old = manager.start(workspace, 100)
            authorization_id = manager.list()["authorizations"][0]["authorizationId"]
            if same_gui_process:
                new_scope = publish("old-gui", 101, "2" * 32)
                owner_pid = 101
            else:
                alive.remove(101)
                alive.add(202)
                new_scope = publish("new-gui", 202, "2" * 32)
                owner_pid = 202
            # This is the existing Internal owner-monitor callback, not an
            # extra MCP request introduced by the delivery probe.
            refreshed = service.refresh_shader_sessions([owner_pid])
            assert refreshed["attempted"] is True
            resumed = refreshed["results"][0]["result"]["results"][0]
            assert resumed["resumed"] is True and resumed["liveEditId"] != old["liveEditId"]
            _wait_until(lambda: manager.poll(resumed["liveEditId"])["state"] == "running")
            current = manager.poll(resumed["liveEditId"])
            assert current["captureBinding"] == new_scope and current["activeReplacement"] is None
            assert manager.poll(old["liveEditId"])["state"] != "running"
            assert manager.list()["authorizations"][0]["authorizationId"] == authorization_id
            service.refresh_shader_sessions([owner_pid])
            assert len(watch.persistent_start_calls) == 1
            assert preflight.call_count == 2
        finally:
            manager.shutdown()


def test_owner_tick_ignores_unregistered_or_ambiguous_gui_mailboxes(tmp_path):
    from src import ipc_client

    manager = Manager()
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    for instance in ("one", "two"):
        (tmp_path / instance).mkdir()

    def manifest(directory):
        instance = Path(directory).name
        return {"instanceId": instance, "pid": 101 if instance == "one" else 202,
                "captureLoaded": True, "captureSessionId": "a" * 32,
                "capturePath": str(tmp_path / "frame.rdc")}

    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", side_effect=manifest), \
            mock.patch.object(service_module, "_shader_capture_mailbox", side_effect=manifest), \
            mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        service.refresh_shader_sessions([])
        service.refresh_shader_sessions([101, 202])
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202])
        assert manager.reconcile_capture.call_args.args[0]["instanceId"] == "two"


def test_busy_or_unavailable_registered_gui_is_not_erased_from_automatic_scope(tmp_path):
    from src import ipc_client

    manager = Manager()
    manager.reconcile_capture = mock.Mock(return_value={"results": []})
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    records = {}
    for instance, pid in (("busy", 101), ("fresh", 202)):
        (tmp_path / instance).mkdir()
        records[instance] = {"instanceId": instance, "pid": pid, "captureLoaded": True,
                             "captureSessionId": "a" * 32, "capturePath": str(tmp_path / "same.rdc")}
    with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(tmp_path)), \
            mock.patch.object(service_module, "_shader_capture_mailbox", side_effect=lambda directory: records.get(directory.name)), \
            mock.patch.object(ipc_client.IPCClient, "_manifest", side_effect=lambda directory: records["fresh"] if Path(directory).name == "fresh" else None), \
            mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True), \
            mock.patch.object(service_module, "_shader_capture_session_state", return_value="current"):
        # No old job is needed to protect the busy GUI: both physical owners
        # remain in uniqueness even when IPC discovery rejects its heartbeat.
        service.refresh_shader_sessions([101, 202])
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202, 303])  # The third owner is unreadable.
        manager.reconcile_capture.assert_not_called()
        service.refresh_shader_sessions([202])
        manager.reconcile_capture.assert_called_once()


def test_internal_start_defers_resume_to_registered_owner_tick():
    manager = Manager()
    manager.resume_persistent = mock.Mock(side_effect=AssertionError("must wait for registered owner tick"))
    service = service_module.InternalToolService(project=object(), restore_jobs=object(), shader_manager=manager)
    assert service.start()["resumeDeferredToOwnerTick"] is True
    manager.resume_persistent.assert_not_called()


def test_existing_internal_monitor_calls_bounded_capture_refresh():
    import ast

    tree = ast.parse(inspect.getsource(internal_host))
    monitor = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "monitor")
    calls = [node for node in ast.walk(monitor) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "tick"]
    assert any(isinstance(call.func.value, ast.Name)
               and call.func.value.id == "shader_session_refresh"
               and isinstance(call.args[0], ast.Name) and call.args[0].id == "owners"
               for call in calls)
    assert not any(isinstance(node, ast.Await) for node in ast.walk(monitor))


def test_mcp_forwards_without_constructing_task_local_manager(tmp_path):
    with mock.patch.object(server, "_active_live_context", return_value=context(tmp_path)):
        with mock.patch.object(client, "request", return_value={"liveEditId": "1" * 32}) as remote:
            with mock.patch.object(server, "_shader_live_edit_manager", side_effect=AssertionError("must use singleton")):
                with bind_direct_tool_call("qrenderdoc-test", "b" * 32):
                    result = json.loads(server.start_shader_workspace_live_edit(str(tmp_path), 100))
    assert result["operation"] == "start"
    remote.assert_called_once_with("start", {"workspace_path": str(tmp_path), "debounce_ms": 100}, binding(tmp_path))


def test_client_binds_generation_response_and_keeps_health_unauthenticated(tmp_path):
    paths = internal_host._paths(tmp_path)
    paths["state"].write_text(json.dumps({"port": 49743}), encoding="utf-8")
    paths["token"].write_text("x" * 48, encoding="ascii")
    health = {"capabilities": ["shader-live-edit"]}
    def respond(request, timeout):
        if request.full_url.endswith("/health"):
            assert request.get_header("Authorization") is None
            return health
        value = json.loads(request.data)
        assert request.full_url == "http://127.0.0.1:49743/replaylab/shader-live-edit"
        return {"schema": service_module.SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                "requestId": value["requestId"], "binding": value["binding"],
                "hostInstanceId": "f" * 32, "result": {"state": "running"}}
    with mock.patch.object(client, "_read_response", side_effect=respond):
        with mock.patch.object(internal_host, "_validate_generation", return_value="f" * 32) as validate:
            assert client.request("list", {}, binding(tmp_path), root=tmp_path) == {"state": "running"}
    assert validate.call_count == 2


def test_redirects_are_rejected_and_mcp_startup_does_not_resume_watchers():
    with pytest.raises(RuntimeError, match="cannot redirect"):
        client._NoRedirect().redirect_request(None, None, 302, "", {}, "https://foreign.example/")
    assert "resume_persistent" not in inspect.getsource(server.main)
