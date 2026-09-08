"""Fixed authenticated loopback client for the singleton Shader editor owner."""
from __future__ import annotations

import json
from pathlib import Path
import uuid
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .internal_tool_service import SHADER_LIVE_EDIT_OPERATION_SCHEMA

PATH = "/replaylab/shader-live-edit"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("ReplayLab internal Shader requests cannot redirect")


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _read_response(request, timeout):
    try:
        response = _OPENER.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        status = response.status
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("ReplayLab internal Shader response exceeds its limit")
    value = json.loads(payload.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise RuntimeError("ReplayLab internal Shader response is not an object")
    if status != 200:
        raise RuntimeError("ReplayLab internal Shader operation failed: %s" % str(value.get("error", status))[:2048])
    return value


def request(operation, parameters, binding, *, root=None):
    from . import internal_host

    directory = internal_host.runtime_root() if root is None else Path(root).resolve()
    state_path = internal_host._paths(directory)["state"]
    if not state_path.is_file() or state_path.is_symlink() or state_path.stat().st_size > 65536:
        raise RuntimeError("ReplayLab internal Shader service is unavailable; open qrenderdoc first")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    port = state.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise RuntimeError("ReplayLab internal Shader port is invalid")
    health = _read_response(Request("http://127.0.0.1:%d/health" % port, method="GET"), 5.0)
    generation = internal_host._validate_generation(directory, port, health)
    if "shader-live-edit" not in health.get("capabilities", []):
        raise RuntimeError("ReplayLab internal Shader service needs the current runtime; restart qrenderdoc after deployment")
    token_path = internal_host._paths(directory)["token"]
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_size > 1024:
        raise RuntimeError("ReplayLab internal Shader credential is unavailable")
    token = token_path.read_text(encoding="ascii").strip()
    if internal_host.TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("ReplayLab internal Shader credential is invalid")
    request_id = uuid.uuid4().hex
    body = json.dumps({"schema": SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                       "requestId": request_id, "operation": operation,
                       "parameters": parameters, "binding": binding},
                      ensure_ascii=True, allow_nan=False).encode("utf-8")
    value = _read_response(Request("http://127.0.0.1:%d%s" % (port, PATH), data=body,
                                  headers={"Authorization": "Bearer " + token,
                                           "Content-Type": "application/json"}, method="POST"), 120.0)
    internal_host._validate_generation(directory, port, health)
    if (value.get("schema") != SHADER_LIVE_EDIT_OPERATION_SCHEMA
            or value.get("requestId") != request_id
            or value.get("hostInstanceId") != generation
            or value.get("binding") != binding
            or not isinstance(value.get("result"), dict)):
        raise RuntimeError("ReplayLab internal Shader response identity changed")
    return value["result"]
