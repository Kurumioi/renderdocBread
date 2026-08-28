from __future__ import annotations

import json
import sys
import time


mode = sys.argv[1] if len(sys.argv) > 1 else "success"
thread_start_attempts = 0
turn_start_attempts = 0
login_completed = False
post_login_account_reads = 0
pending_dynamic_tool: int | None = None
dynamic_mode = mode.startswith("dynamic-tool")


def send(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def send_dynamic_request(
    request_id: int = 900,
    *,
    turn_id: object = "turn-test",
    call_id: object = "dynamic-call-1",
) -> None:
    send(
        {
            "id": request_id,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-test",
                "turnId": turn_id,
                "callId": call_id,
                "namespace": None,
                "tool": "inspect_event",
                "arguments": {"event_id": 7},
            },
        }
    )


def complete_dynamic_turn() -> None:
    send(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-test",
                "turnId": "turn-test",
                "delta": "dynamic evidence ",
            },
        }
    )
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-test",
                "turnId": "turn-test",
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "dynamic evidence answer",
                },
            },
        }
    )
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-test",
                "turn": {"id": "turn-test", "status": "completed", "error": None},
            },
        }
    )


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method is None and dynamic_mode and request_id in {900, 901}:
        assert pending_dynamic_tool == request_id
        if mode in {
            "dynamic-tool-wrong-turn",
            "dynamic-tool-wrong-type",
            "dynamic-tool-duplicate",
        } and (
            mode in {"dynamic-tool-wrong-turn", "dynamic-tool-wrong-type"}
            or request_id == 901
        ):
            assert message["error"]["code"] == -32602
            pending_dynamic_tool = None
            continue
        if mode in {"dynamic-tool-failure", "dynamic-tool-invalid-json"}:
            assert message["result"]["success"] is False
            content = message["result"]["contentItems"]
            assert json.loads(content[0]["text"])["error"] == "internal RenderDoc tool failed"
        else:
            assert message["result"]["success"] is True
            content = message["result"]["contentItems"]
            assert json.loads(content[0]["text"])["eventId"] == 7
        if mode == "dynamic-tool-duplicate" and request_id == 900:
            send_dynamic_request(901)
            pending_dynamic_tool = 901
            continue
        pending_dynamic_tool = None
        complete_dynamic_turn()
    elif method == "initialize":
        if mode == "exit-initialize":
            raise SystemExit(17)
        if dynamic_mode:
            assert message["params"]["capabilities"]["experimentalApi"] is True
        else:
            assert "capabilities" not in message["params"]
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "account/read":
        if mode in {"login-race", "login-update-first"}:
            if login_completed:
                post_login_account_reads += 1
            # Reproduce the real first-login race: login/completed has fired,
            # but one immediately following account/read can still be signed
            # out while account/updated is in the notification queue.
            authenticated = login_completed and post_login_account_reads >= 2
        else:
            authenticated = not mode.startswith("signed-out")
        account = (
            {
                "type": "chatgpt",
                "email": "test@example.invalid",
                "planType": "plus",
            }
            if authenticated
            else None
        )
        result = {"account": account}
        if mode != "signed-out-no-flag":
            result["requiresOpenaiAuth"] = mode != "signed-out-false"
        send({"id": request_id, "result": result})
    elif method == "model/list":
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": "codex-test",
                            "model": "codex-test",
                            "displayName": "Codex Test",
                            "hidden": False,
                            "isDefault": True,
                        }
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method == "account/login/start":
        flow = message["params"]["type"]
        if flow == "chatgpt":
            # ReplayLab must use app-server's local callback success page, not
            # a hosted branded redirect that can open a new Codex/ChatGPT chat.
            assert message["params"] == {"type": "chatgpt"}
        result = {"type": flow, "loginId": "login-test"}
        if flow == "chatgpt":
            result["authUrl"] = "https://chatgpt.com/fake-login"
        else:
            result["verificationUrl"] = "https://auth.openai.com/codex/device"
            result["userCode"] = "TEST-CODE"
        send({"id": request_id, "result": result})
        login_completed = True
        completion_notification = {
            "method": "account/login/completed",
            "params": {
                "loginId": "login-test",
                "success": True,
                "error": None,
            },
        }
        account_notification = {
            "method": "account/updated",
            "params": {"authMode": "chatgpt", "planType": "plus"},
        }
        if mode == "login-update-first":
            send(account_notification)
            send(completion_notification)
        else:
            send(completion_notification)
            send(account_notification)
    elif method == "account/login/cancel":
        send({"id": request_id, "result": {}})
        send(
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": message["params"]["loginId"],
                    "success": False,
                    "error": "cancelled",
                },
            }
        )
    elif method == "account/logout":
        send({"id": request_id, "result": {}})
    elif method == "thread/start":
        assert not mode.startswith("signed-out"), "signed-out run must fail before thread/start"
        assert message["params"]["approvalPolicy"] == "never"
        assert message["params"]["ephemeral"] is True
        if dynamic_mode:
            assert isinstance(message["params"].get("dynamicTools"), list)
            assert [item["name"] for item in message["params"]["dynamicTools"]] == [
                "inspect_event"
            ]
        else:
            assert "dynamicTools" not in message["params"]
        thread_start_attempts += 1
        if mode == "kebab-sandbox" and thread_start_attempts == 1:
            assert message["params"]["sandbox"] == "readOnly"
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": (
                            "Invalid request: unknown variant `readOnly`, expected one of "
                            "`read-only`, `workspace-write`, `danger-full-access`"
                        ),
                    },
                }
            )
            continue
        expected_sandbox = "read-only" if mode == "kebab-sandbox" else "readOnly"
        assert message["params"]["sandbox"] == expected_sandbox
        thread = {"id": "thread-test"}
        if mode == "ambient-instructions":
            thread["instructionSources"] = [{"path": "C:/ambient/AGENTS.md"}]
        send({"id": request_id, "result": {"thread": thread}})
    elif method == "mcpServerStatus/list":
        entries = []
        if mode in {"extra-mcp", "wrong-tools"}:
            entries.append({"name": "ambient", "serverInfo": {}, "tools": {}})
        send(
            {
                "id": request_id,
                "result": {
                    "data": entries,
                    "nextCursor": None,
                },
            }
        )
    elif method == "app/installed":
        apps = [{"id": "ambient-app", "callable": True}] if mode == "callable-app" else []
        send({"id": request_id, "result": {"apps": apps}})
    elif method == "turn/start":
        turn_start_attempts += 1
        policy = message["params"]["sandboxPolicy"]
        assert policy["type"] == "readOnly"
        if mode == "modern-turn-sandbox" and turn_start_attempts == 1:
            assert policy["access"]["type"] == "restricted"
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": (
                            "Invalid request: readOnly.access is no longer supported; "
                            "use permissionProfile for restricted reads"
                        ),
                    },
                }
            )
            continue
        if mode == "modern-turn-sandbox":
            assert "access" not in policy
            assert policy["networkAccess"] is False
        else:
            assert policy["access"]["type"] == "restricted"
            assert policy["access"]["includePlatformDefaults"] is False
            assert message["params"]["cwd"] in policy["access"]["readableRoots"]
        if mode == "dynamic-tool-prestart":
            send_dynamic_request()
            pending_dynamic_tool = 900
        send(
            {
                "id": request_id,
                "result": {"turn": {"id": "turn-test", "status": "inProgress"}},
            }
        )
        if dynamic_mode:
            if mode != "dynamic-tool-prestart":
                dynamic_turn_id: object = "turn-test"
                if mode == "dynamic-tool-wrong-turn":
                    dynamic_turn_id = "turn-other"
                elif mode == "dynamic-tool-wrong-type":
                    dynamic_turn_id = 7
                send_dynamic_request(turn_id=dynamic_turn_id)
                pending_dynamic_tool = 900
            continue
        if mode == "invalid-jsonl":
            sys.stdout.flush()
            sys.stdout.buffer.write(b"{not-json}\n")
            sys.stdout.buffer.flush()
            raise SystemExit(18)
        if mode == "invalid-utf8":
            sys.stdout.flush()
            sys.stdout.buffer.write(b"\xff\xfe\n")
            sys.stdout.buffer.flush()
            raise SystemExit(19)
        if mode == "hang-turn":
            time.sleep(60)
        if mode == "slow-status":
            send(
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "message": "request timed out; retrying",
                    },
                }
            )
            time.sleep(0.05)
        if mode == "approval":
            send(
                {
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "itemId": "item-test",
                    },
                }
            )
        else:
            if mode == "activity-events":
                send(
                    {
                        "method": "turn/plan/updated",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "explanation": "Inspect the capture safely",
                            "plan": [
                                {"step": "Read pipeline state", "status": "inProgress"},
                                {"step": "token=plan-secret", "status": "pending"},
                            ],
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {"id": "plan-1", "type": "plan", "text": ""},
                        },
                    }
                )
                send(
                    {
                        "method": "item/plan/delta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "itemId": "plan-1",
                            "delta": "Inspect resources, then explain the draw.",
                        },
                    }
                )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "plan-1",
                                "type": "plan",
                                "text": "Inspect resources, then explain the draw.",
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "reasoning-1",
                                "type": "reasoning",
                                "summary": [],
                                "content": ["RAW_REASONING_STARTED_MUST_NOT_APPEAR"],
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/reasoning/textDelta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "itemId": "reasoning-1",
                            "delta": "RAW_REASONING_DELTA_MUST_NOT_APPEAR token=raw-secret",
                        },
                    }
                )
                for delta in (
                    "Checking the selected draw. ",
                    "Comparing its public bindings; password=summary-secret",
                ):
                    send(
                        {
                            "method": "item/reasoning/summaryTextDelta",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "itemId": "reasoning-1",
                                "summaryIndex": 0,
                                "delta": delta,
                            },
                        }
                    )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "reasoning-1",
                                "type": "reasoning",
                                "summary": [{"text": "Public reasoning summary complete."}],
                                "content": ["RAW_REASONING_COMPLETED_MUST_NOT_APPEAR"],
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": (
                                    "probe --token cmd-secret password=hunter2 "
                                    "OPENAI_API_KEY=env-secret Authorization: Bearer auth-secret"
                                ),
                                "cwd": "/safe/workspace",
                                "status": "inProgress",
                            },
                        },
                    }
                )
                for delta in ("tok", "en=stream-secret\nvisible output\n" + ("x" * 20_000)):
                    send(
                        {
                            "method": "item/commandExecution/outputDelta",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "itemId": "command-1",
                                "delta": delta,
                            },
                        }
                    )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": (
                                    "probe --token cmd-secret password=hunter2 "
                                    "OPENAI_API_KEY=env-secret Authorization: Bearer auth-secret"
                                ),
                                "cwd": "/safe/workspace",
                                "status": "completed",
                                "aggregatedOutput": "api_key=final-output-secret\nfinal output ok",
                                "exitCode": 0,
                                "durationMs": 23,
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "mcp-1",
                                "type": "mcpToolCall",
                                "server": "replaylab",
                                "tool": "get_pipeline_state",
                                "status": "inProgress",
                                "arguments": {
                                    "eventId": 7,
                                    "api_key": "mcp-secret",
                                    "clientSecret": "camel-secret-value",
                                },
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "mcp-1",
                                "type": "mcpToolCall",
                                "server": "replaylab",
                                "tool": "get_pipeline_state",
                                "status": "completed",
                                "arguments": {
                                    "eventId": 7,
                                    "api_key": "mcp-secret",
                                    "clientSecret": "camel-secret-value",
                                },
                                "result": {"text": "Bearer mcp-result-secret", "ok": True},
                                "durationMs": 11,
                            },
                        },
                    }
                )
                for item in (
                    {"id": "web-1", "type": "webSearch", "query": "RenderDoc docs"},
                    {"id": "image-1", "type": "imageView", "path": "/safe/frame.png"},
                    {"id": "compact-1", "type": "contextCompaction"},
                ):
                    send(
                        {
                            "method": "item/started",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "item": item,
                            },
                        }
                    )
                    send(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "item": item,
                            },
                        }
                    )
            answer = "x" * 100_000 if mode == "large-line" else "evidence answer"
            deltas = ["x" * 8192] * 129 if mode == "many-deltas" else ["evidence "]
            for delta in deltas:
                send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "delta": delta,
                        },
                    }
                )
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": answer,
                        },
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turn": {"id": "turn-test", "status": "completed", "error": None},
                    },
                }
            )
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
