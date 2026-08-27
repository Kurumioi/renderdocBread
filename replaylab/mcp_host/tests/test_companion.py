from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.companion.engine import (
    ORGANIZE_INSTRUCTIONS,
    ORGANIZE_POLICY_VERSION,
    _AppliedWorkspace,
    CompanionConfig,
    CompanionEngine,
    CompanionError,
    _TrackedWatch,
    _truncated_payload,
)
from src.companion.mcp_client import (
    MCPToolSpec,
    StdioMCPClient,
    _sanitised_child_environment,
)
from src.companion.cli import _config, _default_workspace_root, _mcp_launch, _parser
from src.companion.openai_client import OpenAIResponsesClient, OpenAIStartupError
from src.companion.policy import (
    EFFECTFUL_TOOLS,
    PolicyError,
    TurnPolicy,
    parse_authorized_message,
)


SESSION = "a" * 32


def _context() -> dict:
    return {
        "schema": "renderdoc-ultra-current-context/v1",
        "schemaVersion": 1,
        "snapshotId": "b" * 64,
        "capture": {
            "loaded": True,
            "captureSessionId": SESSION,
            "captureId": "c" * 64,
            "path": r"D:\captures\frame.rdc",
            "api": "D3D11",
            "local": True,
        },
        "events": {"currentEventId": 142, "selectedEventId": 142},
        "shaders": {
            "bound": [
                {
                    "stage": "fragment",
                    "resourceId": 902,
                    "entryPoint": "main",
                    "rawSha256": "2" * 64,
                    "exactRawBytesAvailable": True,
                }
            ],
            "focused": None,
            "focusIsExplicit": False,
        },
        "texture": {"available": False},
    }


def _intent(kind: str = "explain-current-event") -> dict:
    operations = {
        "explain-current-event": ["read-capture", "explain-event"],
        "inspect-picked-pixel": ["read-capture", "inspect-picked-pixel"],
        "reverse-edit-selected-shader": [
            "read-capture",
            "synchronise-active-project",
            "resolve-project-symbols",
            "export-shader",
            "create-workspace",
            "decompile-shader",
            "organise-source-with-ai",
            "persist-project-knowledge",
            "open-external-editor",
            "watch-and-compile-saves",
            "apply-compatible-shared-resource-saves",
        ],
    }[kind]
    arguments = {"eventId": 142}
    if kind == "reverse-edit-selected-shader":
        arguments.update(
            {
                "stage": "fragment",
                "shaderResourceId": 902,
                "entryPoint": "main",
                "rawSha256": "2" * 64,
            }
        )
    if kind == "inspect-picked-pixel":
        arguments.update(
            {
                "textureResourceId": 44,
                "subresource": {"mip": 0, "slice": 0, "sample": 0},
                "pixel": {"x": 10, "y": 20},
            }
        )
    return {
        "intentId": "d" * 32,
        "leaseToken": "e" * 32,
        "kind": kind,
        "origin": "renderdoc-ui",
        "context": _context(),
        "arguments": arguments,
        "recommendedWorkflow": {
            "automaticExecutionAuthorized": True,
            "replayMutationAuthorized": False,
            "allowedOperations": operations,
        },
    }


def _asset_recovery_assessment(level: int = 6) -> dict:
    return {
        "identityValue": 6,
        "sourceValue": 3,
        "compileUtility": 3,
        "recoveryLevel": level,
        "identityReason": "raw binary-container SHA-256 equality",
        "recoveryReason": "raw binary-container equality",
        "sourceReasons": ["readable untrusted primary source"],
        "matches": {
            "rawSha256Equal": True,
            "codeSha256Equal": False,
            "stageEqual": True,
            "profileEqual": True,
            "entryPointEqual": True,
            "interfaceHashEqual": False,
            "layoutHashEqual": False,
            "pathOrNameHint": True,
        },
        "conflicts": [],
        "abiFactsEqual": False,
        "applyAuthorized": False,
        "workspaceSeedAuthorized": False,
        "trustState": "untrusted-discovery",
        "requiresIndependentVerification": [
            "fixed toolchain route when compilation is requested",
            "full reflected ABI and live-capture validation before apply",
        ],
    }


def _asset_shader_recovery_report(source_preview: str = "") -> dict:
    candidate_id = "1" * 32
    file_sha256 = "3" * 64
    preview_bytes = source_preview.encode("utf-8")
    report = {
        "schema": "replaylab-active-asset-shader-recovery/v1",
        "schemaVersion": 1,
        "inventory": {"candidateCount": 1},
        "boundShaders": [
            {
                "stage": "fragment",
                "shaderResourceId": 902,
                "entryPoint": "main",
                "rawSha256": "2" * 64,
                "candidateCountWithValue": 1,
                "topCandidates": [
                    {
                        "candidateId": candidate_id,
                        "relativePath": "Shaders/main.hlsl",
                        "fileSha256": file_sha256,
                        "fileOffset": 0,
                        "byteSize": max(1, len(preview_bytes)),
                        "format": "source",
                        "language": "hlsl",
                        "role": "primary-source",
                        "fingerprints": {
                            "rawSha256": "2" * 64,
                            "codeSha256": "",
                        },
                        "shaderFacts": {
                            "stage": "fragment",
                            "profile": "ps_6_0",
                            "entryPoint": "main",
                            "interfaceHash": "",
                            "layoutHash": "",
                        },
                        "engineHints": ["generic-shader-source"],
                        "sourceMetadata": {
                            "encoding": "utf-8",
                            "includeCount": 0,
                        },
                        "assessment": _asset_recovery_assessment(),
                    }
                ],
                "candidatesTruncated": False,
            }
        ],
        "policy": {
            "assetCodeExecuted": False,
            "compilerInvoked": False,
            "allCandidatesUntrusted": True,
            "workspaceSeedAuthorized": False,
            "applyAuthorized": False,
            "exactBinaryStillRequiresCaptureBoundValidation": True,
            "cacheScope": "active capture session + asset root + build",
        },
    }
    if source_preview:
        report["sourcePreviews"] = [
            {
                "candidateId": candidate_id,
                "relativePath": "Shaders/main.hlsl",
                "fileSha256": file_sha256,
                "sourcePreview": source_preview,
                "sourcePreviewSha256": hashlib.sha256(preview_bytes).hexdigest(),
                "sourcePreviewUtf8Bytes": len(preview_bytes),
                "sourcePreviewTruncated": False,
            }
        ]
        report["sourcePreviewPolicy"] = {
            "maximumCandidateUtf8Bytes": 64 * 1024,
            "maximumTotalUtf8Bytes": 512 * 1024,
            "maximumCandidates": 16,
            "fullFileHashRevalidated": True,
            "allPreviewTextIsUntrustedData": True,
        }
    return report


def _spec(name: str) -> MCPToolSpec:
    return MCPToolSpec(
        name=name,
        description="test tool " + name,
        input_schema={"type": "object", "properties": {}},
    )


class _FakeMCP:
    def __init__(self, events=None, workspace_source: str | None = None):
        self.events = events if events is not None else []
        names = {
            "get_current_renderdoc_context",
            "claim_renderdoc_ui_intents",
            "complete_renderdoc_ui_intent",
            "renew_renderdoc_ui_intent",
            "get_draw_call_details",
            "get_pipeline_state",
            "get_shader_info",
            "get_bound_textures",
            "find_draws_by_resource",
            "get_debug_messages",
            "inspect_pixel_object",
            "pixel_history",
            "get_texture_info",
            "reverse_shader",
            "prepare_shader_workspace",
            "get_current_shader_representation",
            "compile_shader_workspace",
            "get_shader_workspace_status",
            "begin_shader_workspace_organization",
            "review_shader_workspace_organization",
            "validate_shader_workspace_candidate",
            "list_shader_workspace_family_uses",
            "apply_shader_workspace_candidate_family",
            "rollback_shader_workspace_override_family",
            "apply_shader_workspace_shared_resource",
            "rollback_shader_workspace_shared_resource",
            "start_shader_workspace_watch",
            "poll_shader_workspace_watch",
            "stop_shader_workspace_watch",
            "export_capture_symbol_catalog",
            "create_replay_project",
            "get_replay_project_status",
            "import_replay_project_capture",
            "get_replay_project_reuse_plan",
            "seed_shader_workspace_from_replay_project",
            "register_replay_project_workspace",
        }
        self.tools = {name: _spec(name) for name in names}
        self.large_tool_output = ""
        self.workspace_source = workspace_source
        self.claims: list[dict] = []
        self.completed: list[dict] = []
        self.stop_event: asyncio.Event | None = None
        self.compile_attempts = 0
        self.fail_compile_attempt = 0
        self.fail_shared_rollback = False
        self.current_context = _context()
        self.project_matches: list[dict] = []
        self.project_source = "float4 main() : SV_Target { return 0.5; }\n"
        self.project_scope: dict[str, str] | None = None
        self.replay_abi_validated = True
        self.prepare_error = ""
        self.prepare_representation_lane = ""
        self.current_representation_lane = "explanatory-ir"
        self.current_representation_path = ""
        self.compiled_candidates: dict[str, dict] = {}
        self.organization_sessions: dict[tuple[str, str], dict] = {}
        self.compile_code_sha256s: list[str] = []
        self.captured_code_sha256 = "4" * 64
        self.validation_revision_override: int | None = None
        self.validation_raw_override: str | None = None
        self.mutate_source_after_validation: str | None = None
        self.advance_candidate_after_validation = False

    @staticmethod
    def _source_in_workspace(workspace: Path) -> Path:
        source_root = workspace / "source"
        candidates = [
            value
            for value in source_root.iterdir()
            if value.is_file() and not value.is_symlink()
        ]
        if len(candidates) != 1:
            raise AssertionError("test workspace must contain exactly one source")
        return candidates[0]

    async def list_tools(self):
        return dict(self.tools)

    async def call_tool(self, name, arguments):
        self.events.append(("mcp", name, dict(arguments)))
        if name == "get_current_renderdoc_context":
            return json.dumps(self.current_context)
        if name in {"create_replay_project", "get_replay_project_status"}:
            project_path = Path(arguments["project_path"])
            if name == "create_replay_project":
                project_path.mkdir(parents=False)
                self.project_scope = {
                    "gameId": arguments["game_id"],
                    "buildId": arguments["build_id"],
                    "platform": arguments.get("platform", ""),
                    "apiFamily": str(arguments.get("api_family", "")).lower(),
                }
            scope = self.project_scope or {
                "gameId": "existing-game",
                "buildId": "existing-build",
                "platform": "",
                "apiFamily": "d3d11",
            }
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-replay-project/v1",
                    "schemaVersion": 1,
                    "projectId": "9" * 32,
                    "projectPath": str(project_path),
                    "scope": scope,
                    "policy": {
                        "crossFrameReplayStateReused": False,
                        "oldAbiReportsReused": False,
                        "autoApplyAuthorized": False,
                        "liveAbiRevalidationRequired": True,
                        "oneGameBuildPlatformApiScope": True,
                    },
                }
            )
        if name == "export_capture_symbol_catalog":
            output = Path(arguments["output_dir"])
            output.mkdir(parents=True, exist_ok=True)
            catalog = output / "shader_catalog.v1.json"
            catalog.write_text("{}\n", encoding="utf-8")
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-symbol-catalog/v1",
                    "captureId": self.current_context["capture"]["captureId"],
                    "catalogPath": str(catalog),
                }
            )
        if name in {
            "import_replay_project_capture",
            "get_replay_project_reuse_plan",
        }:
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-project-reuse-plan/v1",
                    "schemaVersion": 1,
                    "projectId": "9" * 32,
                    "projectPath": arguments["project_path"],
                    "captureId": self.current_context["capture"]["captureId"],
                    "matchedShaderCount": len(self.project_matches),
                    "matches": self.project_matches,
                    "policy": {
                        "automaticHashMatching": True,
                        "rawExactOrCodeInterfaceLayoutExactOnly": True,
                        "oldWorkspaceManifestReused": False,
                        "oldCaptureAbiReportReused": False,
                        "oldMaterialSessionReused": False,
                        "autoApplyAuthorized": False,
                        "compileRequiredAfterSourceSeed": True,
                        "liveAbiRevalidationRequiredBeforeShaderApply": True,
                        "liveMaterialLayoutRevalidationRequired": True,
                    },
                }
            )
        if name == "seed_shader_workspace_from_replay_project":
            workspace = Path(arguments["workspace_path"])
            source = workspace / "source" / "shader.hlsl"
            source.write_text(self.project_source, encoding="utf-8")
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-project-workspace-seed/v1",
                    "projectId": "9" * 32,
                    "captureId": self.current_context["capture"]["captureId"],
                    "knowledgeId": arguments["knowledge_id"],
                    "workspacePath": str(workspace),
                    "sourcePath": str(source),
                    "sourceSha256": hashlib.sha256(
                        self.project_source.encode("utf-8")
                    ).hexdigest(),
                    "matchMode": "raw-exact",
                    "applied": False,
                    "compiled": False,
                    "requiresCompile": True,
                    "requiresLiveAbiRevalidation": True,
                }
            )
        if name == "register_replay_project_workspace":
            return json.dumps(
                {
                    "knowledge": {
                        "knowledgeId": "6" * 32,
                        "workspacePath": arguments["workspace_path"],
                    },
                    "idempotent": False,
                    "applied": False,
                }
            )
        if name == "claim_renderdoc_ui_intents":
            intents = self.claims[:1]
            self.claims = self.claims[1:]
            return json.dumps({"intents": intents})
        if name == "complete_renderdoc_ui_intent":
            self.completed.append(dict(arguments))
            if self.stop_event is not None:
                self.stop_event.set()
            return json.dumps({"state": arguments["outcome"]})
        if name == "renew_renderdoc_ui_intent":
            return json.dumps(
                {
                    "intentId": arguments["intent_id"],
                    "state": "claimed",
                    "leaseToken": arguments["lease_token"],
                }
            )
        if name == "begin_shader_workspace_organization":
            workspace = Path(arguments["workspace_path"]).resolve()
            source = self._source_in_workspace(workspace)
            baseline_bytes = source.read_bytes()
            baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
            compiled_envelope = json.loads(
                await self.call_tool(
                    "compile_shader_workspace", {"workspace_path": str(workspace)}
                )
            )
            if compiled_envelope.get("error"):
                return json.dumps(compiled_envelope)
            compiled = compiled_envelope["result"]
            validated_envelope = json.loads(
                await self.call_tool(
                    "validate_shader_workspace_candidate",
                    {
                        "workspace_path": str(workspace),
                        "expected_revision": compiled["revision"],
                        "expected_candidate_raw_sha256": compiled["rawSha256"],
                    },
                )
            )
            if not validated_envelope["validation"]["replayAbiValidated"]:
                return json.dumps({"error": "fake baseline ABI rejection"})
            status_envelope = json.loads(
                await self.call_tool(
                    "get_shader_workspace_status",
                    {"workspace_path": str(workspace)},
                )
            )
            fidelity = status_envelope["result"]["reconstructionFidelity"]
            organization_id = "%032x" % (len(self.organization_sessions) + 1)
            ai_dir = workspace / "ai"
            ai_dir.mkdir(exist_ok=True)
            (ai_dir / "decompiler-original.hlsl").write_bytes(baseline_bytes)
            session_dir = ai_dir / "organization-sessions" / organization_id
            session_dir.mkdir(parents=True)
            proposal = session_dir / "organized-proposal.hlsl"
            analysis = session_dir / "analysis.md"
            flowchart = session_dir / "flowchart.mmd"
            semantic_context = session_dir / "semantic-context.json"
            session_path = session_dir / "session.json"
            proposal.write_bytes(baseline_bytes)
            analysis.write_text("# Analysis\n", encoding="utf-8")
            flowchart.write_text("flowchart TD\n  A-->B\n", encoding="utf-8")
            context = {
                "schema": "renderdoc-ai-shader-semantic-context/v1",
                "schemaVersion": 1,
                "workspaceId": "7" * 32,
                "captureId": self.current_context["capture"]["captureId"],
                "interface": {
                    "inputSignature": [
                        {
                            "semanticName": "TEXCOORD",
                            "semanticIndex": 0,
                            "registerIndex": 0,
                        }
                    ],
                    "outputSignature": [
                        {
                            "semanticName": "SV_Target",
                            "semanticIndex": 0,
                            "registerIndex": 0,
                        }
                    ],
                    "readOnlyResources": [{"register": 0, "space": 0}],
                    "readWriteResources": [],
                    "samplers": [{"register": 0, "space": 0}],
                    "constantBlocks": [{"register": 0, "space": 0, "byteSize": 64}],
                },
                "liveBindingEvidence": {
                    "available": True,
                    "eventId": int(arguments["event_id"]),
                    "captureId": self.current_context["capture"]["captureId"],
                    "captureSessionId": self.current_context["capture"][
                        "captureSessionId"
                    ],
                    "boundTextures": [{"slot": 0, "resourceId": 44}],
                    "pipelineState": {
                        "shaders": {"fragment": {"shaderId": 902}}
                    },
                },
                "reconstructionFidelity": fidelity,
            }
            semantic_context.write_text(json.dumps(context), encoding="utf-8")
            session_path.write_text("{}", encoding="utf-8")
            self.organization_sessions[(str(workspace), organization_id)] = {
                "sourcePath": source,
                "baselineBytes": baseline_bytes,
                "baselineSourceSha256": baseline_sha,
                "baselineCodeSha256": compiled["codeSha256"],
                "proposalPath": proposal,
                "analysisPath": analysis,
                "flowchartPath": flowchart,
                "sessionDir": session_dir,
            }
            return json.dumps(
                {
                    "schema": "renderdoc-ai-shader-organization-begin/v1",
                    "schemaVersion": 1,
                    "workspacePath": str(workspace),
                    "organizationId": organization_id,
                    "sourcePath": str(source),
                    "proposalSourcePath": str(proposal),
                    "analysisPath": str(analysis),
                    "flowchartPath": str(flowchart),
                    "semanticContextPath": str(semantic_context),
                    "sessionPath": str(session_path),
                    "baselineSourceSha256": baseline_sha,
                    "baselineCodeSha256": compiled["codeSha256"],
                    "semanticContext": context,
                    "applied": False,
                }
            )
        if name == "review_shader_workspace_organization":
            workspace = Path(arguments["workspace_path"]).resolve()
            organization_id = arguments["organization_id"]
            session = self.organization_sessions[(str(workspace), organization_id)]
            source = session["sourcePath"]
            baseline_bytes = session["baselineBytes"]
            proposal_bytes = session["proposalPath"].read_bytes()
            proposal_sha = hashlib.sha256(proposal_bytes).hexdigest()
            source.write_bytes(proposal_bytes)
            compiled_envelope = json.loads(
                await self.call_tool(
                    "compile_shader_workspace", {"workspace_path": str(workspace)}
                )
            )
            if compiled_envelope.get("error"):
                source.write_bytes(baseline_bytes)
                recovery_envelope = json.loads(
                    await self.call_tool(
                        "compile_shader_workspace", {"workspace_path": str(workspace)}
                    )
                )
                if not recovery_envelope.get("error"):
                    recovery = recovery_envelope["result"]
                    await self.call_tool(
                        "validate_shader_workspace_candidate",
                        {
                            "workspace_path": str(workspace),
                            "expected_revision": recovery["revision"],
                            "expected_candidate_raw_sha256": recovery["rawSha256"],
                        },
                    )
                failure_gate = {
                    "schema": "renderdoc-ai-shader-organization-gate/v1",
                    "organizationId": organization_id,
                    "decision": "organization-review-failed",
                    "codeSha256Equal": False,
                    "organizedSourcePromoted": False,
                    "sourceRestored": True,
                    "semanticEquivalenceVerified": False,
                    "replayApplied": False,
                    "safeToStartLiveEdit": False,
                }
                (workspace / "ai" / "organize-code-gate.json").write_text(
                    json.dumps(failure_gate), encoding="utf-8"
                )
                (session["sessionDir"] / "failure.json").write_text(
                    json.dumps({"sourceRestored": True}), encoding="utf-8"
                )
                return json.dumps(
                    {"error": "organization review failed; baseline source restored"}
                )
            organized_compile = compiled_envelope["result"]
            await self.call_tool(
                "validate_shader_workspace_candidate",
                {
                    "workspace_path": str(workspace),
                    "expected_revision": organized_compile["revision"],
                    "expected_candidate_raw_sha256": organized_compile["rawSha256"],
                },
            )
            code_equal = (
                organized_compile["codeSha256"] == session["baselineCodeSha256"]
            )
            root_analysis = workspace / "ai" / "analysis.md"
            root_flowchart = workspace / "ai" / "flowchart.mmd"
            root_analysis.write_bytes(session["analysisPath"].read_bytes())
            root_flowchart.write_bytes(session["flowchartPath"].read_bytes())
            recovery_compile = None
            if code_equal:
                (workspace / "ai" / "organized.hlsl").write_bytes(proposal_bytes)
                final_compile = organized_compile
            else:
                (workspace / "ai" / "organized-provisional.hlsl").write_bytes(
                    proposal_bytes
                )
                source.write_bytes(baseline_bytes)
                recovery_envelope = json.loads(
                    await self.call_tool(
                        "compile_shader_workspace", {"workspace_path": str(workspace)}
                    )
                )
                recovery_compile = recovery_envelope["result"]
                await self.call_tool(
                    "validate_shader_workspace_candidate",
                    {
                        "workspace_path": str(workspace),
                        "expected_revision": recovery_compile["revision"],
                        "expected_candidate_raw_sha256": recovery_compile["rawSha256"],
                    },
                )
                final_compile = recovery_compile
            request_id = hashlib.sha256(
                ("request:" + str(final_compile["revision"])).encode("ascii")
            ).hexdigest()
            report_sha = hashlib.sha256(
                ("report:" + final_compile["rawSha256"]).encode("ascii")
            ).hexdigest()
            final_binding = {
                "complete": True,
                "sourceSha256": final_compile["sourceSha256"],
                "revision": final_compile["revision"],
                "candidateRawSha256": final_compile["rawSha256"],
                "codeSha256": final_compile["codeSha256"],
                "abiRequestId": request_id,
                "abiReportSha256": report_sha,
            }
            gate = {
                "schema": "renderdoc-ai-shader-organization-gate/v1",
                "organizationId": organization_id,
                "baselineSourceSha256": session["baselineSourceSha256"],
                "proposalSourceSha256": proposal_sha,
                "baselineCodeSha256": session["baselineCodeSha256"],
                "organizedCodeSha256": organized_compile["codeSha256"],
                "codeSha256Equal": code_equal,
                "decision": (
                    "accepted-executable-editable"
                    if code_equal
                    else "retained-provisional-code-change"
                ),
                "organizedSourcePromoted": code_equal,
                "semanticEquivalenceVerified": False,
                "replayApplied": False,
                "safeToStartLiveEdit": code_equal,
                "finalBinding": final_binding,
                "finalSourceSha256": final_binding["sourceSha256"],
                "finalRevision": final_binding["revision"],
                "finalCandidateRawSha256": final_binding["candidateRawSha256"],
                "finalCodeSha256": final_binding["codeSha256"],
                "finalAbiRequestId": request_id,
                "finalAbiReportSha256": report_sha,
            }
            gate_path = workspace / "ai" / "organize-code-gate.json"
            gate_path.write_text(json.dumps(gate), encoding="utf-8")
            return json.dumps(
                {
                    "schema": "renderdoc-ai-shader-organization-review/v1",
                    "schemaVersion": 1,
                    "workspacePath": str(workspace),
                    "organizationId": organization_id,
                    "sourcePath": str(source),
                    "analysisPath": str(root_analysis),
                    "flowchartPath": str(root_flowchart),
                    "gatePath": str(gate_path),
                    "gate": gate,
                    "organizedCompile": organized_compile,
                    "organizedAbi": {"replayAbiValidated": True},
                    "recoveryCompile": recovery_compile,
                    "recoveryAbi": (
                        {"replayAbiValidated": True}
                        if recovery_compile is not None
                        else None
                    ),
                    "applied": False,
                }
            )
        if name == "validate_shader_workspace_candidate":
            revision = arguments.get("expected_revision", 1)
            candidate_hash = arguments.get(
                "expected_candidate_raw_sha256", "3" * 64
            )
            workspace = Path(arguments["workspace_path"]).resolve()
            candidate = self.compiled_candidates.get(str(workspace))
            if (
                candidate is not None
                and revision == candidate["revision"]
                and candidate_hash == candidate["rawSha256"]
            ):
                candidate["validated"] = self.replay_abi_validated
            returned_revision = (
                revision
                if self.validation_revision_override is None
                else self.validation_revision_override
            )
            returned_hash = (
                candidate_hash
                if self.validation_raw_override is None
                else self.validation_raw_override
            )
            if self.mutate_source_after_validation is not None and candidate is not None:
                Path(candidate["sourcePath"]).write_text(
                    self.mutate_source_after_validation, encoding="utf-8"
                )
            if self.advance_candidate_after_validation and candidate is not None:
                candidate["revision"] += 1
                candidate["rawSha256"] = "e" * 64
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-abi-validation-mcp/v1",
                    "operation": "validate",
                    "binding": {
                        "revision": returned_revision,
                        "candidateRawSha256": returned_hash,
                    },
                    "validation": {
                        "requiresReplayAbiValidation": True,
                        "replayAbiValidated": self.replay_abi_validated,
                    },
                    "candidateState": {
                        "present": True,
                        "requiresReplayAbiValidation": True,
                        "replayAbiValidated": self.replay_abi_validated,
                        "applied": False,
                    },
                    "renderDocReplacement": {
                        "attempted": False,
                        "applied": False,
                        "eventScoped": True,
                    },
                    "workspace": {
                        "workspacePath": str(workspace),
                        "workspaceId": "8" * 32,
                        "captureId": self.current_context["capture"]["captureId"],
                        "revision": returned_revision,
                        "candidateRawSha256": returned_hash,
                    },
                }
            )
        if name == "compile_shader_workspace":
            self.compile_attempts += 1
            if self.compile_attempts == self.fail_compile_attempt:
                return json.dumps({"error": "intentional compile failure"})
            workspace = Path(arguments["workspace_path"]).resolve()
            source = self._source_in_workspace(workspace)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            raw_hash = hashlib.sha256(
                (
                    "candidate|%s|%d|%s"
                    % (workspace, self.compile_attempts, source_hash)
                ).encode("utf-8")
            ).hexdigest()
            code_hash = (
                self.compile_code_sha256s[self.compile_attempts - 1]
                if self.compile_attempts <= len(self.compile_code_sha256s)
                else "5" * 64
            )
            candidate = {
                "revision": self.compile_attempts,
                "rawSha256": raw_hash,
                "codeSha256": code_hash,
                "sourceSha256": source_hash,
                "sourcePath": str(source),
                "validated": False,
            }
            self.compiled_candidates[str(workspace)] = candidate
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                    "operation": "compile",
                    "result": {
                        "workspacePath": str(workspace),
                        "revision": candidate["revision"],
                        "sourceSha256": source_hash,
                        "candidateBlobPath": str(
                            workspace / "revisions" / str(self.compile_attempts) / "shader.bin"
                        ),
                        "rawSha256": raw_hash,
                        "codeSha256": code_hash,
                        "kind": "dxil",
                        "requiresReplayAbiValidation": True,
                        "replayAbiValidated": False,
                    },
                    "candidateState": {
                        "present": True,
                        "requiresReplayAbiValidation": True,
                        "replayAbiValidated": False,
                        "applied": False,
                    },
                    "renderDocReplacement": {
                        "attempted": False,
                        "applied": False,
                    },
                }
            )
        if name == "get_shader_workspace_status":
            workspace = Path(arguments["workspace_path"]).resolve()
            candidate = self.compiled_candidates.get(str(workspace))
            if candidate is None:
                return json.dumps({"ok": True, "tool": name, "arguments": arguments})
            source = Path(candidate["sourcePath"])
            current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            validated = candidate["validated"] is True
            suffixes = source.name.split(".")[1:]
            source_format = "-".join(suffixes) if suffixes else "hlsl"
            code_equal = candidate["codeSha256"] == self.captured_code_sha256
            candidate_matches_untouched = (
                current_hash == candidate["sourceSha256"]
            )
            candidate_abi_compatible = bool(
                candidate_matches_untouched and validated
            )
            fidelity_classification = (
                "exact-executable-code-match"
                if code_equal
                else (
                    "abi-compatible-executable-code-mismatch"
                    if candidate_abi_compatible
                    else "executable-code-mismatch"
                )
            )
            status = {
                "schema": "renderdoc-ultra-shader-workspace-status/v1",
                "workspacePath": str(workspace),
                "workspaceId": "8" * 32,
                "revision": candidate["revision"],
                "source": {
                    "present": True,
                    "path": str(source),
                    "format": source_format,
                    "currentSha256": current_hash,
                    "lane": "executable-editable",
                    "compileEligible": True,
                },
                "representation": {
                    "present": True,
                    "lane": "executable-editable",
                    "path": str(source),
                    "sha256": current_hash,
                    "compileEligible": True,
                    "applyEligible": False,
                    "abiCompatible": validated,
                    "semanticEquivalenceVerified": False,
                },
                "latestCompiled": {
                    "revision": candidate["revision"],
                    "rawSha256": candidate["rawSha256"],
                    "codeSha256": candidate["codeSha256"],
                    "sourceSha256": candidate["sourceSha256"],
                    "requiresReplayAbiValidation": not validated,
                    "replayAbiValidated": validated,
                    "abiCompatible": validated,
                    "applied": False,
                },
                "reconstructionFidelity": {
                    "schema": "renderdoc-ultra-shader-reconstruction-fidelity/v1",
                    "scope": "captured-baseline-vs-untouched-route-round-trip",
                    "available": True,
                    "baselineCompileVerified": True,
                    "capturedCodeSha256": self.captured_code_sha256,
                    "reconstructedCodeSha256": candidate["codeSha256"],
                    "codeSha256Equal": code_equal,
                    "currentSourceMatchesUntouchedReconstruction": (
                        candidate_matches_untouched
                    ),
                    "currentCandidateMatchesUntouchedReconstruction": (
                        candidate_matches_untouched
                    ),
                    "currentCandidateReplayAbiCompatible": (
                        candidate_abi_compatible
                    ),
                    "classification": fidelity_classification,
                    "semanticEquivalenceVerified": False,
                    "note": "fixed fake fidelity evidence",
                },
                "applied": False,
            }
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                    "operation": "status",
                    "result": status,
                    "candidateState": {
                        "present": True,
                        "requiresReplayAbiValidation": True,
                        "replayAbiValidated": validated,
                        "applied": False,
                    },
                    "renderDocReplacement": {
                        "attempted": False,
                        "applied": False,
                    },
                }
            )
        if name == "start_shader_workspace_watch":
            return json.dumps(
                {
                    "result": {
                        "watchId": "f" * 32,
                        "compileSuccesses": 0,
                        "compileFailures": 0,
                    }
                }
            )
        if name == "apply_shader_workspace_candidate_family":
            return json.dumps(
                {
                    "applied": True,
                    "shaderFamilyId": "family",
                    "rollback": {
                        "workspacePath": arguments["workspace_path"],
                        "overrideIds": ["override-1", "override-2"],
                    },
                }
            )
        if name == "rollback_shader_workspace_override_family":
            return json.dumps({"applied": False, "rolledBack": True})
        if name == "apply_shader_workspace_shared_resource":
            revision = arguments.get("expected_revision", 1)
            candidate_hash = arguments.get(
                "expected_candidate_raw_sha256", "3" * 64
            )
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                    "applied": True,
                    "binding": {
                        "revision": revision,
                        "candidateRawSha256": candidate_hash,
                    },
                    "replacement": {
                        "replacementId": "a" * 32,
                        "originalResourceId": 902,
                        "captureSessionId": SESSION,
                        "revision": revision,
                        "candidateRawSha256": candidate_hash,
                    },
                    "rollback": {
                        "workspacePath": arguments["workspace_path"],
                        "replacementId": "a" * 32,
                        "tool": "rollback_shader_workspace_shared_resource",
                    },
                }
            )
        if name == "rollback_shader_workspace_shared_resource":
            if self.fail_shared_rollback:
                return json.dumps({"error": "intentional rollback failure"})
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                    "applied": False,
                    "replacement": {
                        "replacementId": arguments["replacement_id"],
                        "cleared": True,
                        "originalShaderPreserved": True,
                    },
                }
            )
        if name == "reverse_shader":
            return json.dumps({"stage": arguments["stage"], "disassembly": "ret"})
        if name == "get_current_shader_representation":
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-current-shader-representation/v1",
                    "present": True,
                    "lane": self.current_representation_lane,
                    "kind": "renderdoc-source-disassembly-bundle",
                    "path": self.current_representation_path,
                    "sha256": "4" * 64,
                    "eventId": int(arguments["event_id"]),
                    "shaderResourceId": 902,
                    "stage": arguments["stage"],
                    "entryPoint": "main",
                    "rawSha256": "2" * 64,
                    "compileEligible": False,
                    "applyEligible": False,
                    "abiCompatible": False,
                    "semanticEquivalenceVerified": False,
                    "diagnostics": [],
                }
            )
        if name == "prepare_shader_workspace":
            if self.prepare_error:
                return json.dumps({"error": self.prepare_error})
            root = Path(arguments["workspace_path"])
            representation = None
            if self.prepare_representation_lane:
                explanation = root / "explanation"
                explanation.mkdir(parents=True)
                artifact = explanation / "native-ir.txt"
                artifact.write_text("mov r0, r0\n", encoding="utf-8")
                representation = {
                    "present": True,
                    "lane": self.prepare_representation_lane,
                    "kind": "renderdoc-native-disassembly",
                    "path": str(artifact),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "compileEligible": False,
                    "applyEligible": False,
                    "abiCompatible": False,
                    "semanticEquivalenceVerified": False,
                }
            else:
                (root / "source").mkdir(parents=True)
                (root / "source" / "shader.hlsl").write_text(
                    self.workspace_source
                    or "float4 main() : SV_Target { return 1; }\n",
                    encoding="utf-8",
                )
            result = {
                "workspacePath": str(root),
                "selectedShader": {
                    "resourceId": 902,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": "2" * 64,
                },
            }
            if representation is not None:
                result["representation"] = representation
            return json.dumps(
                {
                    "result": result
                }
            )
        if name == "get_draw_call_details" and self.large_tool_output:
            return self.large_tool_output
        return json.dumps({"ok": True, "tool": name, "arguments": arguments})


class _RecoveryFakeMCP(_FakeMCP):
    """Compile-only workspace fake with a hash-bound recovery contract."""

    def __init__(self, *args, representation_lane="explanatory-ir", **kwargs):
        super().__init__(*args, **kwargs)
        self.prepare_representation_lane = representation_lane
        self.current_representation_lane = representation_lane
        self.tools["promote_shader_workspace_explanation"] = _spec(
            "promote_shader_workspace_explanation"
        )

    @staticmethod
    def _canonical_hash(value: dict) -> str:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _plans(self, workspace: Path) -> tuple[dict, dict]:
        route = {
            "schema": "renderdoc-ultra-shader-route-plan/v1",
            "schemaVersion": 1,
            "routeCatalogPath": str(workspace / "fixed-routes.json"),
            "routeCatalogSha256": "7" * 64,
            "toolRegistryPath": str(workspace / "fixed-tools.json"),
            "toolRegistrySha256": "8" * 64,
            "inputFormat": "dxil",
            "editableFormat": "hlsl",
            "stage": "fragment",
            "decompile": [],
            "compile": [
                {
                    "hop": 1,
                    "capability": {
                        "routeId": "test-hlsl-dxil",
                        "toolId": "fixed-dxc",
                        "operation": "compile",
                        "inputFormat": "hlsl",
                        "outputFormat": "dxil",
                    },
                    "operation": {
                        "toolId": "fixed-dxc",
                        "operation": "compile",
                        "configDigest": "9" * 64,
                        "executableSha256": "6" * 64,
                        "fixedFileHashes": {"integrated-provider": "5" * 64},
                    },
                }
            ],
        }
        route["planSha256"] = self._canonical_hash(route)
        recovery = {
            "schema": "renderdoc-ultra-shader-workspace-recovery/v1",
            "schemaVersion": 1,
            "mode": "compile-only-recovery",
            "diagnostics": [
                {
                    "phase": "decompile",
                    "category": "route-unavailable",
                }
            ],
            "allowedActions": [
                {
                    "id": "inspect-native-ir",
                    "owner": "program",
                    "requiresCompileRoute": False,
                    "producesExecutableSource": False,
                },
                {
                    "id": "construct-provisional-source",
                    "owner": "ai",
                    "requiresExplicitHashBoundPromotion": True,
                    "producesExecutableSource": True,
                },
            ],
            "decompileCandidates": [],
            "compileCandidates": [["test-hlsl-dxil"]],
            "configuredButUnavailable": {"decompile": [], "compile": []},
            "aiPolicy": {
                "mayClassifyDiagnostics": True,
                "mayChooseOnlyListedActions": True,
                "mayAuthorProvisionalSource": True,
                "maySupplyCommandOrExecutable": False,
                "mayAuthorizeApply": False,
            },
            "sourceConstructionEligible": True,
            "assetSourceSeedEligibleAfterVerification": True,
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
        }
        recovery["planSha256"] = self._canonical_hash(recovery)
        return route, recovery

    async def call_tool(self, name, arguments):
        if name == "prepare_shader_workspace":
            raw = await super().call_tool(name, arguments)
            value = json.loads(raw)
            root = Path(arguments["workspace_path"]).resolve()
            (root / "source").mkdir(exist_ok=True)
            route, recovery = self._plans(root)
            representation = value["result"]["representation"]
            representation["byteSize"] = Path(representation["path"]).stat().st_size
            representation["shader"] = {
                "resourceId": 902,
                "stage": "fragment",
                "entryPoint": "main",
            }
            representation["baseline"] = {"rawSha256": "2" * 64}
            value["result"]["created"] = {
                "workspacePath": str(root),
                "workspaceId": "a" * 32,
                "captureId": self.current_context["capture"]["captureId"],
                "baselineRawSha256": "2" * 64,
                "routePlan": route,
                "recoveryPlan": recovery,
            }
            return json.dumps(value)
        if name == "promote_shader_workspace_explanation":
            self.events.append(("mcp", name, dict(arguments)))
            workspace = Path(arguments["workspace_path"]).resolve()
            representation = next(
                path
                for path in (workspace / "explanation").iterdir()
                if path.name in {"native-ir.txt", "captured-binary.txt"}
            )
            promoted = workspace / "explanation" / "promoted.hlsl"
            source = promoted.read_text(encoding="utf-8", errors="strict")
            if hashlib.sha256(representation.read_bytes()).hexdigest() != arguments[
                "expected_representation_sha256"
            ]:
                return json.dumps({"error": "representation hash mismatch"})
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source_hash != arguments["expected_source_sha256"]:
                return json.dumps({"error": "source hash mismatch"})
            if arguments["acknowledge_semantic_equivalence_unverified"] is not True:
                return json.dumps({"error": "missing acknowledgement"})
            destination = workspace / "source" / "shader.hlsl"
            destination.write_text(source, encoding="utf-8")
            return json.dumps(
                {
                    "result": {
                        "workspacePath": str(workspace),
                        "sourcePath": str(destination),
                        "sourceSha256": source_hash,
                        "lane": "executable-editable",
                        "compileEligible": True,
                        "semanticEquivalenceVerified": False,
                        "applied": False,
                    }
                }
            )
        return await super().call_tool(name, arguments)


class _AlternateRecoveryFakeMCP(_RecoveryFakeMCP):
    """Primary decompiler failure with one fixed registered alternative."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.returned_attempt_source_workspace_id = "a" * 32
        self.additional_alternate_route_ids: list[str] = []
        self.additional_compile_route_ids: list[str] = []
        self.failed_recovery_candidate_indices: set[int] = set()
        self.failed_recovery_candidate_pairs: set[tuple[int, int]] = set()
        self.prepare_failure_action_message = (
            "preserve-evidence, try-registered-alternate-route"
        )
        self.tools["try_shader_workspace_recovery_candidate"] = _spec(
            "try_shader_workspace_recovery_candidate"
        )

    @staticmethod
    def _route_hop(route_id: str, operation: str) -> dict:
        return {
            "hop": 1,
            "capability": {
                "routeId": route_id,
                "toolId": "fixed-source-integrated-tool",
                "operation": operation,
                "inputFormat": "dxil" if operation == "decompile" else "hlsl",
                "outputFormat": "hlsl" if operation == "decompile" else "dxil",
            },
            "operation": {
                "toolId": "fixed-source-integrated-tool",
                "operation": operation,
                "configDigest": "9" * 64,
                "executableSha256": "6" * 64,
                "fixedFileHashes": {"integrated-provider": "5" * 64},
            },
        }

    def _plans(self, workspace: Path) -> tuple[dict, dict]:
        decompile_candidates = [
            ["fixed-primary-dxil-hlsl"],
            ["fixed-alternate-dxil-hlsl"],
            *[[route_id] for route_id in self.additional_alternate_route_ids],
        ]
        compile_candidates = [
            ["fixed-hlsl-dxil"],
            *[[route_id] for route_id in self.additional_compile_route_ids],
        ]
        candidate_route_pairs = [
            {
                "decompileCandidateIndex": decompile_index,
                "compileCandidateIndex": compile_index,
                "decompileRouteIds": decompile_path,
                "compileRouteIds": compile_path,
            }
            for decompile_index, decompile_path in enumerate(decompile_candidates)
            for compile_index, compile_path in enumerate(compile_candidates)
            if (decompile_index, compile_index) != (0, 0)
        ]
        route = {
            "schema": "renderdoc-ultra-shader-route-plan/v1",
            "schemaVersion": 1,
            "routeCatalogPath": str(workspace / "fixed-routes.json"),
            "routeCatalogSha256": "7" * 64,
            "toolRegistryPath": str(workspace / "fixed-tools.json"),
            "toolRegistrySha256": "8" * 64,
            "inputFormat": "dxil",
            "editableFormat": "hlsl",
            "stage": "fragment",
            "decompile": [
                self._route_hop("fixed-primary-dxil-hlsl", "decompile")
            ],
            "compile": [self._route_hop("fixed-hlsl-dxil", "compile")],
        }
        route["planSha256"] = self._canonical_hash(route)
        recovery = {
            "schema": "renderdoc-ultra-shader-workspace-recovery/v1",
            "schemaVersion": 1,
            "mode": "round-trip",
            "diagnostics": [
                {
                    "phase": "decompile",
                    "category": "tool-rejected-input",
                    "allowedActions": [
                        "preserve-evidence",
                        "try-registered-alternate-route",
                    ],
                }
            ],
            "allowedActions": [
                {
                    "id": "inspect-native-ir",
                    "owner": "program",
                    "requiresCompileRoute": False,
                    "producesExecutableSource": False,
                },
                {
                    "id": "run-registered-decompiler",
                    "owner": "program",
                    "routeIds": decompile_candidates[0],
                    "requiresCompileRoute": True,
                    "producesExecutableSource": True,
                    "producesExplanatoryIr": False,
                },
                {
                    "id": "retry-registered-decompiler",
                    "owner": "program-or-ai-selection",
                    "candidateRoutePaths": decompile_candidates[1:],
                    "candidateRoutePairs": candidate_route_pairs,
                    "requiresCompileRoute": True,
                    "producesExecutableSource": True,
                    "producesExplanatoryIr": False,
                },
                {
                    "id": "seed-reviewed-asset-source",
                    "owner": "program-or-ai-selection",
                    "requiresExactIdentityAndFreshAbiValidation": True,
                    "producesExecutableSource": True,
                },
                {
                    "id": "construct-provisional-source",
                    "owner": "ai",
                    "requiresExplicitHashBoundPromotion": True,
                    "producesExecutableSource": True,
                },
            ],
            "decompileCandidates": decompile_candidates,
            "compileCandidates": compile_candidates,
            "configuredButUnavailable": {"decompile": [], "compile": []},
            "aiPolicy": {
                "mayClassifyDiagnostics": True,
                "mayChooseOnlyListedActions": True,
                "mayAuthorProvisionalSource": True,
                "maySupplyCommandOrExecutable": False,
                "mayAuthorizeApply": False,
            },
            "sourceConstructionEligible": True,
            "assetSourceSeedEligibleAfterVerification": True,
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
        }
        recovery["planSha256"] = self._canonical_hash(recovery)
        return route, recovery

    async def call_tool(self, name, arguments):
        if name == "prepare_shader_workspace":
            raw = await super().call_tool(name, arguments)
            value = json.loads(raw)
            value["result"]["representation"]["diagnostics"] = [
                {
                    "code": "recovery-category:tool-rejected-input",
                    "message": self.prepare_failure_action_message,
                }
            ]
            value["result"]["decompiled"] = value["result"]["representation"]
            return json.dumps(value)
        if name != "try_shader_workspace_recovery_candidate":
            return await super().call_tool(name, arguments)

        self.events.append(("mcp", name, dict(arguments)))
        source_workspace = Path(arguments["source_workspace_path"]).resolve()
        target = Path(arguments["workspace_path"]).resolve()
        _source_route, source_recovery = self._plans(source_workspace)
        decompile_index = arguments.get("decompile_candidate_index")
        compile_index = arguments.get("compile_candidate_index")
        candidates = source_recovery["decompileCandidates"]
        compile_candidates = source_recovery["compileCandidates"]
        if (
            type(decompile_index) is not int
            or decompile_index < 0
            or decompile_index >= len(candidates)
            or type(compile_index) is not int
            or compile_index < 0
            or compile_index >= len(compile_candidates)
            or (decompile_index, compile_index) == (0, 0)
        ):
            return json.dumps({"error": "recovery candidate index mismatch"})
        selected_decompile_route_ids = candidates[decompile_index]
        selected_compile_route_ids = compile_candidates[compile_index]
        expected = {
            "expected_recovery_plan_sha256": source_recovery["planSha256"],
            "expected_source_workspace_id": "a" * 32,
            "expected_baseline_raw_sha256": "2" * 64,
            "expected_capture_id": self.current_context["capture"]["captureId"],
        }
        if any(arguments.get(key) != value for key, value in expected.items()):
            return json.dumps({"error": "recovery binding mismatch"})
        if target == source_workspace or target.exists():
            return json.dumps({"error": "recovery workspace was not fresh"})

        target_route, target_recovery = self._plans(target)
        target_route["decompile"] = [
            self._route_hop(selected_decompile_route_ids[0], "decompile")
        ]
        target_route["compile"] = [
            self._route_hop(selected_compile_route_ids[0], "compile")
        ]
        unsigned_route = dict(target_route)
        unsigned_route.pop("planSha256")
        target_route["planSha256"] = self._canonical_hash(unsigned_route)
        target_workspace_id = hashlib.sha256(
            (
                "recovery-workspace-%d-%d" % (decompile_index, compile_index)
            ).encode("utf-8")
        ).hexdigest()[:32]
        attempt = {
            "operation": "recovery-candidate-attempt",
            "revision": 0,
            "status": "selected",
            "sourceWorkspacePath": str(source_workspace),
            "sourceWorkspaceId": self.returned_attempt_source_workspace_id,
            "sourceRecoveryPlanSha256": source_recovery["planSha256"],
            "decompileCandidateIndex": decompile_index,
            "compileCandidateIndex": compile_index,
            "decompileRouteIds": selected_decompile_route_ids,
            "compileRouteIds": selected_compile_route_ids,
            "targetWorkspaceId": target_workspace_id,
            "targetRoutePlanSha256": target_route["planSha256"],
        }
        if (
            decompile_index in self.failed_recovery_candidate_indices
            or (decompile_index, compile_index)
            in self.failed_recovery_candidate_pairs
        ):
            explanation = target / "explanation"
            explanation.mkdir(parents=True)
            artifact = explanation / "native-ir.txt"
            artifact.write_text(
                "fixed route %d rejected the captured shader\n" % decompile_index,
                encoding="utf-8",
            )
            representation = {
                "present": True,
                "lane": "explanatory-ir",
                "kind": "renderdoc-native-disassembly",
                "path": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "compileEligible": False,
                "applyEligible": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
                "shader": {
                    "resourceId": 902,
                    "stage": "fragment",
                    "entryPoint": "main",
                    "rawSha256": "2" * 64,
                },
                "baseline": {"rawSha256": "2" * 64},
                "diagnostics": [
                    {
                        "code": "recovery-category:tool-rejected-input",
                        "message": (
                            "preserve-evidence, try-registered-alternate-route"
                        ),
                    }
                ],
            }
        else:
            source_root = target / "source"
            source_root.mkdir(parents=True)
            source = source_root / "shader.hlsl"
            source.write_text(
                "float4 main() : SV_Target { return float4(0.75, 0.5, 0.25, 1); }\n",
                encoding="utf-8",
            )
            representation = {
                "present": True,
                "lane": "executable-editable",
                "kind": "reconstructed-hlsl",
                "format": "hlsl",
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "compileEligible": True,
                "applyEligible": False,
                "abiCompatible": False,
                "semanticEquivalenceVerified": False,
            }
        return json.dumps(
            {
                "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                "operation": "recovery-candidate-attempt",
                "result": {
                    "sourceWorkspacePath": str(source_workspace),
                    "workspacePath": str(target),
                    "created": {
                        "workspacePath": str(target),
                        "workspaceId": target_workspace_id,
                        "captureId": self.current_context["capture"]["captureId"],
                        "shader": {
                            "resourceId": 902,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "rawSha256": "2" * 64,
                        },
                        "baselineRawSha256": "2" * 64,
                        "routePlan": target_route,
                        "recoveryPlan": target_recovery,
                        "recoveryAttempt": attempt,
                    },
                    "representation": representation,
                    "applied": False,
                },
                "candidateState": {
                    "present": False,
                    "requiresReplayAbiValidation": False,
                    "replayAbiValidated": False,
                    "applied": False,
                },
                "renderDocReplacement": {
                    "attempted": False,
                    "applied": False,
                },
            }
        )


class _RoutedSourceFakeMCP(_FakeMCP):
    SOURCE_NAMES = {
        "glsl": "shader.glsl",
        "dxbc-asm": "shader.dxbc.asm",
    }

    def __init__(self, editable_format: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.editable_format = editable_format

    async def call_tool(self, name, arguments):
        if name != "prepare_shader_workspace":
            return await super().call_tool(name, arguments)
        self.events.append(("mcp", name, dict(arguments)))
        root = Path(arguments["workspace_path"]).resolve()
        source_dir = root / "source"
        source_dir.mkdir(parents=True)
        source_path = source_dir / self.SOURCE_NAMES[self.editable_format]
        source_text = (
            "#version 450\nlayout(location=0) out vec4 colour;\n"
            "void main(){ colour=vec4(1.0); }\n"
            if self.editable_format == "glsl"
            else "ps_5_0\ndcl_output o0.xyzw\nmov o0.xyzw, l(1, 0, 0, 1)\nret\n"
        )
        source_path.write_text(source_text, encoding="utf-8")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        route_plan = {
            "editableFormat": self.editable_format,
            "decompile": [{"hop": 1}],
            "compile": [{"hop": 1}],
        }
        return json.dumps(
            {
                "result": {
                    "workspacePath": str(root),
                    "selectedShader": {
                        "resourceId": 902,
                        "stage": "fragment",
                        "entryPoint": "main",
                        "rawSha256": "2" * 64,
                    },
                    "created": {
                        "workspacePath": str(root),
                        "routePlan": route_plan,
                    },
                    "representation": {
                        "present": True,
                        "lane": "executable-editable",
                        "kind": "editable-%s" % self.editable_format,
                        "format": self.editable_format,
                        "path": str(source_path),
                        "sha256": digest,
                        "compileEligible": True,
                        "applyEligible": False,
                        "abiCompatible": False,
                        "semanticEquivalenceVerified": False,
                    },
                }
            }
        )


class _ActiveProjectFakeMCP(_FakeMCP):
    """New-server fake with a process-owned active project capability set."""

    ACTIVE_NAMES = {
        "ensure_active_replay_project",
        "get_active_replay_project_status",
        "prepare_active_capture_project",
        "get_active_shader_workspace_path",
        "register_active_project_workspace",
        "seed_active_shader_workspace",
    }

    def __init__(self, project_root: Path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tools.update({name: _spec(name) for name in self.ACTIVE_NAMES})
        self.active_root = Path(project_root).resolve()
        self.active_shader_root = self.active_root / "workspaces" / "shaders"
        self.active_shader_root.mkdir(parents=True)
        self.active_workspace_id = "7" * 32
        self.active_scope_configured = True
        self.active_prepare_calls = 0

    def _active_status(self) -> dict:
        return {
            "schema": "replaylab-project-workspace/v1",
            "schemaVersion": 1,
            "workspaceId": self.active_workspace_id,
            "name": "Auto Game",
            "state": "bound" if self.active_scope_configured else "pending-scope",
            "projectRoot": str(self.active_root),
            "scope": {
                "gameId": "game-id" if self.active_scope_configured else "",
                "buildId": "build-id" if self.active_scope_configured else "",
                "platform": "windows-x64" if self.active_scope_configured else "",
                "apiFamily": "d3d11" if self.active_scope_configured else "",
            },
            "scopeConfigured": self.active_scope_configured,
            "assetRoot": str(self.active_root / "unpacked"),
            "assetRootConfigured": True,
            "assetRootAvailable": True,
            "paths": {"shaderWorkspaces": str(self.active_shader_root)},
            "policy": {
                "physicalKnowledgeReuseAllowed": self.active_scope_configured,
                "aiProvisionalAutoApply": False,
                "sourceSeedIsApplyAuthorization": False,
                "liveAbiRevalidationRequired": True,
            },
        }

    def _active_plan(self) -> dict:
        if not self.active_scope_configured:
            return {
                "schema": "replaylab-future-frame-load-plan/v1",
                "schemaVersion": 1,
                "scopeConfigured": False,
                "blocked": True,
                "blockedReason": "game/build not configured",
                "reusePlan": None,
                "policy": {
                    "aiProvisionalAutoApply": False,
                    "autoApplyAuthorized": False,
                },
            }
        return {
            "schema": "replaylab-future-frame-load-plan/v1",
            "schemaVersion": 1,
            "scopeConfigured": True,
            "blocked": False,
            "blockedReason": "",
            "reusePlan": {
                "schema": "renderdoc-ultra-project-reuse-plan/v1",
                "schemaVersion": 1,
                "projectId": "9" * 32,
                "projectPath": str(self.active_root / "replay-project"),
                "captureId": self.current_context["capture"]["captureId"],
                "matchedShaderCount": len(self.project_matches),
                "matches": self.project_matches,
                "policy": {
                    "automaticHashMatching": True,
                    "rawExactOrCodeInterfaceLayoutExactOnly": True,
                    "oldWorkspaceManifestReused": False,
                    "oldCaptureAbiReportReused": False,
                    "oldMaterialSessionReused": False,
                    "autoApplyAuthorized": False,
                    "compileRequiredAfterSourceSeed": True,
                    "liveAbiRevalidationRequiredBeforeShaderApply": True,
                    "liveMaterialLayoutRevalidationRequired": True,
                },
            },
            "policy": {
                "aiProvisionalAutoApply": False,
                "autoApplyAuthorized": False,
            },
        }

    def _active_workspace(self) -> Path:
        capture_id = self.current_context["capture"]["captureId"]
        return (
            self.active_shader_root
            / capture_id
            / "fragment"
            / ("rid-902-%s" % ("2" * 64))
        ).resolve()

    def _active_workspace_status(self, workspace: Path) -> dict:
        source = workspace / "source" / "shader.hlsl"
        current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        candidate = self.compiled_candidates.get(str(workspace.resolve()))
        status = {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(workspace),
            "workspaceId": "8" * 32,
            "capture": {
                "captureId": self.current_context["capture"]["captureId"]
            },
            "shader": {
                "resourceId": 902,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": "2" * 64,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "source": {
                "present": True,
                "path": str(source),
                "currentSha256": current_hash,
                "decompiledSha256": "5" * 64,
                "modifiedSinceDecompile": current_hash != "5" * 64,
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "representation": {
                "present": True,
                "lane": "executable-editable",
                "path": str(source),
                "sha256": current_hash,
                "compileEligible": True,
                "applyEligible": False,
                "abiCompatible": bool(
                    candidate and candidate.get("validated") is True
                ),
                "semanticEquivalenceVerified": False,
            },
            "revision": candidate["revision"] if candidate else 0,
            "latestCompiled": (
                {
                    "revision": candidate["revision"],
                    "rawSha256": candidate["rawSha256"],
                    "codeSha256": candidate["codeSha256"],
                    "sourceSha256": candidate["sourceSha256"],
                    "requiresReplayAbiValidation": candidate.get("validated") is not True,
                    "replayAbiValidated": candidate.get("validated") is True,
                    "abiCompatible": candidate.get("validated") is True,
                    "applied": False,
                }
                if candidate
                else None
            ),
            "applied": False,
        }
        return status

    async def call_tool(self, name, arguments):
        if name not in self.ACTIVE_NAMES and name != "get_shader_workspace_status":
            result = await super().call_tool(name, arguments)
            if name == "prepare_shader_workspace" and not self.prepare_error:
                root = Path(arguments["workspace_path"])
                (root / "manifest.json").write_text("{}\n", encoding="utf-8")
            return result

        self.events.append(("mcp", name, dict(arguments)))
        capture_id = self.current_context["capture"]["captureId"]
        session = self.current_context["capture"]["captureSessionId"]
        if name in {
            "ensure_active_replay_project",
            "get_active_replay_project_status",
        }:
            return json.dumps(
                {
                    "schema": "replaylab-active-project-mcp/v1",
                    "schemaVersion": 1,
                    "project": self._active_status(),
                    "applied": False,
                }
            )
        if name == "prepare_active_capture_project":
            self.active_prepare_calls += 1
            return json.dumps(
                {
                    "schema": "replaylab-active-capture-prepare/v1",
                    "schemaVersion": 1,
                    "capture": {
                        "captureId": capture_id,
                        "captureSessionId": session,
                    },
                    "project": self._active_status(),
                    "stagedCatalog": {
                        "captureId": capture_id,
                        "catalogSha256": "3" * 64,
                    },
                    "reusePlan": self._active_plan(),
                    "symbolResolution": {
                        "attempted": True,
                        "blocked": False,
                        "reason": "",
                        "scanCacheHit": self.active_prepare_calls > 1,
                        "match": {"verifiedCount": 3, "unresolvedCount": 1},
                        "unresolved": {"count": 1},
                        "aliasTable": {"verifiedOnly": True},
                        "policy": {
                            "verifiedAliasesOnly": True,
                            "aiProvisionalAutoApply": False,
                        },
                    },
                    "applied": False,
                }
            )
        if name == "get_active_shader_workspace_path":
            workspace = self._active_workspace()
            workspace.mkdir(parents=True, exist_ok=True)
            return json.dumps(
                {
                    "schema": "replaylab-active-shader-workspace-path/v1",
                    "schemaVersion": 1,
                    "captureId": capture_id,
                    "captureSessionId": session,
                    "eventId": int(arguments["event_id"]),
                    "stage": "fragment",
                    "entryPoint": "main",
                    "shaderResourceId": 902,
                    "rawSha256": "2" * 64,
                    "workspacePath": str(workspace),
                    "applied": False,
                }
            )
        if name == "get_shader_workspace_status":
            status = self._active_workspace_status(
                Path(arguments["workspace_path"]).resolve()
            )
            candidate = self.compiled_candidates.get(status["workspacePath"])
            validated = bool(candidate and candidate.get("validated") is True)
            return json.dumps(
                {
                    "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                    "operation": "status",
                    "result": status,
                    "candidateState": {
                        "present": candidate is not None,
                        "requiresReplayAbiValidation": bool(candidate and not validated),
                        "replayAbiValidated": validated,
                        "applied": False,
                    },
                    "renderDocReplacement": {
                        "attempted": False,
                        "applied": False,
                    },
                }
            )
        if name == "register_active_project_workspace":
            return json.dumps(
                {
                    "schema": "replaylab-active-workspace-registration/v1",
                    "captureId": capture_id,
                    "captureSessionId": session,
                    "workspacePath": arguments["workspace_path"],
                    "result": {
                        "knowledge": {"knowledgeId": "6" * 32},
                        "applied": False,
                    },
                    "applied": False,
                }
            )
        if name == "seed_active_shader_workspace":
            workspace = Path(arguments["workspace_path"])
            source = workspace / "source" / "shader.hlsl"
            source.write_text(self.project_source, encoding="utf-8")
            digest = hashlib.sha256(self.project_source.encode("utf-8")).hexdigest()
            return json.dumps(
                {
                    "schema": "replaylab-active-workspace-seed/v1",
                    "captureId": capture_id,
                    "captureSessionId": session,
                    "workspacePath": str(workspace),
                    "result": {
                        "schema": "renderdoc-ultra-project-workspace-seed/v1",
                        "knowledgeId": arguments["knowledge_id"],
                        "workspacePath": str(workspace),
                        "sourceSha256": digest,
                        "applied": False,
                        "compiled": False,
                        "requiresCompile": True,
                        "requiresLiveAbiRevalidation": True,
                    },
                    "applied": False,
                }
            )
        raise AssertionError(name)


class _LiveEditFakeMCP(_FakeMCP):
    LIVE_ID = "5" * 32

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            "start_shader_workspace_live_edit",
            "poll_shader_workspace_live_edit",
            "stop_shader_workspace_live_edit",
        ):
            self.tools[name] = _spec(name)
        self.live_workspace = ""
        self.live_poll_applies = False
        self.live_replacement_active = False

    def _live_state(self, state: str = "running") -> dict:
        replacement = None
        rollback = None
        successes = 0
        if self.live_replacement_active:
            successes = 1
            replacement = {
                "replacementId": "a" * 32,
                "revision": 1,
                "candidateRawSha256": "3" * 64,
                "originalResourceId": 902,
                "resourceScoped": True,
                "eventScoped": False,
                "allOriginalResourceUsesAffected": True,
                "originalShaderPreserved": True,
            }
            rollback = {
                "tool": "rollback_shader_workspace_shared_resource",
                "workspacePath": self.live_workspace,
                "replacementId": "a" * 32,
            }
        return {
            "schema": "replaylab-shader-live-edit/v1",
            "schemaVersion": 1,
            "liveEditId": self.LIVE_ID,
            "state": state,
            "workspacePath": self.live_workspace,
            "watchId": "4" * 32,
            "autoApplyCompatibleSaves": True,
            "applyScope": "captured-shader-resource",
            "applySuccesses": successes,
            "applyRejections": 0,
            "applyFailures": 0,
            "activeReplacement": replacement,
            "rollback": rollback,
            "policy": {
                "newSaveRequired": True,
                "freshCompileRequired": True,
                "freshReplayAbiValidationRequired": True,
                "freshLiveCaptureValidationRequired": True,
                "sharedResourceIdentityPreserved": True,
                "originalShaderPreserved": True,
                "aiCanBypassApplyGate": False,
            },
        }

    def _live_envelope(self, operation: str, state: str = "running") -> str:
        result = self._live_state(state)
        return json.dumps(
            {
                "schema": "replaylab-shader-live-edit-mcp/v1",
                "schemaVersion": 1,
                "operation": operation,
                "result": result,
                "appliedByThisCall": False,
                "backgroundAutoApplyAuthorized": True,
                "applyScope": "captured-shader-resource",
                "rollback": result["rollback"],
                "policy": {
                    "newSaveRequired": True,
                    "freshCompileRequired": True,
                    "freshReplayAbiValidationRequired": True,
                    "freshLiveCaptureValidationRequired": True,
                    "sharedResourceIdentityPreserved": True,
                    "distinctResourceIdsNeverMerged": True,
                    "originalShaderPreserved": True,
                    "aiCanBypassApplyGate": False,
                },
            }
        )

    async def call_tool(self, name, arguments):
        if name == "start_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            self.live_workspace = str(Path(arguments["workspace_path"]).resolve())
            return self._live_envelope("start")
        if name == "poll_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            if self.live_poll_applies:
                self.live_replacement_active = True
            return self._live_envelope("poll")
        if name == "stop_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            return self._live_envelope("stop", "stopped")
        if name == "rollback_shader_workspace_shared_resource":
            result = await super().call_tool(name, arguments)
            self.live_replacement_active = False
            return result
        return await super().call_tool(name, arguments)


class _PendingActiveLiveEditFakeMCP(_ActiveProjectFakeMCP):
    LIVE_ID = _LiveEditFakeMCP.LIVE_ID
    _live_state = _LiveEditFakeMCP._live_state
    _live_envelope = _LiveEditFakeMCP._live_envelope

    def __init__(self, project_root: Path, *args, **kwargs):
        super().__init__(project_root, *args, **kwargs)
        self.active_scope_configured = False
        for name in (
            "start_shader_workspace_live_edit",
            "poll_shader_workspace_live_edit",
            "stop_shader_workspace_live_edit",
        ):
            self.tools[name] = _spec(name)
        self.live_workspace = ""
        self.live_poll_applies = False
        self.live_replacement_active = False

    async def call_tool(self, name, arguments):
        if name == "start_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            self.live_workspace = str(Path(arguments["workspace_path"]).resolve())
            return self._live_envelope("start")
        if name == "poll_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            return self._live_envelope("poll")
        if name == "stop_shader_workspace_live_edit":
            self.events.append(("mcp", name, dict(arguments)))
            return self._live_envelope("stop", "stopped")
        return await super().call_tool(name, arguments)


class _FakeResponse:
    def __init__(self, output, output_text=""):
        self.output = output
        self.output_text = output_text


class _FakeResponses:
    def __init__(self, responses, events=None):
        self.responses = list(responses)
        self.requests = []
        self.events = events if events is not None else []

    async def create_response(self, **parameters):
        self.events.append(("model", len(self.requests)))
        self.requests.append(parameters)
        if not self.responses:
            raise AssertionError("unexpected model request")
        return self.responses.pop(0)


def _call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": call_id,
    }


class CompanionPolicyTests(unittest.TestCase):
    def test_companion_defaults_prepare_organized_watched_workspace(self):
        with tempfile.TemporaryDirectory() as local_app_data, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": local_app_data}, clear=False
        ):
            parser = _parser()
            arguments = parser.parse_args([])
            self.assertEqual(
                arguments.workspace_root,
                str((Path(local_app_data) / "ReplayLab" / "workspaces").resolve()),
            )
            self.assertTrue(arguments.organize_source)
            self.assertTrue(arguments.watch_saved_shaders)
            config = _config(arguments)
            self.assertTrue(Path(config.workspace_root).is_dir())
            self.assertTrue(config.organize_source)
            self.assertTrue(config.watch_saved_shaders)

            disabled = parser.parse_args(
                ["--no-organize-source", "--no-watch-saved-shaders"]
            )
            self.assertFalse(disabled.organize_source)
            self.assertFalse(disabled.watch_saved_shaders)

    def test_truncated_tool_output_preserves_bounded_path_hints(self):
        raw = json.dumps(
            {
                "workspacePath": r"D:\workspaces\shader",
                "nested": {"jobDirectory": r"D:\workspaces\shader\jobs\1"},
                "bulk": "x" * 20_000,
            }
        )
        marker = json.loads(_truncated_payload(raw, 2_000, "test"))
        self.assertTrue(marker["truncated"])
        self.assertEqual(
            {item["field"] for item in marker["pathHints"]},
            {"workspacePath", "jobDirectory"},
        )
        self.assertLessEqual(len(json.dumps(marker)), 2_000)

    def test_effects_require_exact_one_turn_allow_syntax(self):
        message, tools = parse_authorized_message(
            "/allow apply_shader_workspace_candidate_family | apply this family"
        )
        self.assertEqual(message, "apply this family")
        self.assertEqual(tools, {"apply_shader_workspace_candidate_family"})
        with self.assertRaises(PolicyError):
            parse_authorized_message("/allow not_a_tool | do it")

    def test_apply_requires_successful_same_turn_validation(self):
        workspace_root = str(Path(r"D:\work").resolve())
        policy = TurnPolicy.for_chat(
            {"apply_shader_workspace_candidate_family"},
            {workspace_root},
        )
        args = {"workspace_path": r"D:\work\shader"}
        with self.assertRaisesRegex(PolicyError, "same-turn"):
            policy.check("apply_shader_workspace_candidate_family", args)
        policy.record_result(
            "validate_shader_workspace_candidate",
            args,
            json.dumps({"validation": {"replayAbiValidated": True}}),
        )
        policy.check("apply_shader_workspace_candidate_family", args)

    def test_effectful_workspace_paths_are_confined_to_configured_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = TurnPolicy.for_chat(
                {"compile_shader_workspace"},
                {str(root)},
            )
            policy.check(
                "compile_shader_workspace",
                {"workspace_path": str(root / "shader")},
            )
            with self.assertRaisesRegex(PolicyError, "workspace root"):
                policy.check(
                    "compile_shader_workspace",
                    {"workspace_path": str(root.parent / "outside")},
                )

    def test_shader_organization_requires_allow_and_workspace_confinement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = {
                "begin_shader_workspace_organization",
                "review_shader_workspace_organization",
            }
            default_policy = TurnPolicy.for_chat(readable_roots={str(root)})
            for name in tools:
                self.assertNotIn(name, default_policy.exposed_tools)

            policy = TurnPolicy.for_chat(tools, {str(root)})
            for name in tools:
                policy.check(name, {"workspace_path": str(root / "shader")})
                with self.assertRaisesRegex(PolicyError, "workspace root"):
                    policy.check(
                        name,
                        {"workspace_path": str(root.parent / "outside")},
                    )

    def test_recovery_candidate_policy_confines_source_and_fresh_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(PolicyError, "unknown or non-effectful"):
                TurnPolicy.for_chat(
                    {"try_shader_workspace_recovery_candidate"},
                    {str(root)},
                )
            # Even a deliberately constructed internal policy retains both
            # source/target confinement; ordinary model turns cannot construct it.
            policy = TurnPolicy(
                exposed_tools=frozenset(
                    {"try_shader_workspace_recovery_candidate"}
                ),
                readable_roots=(os.path.normcase(str(root)),),
            )
            arguments = {
                "source_workspace_path": str(root / "source"),
                "workspace_path": str(root / "attempt"),
            }
            policy.check("try_shader_workspace_recovery_candidate", arguments)
            with self.assertRaisesRegex(PolicyError, "distinct new workspace"):
                policy.check(
                    "try_shader_workspace_recovery_candidate",
                    {
                        "source_workspace_path": str(root / "same"),
                        "workspace_path": str(root / "same"),
                    },
                )
            with self.assertRaisesRegex(
                PolicyError, "outside the failed source workspace"
            ):
                policy.check(
                    "try_shader_workspace_recovery_candidate",
                    {
                        "source_workspace_path": str(root / "source"),
                        "workspace_path": str(root / "source" / "nested-attempt"),
                    },
                )
            with self.assertRaisesRegex(PolicyError, "source_workspace_path"):
                policy.check(
                    "try_shader_workspace_recovery_candidate",
                    {
                        "source_workspace_path": str(root.parent / "outside"),
                        "workspace_path": str(root / "attempt-2"),
                    },
                )

    def test_read_only_workspace_inspection_is_also_root_confined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = TurnPolicy.for_chat(readable_roots={str(root)})
            policy.check(
                "get_shader_workspace_status",
                {"workspace_path": str(root / "shader")},
            )
            with self.assertRaisesRegex(PolicyError, "workspace root"):
                policy.check(
                    "get_shader_workspace_status",
                    {"workspace_path": str(root.parent / "outside")},
                )

    def test_read_only_shader_recovery_plan_is_exposed_and_path_confined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            policy = TurnPolicy.for_chat(readable_roots={str(root)})
            self.assertIn("plan_shader_recovery", policy.exposed_tools)
            policy.check(
                "plan_shader_recovery",
                {
                    "shader_path": str(root / "captured.dxil"),
                    "stage": "fragment",
                },
            )
            with self.assertRaisesRegex(PolicyError, "workspace root"):
                policy.check(
                    "plan_shader_recovery",
                    {
                        "shader_path": str(root.parent / "outside.dxil"),
                        "stage": "fragment",
                    },
                )

    def test_replay_project_paths_and_import_artifacts_are_root_confined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            catalog = root / "captures" / "frame.json"
            policy = TurnPolicy.for_chat(
                {"import_replay_project_capture"},
                {str(root)},
            )
            policy.check(
                "import_replay_project_capture",
                {
                    "project_path": str(project),
                    "catalog_path": str(catalog),
                },
            )
            with self.assertRaisesRegex(PolicyError, "catalog_path"):
                policy.check(
                    "import_replay_project_capture",
                    {
                        "project_path": str(project),
                        "catalog_path": str(root.parent / "outside.json"),
                    },
                )

    def test_replay_project_plans_are_read_only_but_seeding_requires_allow(self):
        policy = TurnPolicy.for_chat(readable_roots={str(Path.cwd())})
        self.assertIn("get_replay_project_status", policy.exposed_tools)
        self.assertIn("get_replay_project_reuse_plan", policy.exposed_tools)
        self.assertNotIn(
            "seed_shader_workspace_from_replay_project", policy.exposed_tools
        )

    def test_raw_and_path_enumeration_tools_are_not_default_read(self):
        policy = TurnPolicy.for_chat()
        for name in (
            "get_texture_data",
            "get_buffer_contents",
            "get_post_vs_data",
            "list_captures",
            "identify_drawcalls",
        ):
            self.assertNotIn(name, policy.exposed_tools)

    def test_missing_api_key_fails_before_sdk_import(self):
        clean = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
        with mock.patch.dict(os.environ, clean, clear=True):
            with self.assertRaisesRegex(OpenAIStartupError, "OPENAI_API_KEY"):
                OpenAIResponsesClient()

    def test_model_secrets_are_removed_from_mcp_child_environment(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "secret", "OPENAI_ORG_ID": "org", "SAFE_VALUE": "yes"},
            clear=True,
        ):
            child = _sanitised_child_environment()
        self.assertEqual(child, {"SAFE_VALUE": "yes"})

    def test_default_mcp_launch_uses_the_current_virtualenv_interpreter(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            arguments = _parser().parse_args([])
        command, args, cwd = _mcp_launch(arguments)
        self.assertEqual(command, sys.executable)
        self.assertEqual(args, ["-m", "src.server"])
        self.assertIsNone(cwd)

    def test_explicit_mcp_launch_remains_process_owner_controlled(self):
        arguments = _parser().parse_args(
            [
                "--mcp-command",
                r"C:\trusted\renderdoc-mcp.exe",
                "--mcp-arg=--fixed",
                "--mcp-cwd",
                r"C:\trusted",
            ]
        )
        self.assertEqual(
            _mcp_launch(arguments),
            (
                r"C:\trusted\renderdoc-mcp.exe",
                ["--fixed"],
                r"C:\trusted",
            ),
        )

    def test_project_mode_requires_one_existing_root_and_safe_project_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = CompanionConfig(
                project_root=temporary,
                project="game-a",
                project_game_id="game",
                project_build_id="build-1",
            )
            self.assertEqual(config.project, "game-a")
            with self.assertRaisesRegex(ValueError, "both project_root and project"):
                CompanionConfig(project_root=temporary)
            with self.assertRaisesRegex(ValueError, "safe name"):
                CompanionConfig(
                    project_root=temporary,
                    project="../escape",
                    project_game_id="game",
                    project_build_id="build",
                )
            with self.assertRaisesRegex(ValueError, "existing absolute"):
                CompanionConfig(
                    project_root=str(Path(temporary) / "missing"),
                    project="game",
                    project_game_id="game",
                    project_build_id="build",
                )
            with self.assertRaisesRegex(ValueError, "new project requires"):
                CompanionConfig(project_root=temporary, project="new-project")
            existing = Path(temporary) / "existing-project"
            existing.mkdir()
            self.assertEqual(
                CompanionConfig(
                    project_root=temporary, project="existing-project"
                ).project,
                "existing-project",
            )

    def test_project_cli_preserves_explicit_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            arguments = _parser().parse_args(
                [
                    "--project-root",
                    temporary,
                    "--project",
                    "game-build-d3d11",
                    "--project-game-id",
                    "game",
                    "--project-build-id",
                    "build-42",
                    "--project-platform",
                    "windows-x64",
                    "--project-api-family",
                    "D3D11",
                ]
            )
            config = _config(arguments)
            self.assertEqual(config.project_game_id, "game")
            self.assertEqual(config.project_build_id, "build-42")
            self.assertEqual(config.project_platform, "windows-x64")
            self.assertEqual(config.project_api_family, "D3D11")

    def test_project_root_does_not_expand_shader_workspace_authority(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as projects:
            policy = TurnPolicy.for_chat(
                ["apply_shader_workspace_shared_resource"],
                [workspace],
                [projects],
            )
            policy.check(
                "get_replay_project_status",
                {"project_path": str(Path(projects) / "game")},
            )
            with self.assertRaisesRegex(PolicyError, "workspace_path"):
                policy.check(
                    "apply_shader_workspace_shared_resource",
                    {"workspace_path": str(Path(projects) / "not-a-workspace")},
                )
            with self.assertRaisesRegex(PolicyError, "project_path"):
                policy.check(
                    "get_replay_project_status",
                    {"project_path": str(Path(workspace) / "not-a-project")},
                )


class CompanionEngineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _project_shader_match() -> dict:
        return {
            "shader": {
                "resourceId": 902,
                "stage": "fragment",
                "entryPoint": "main",
                "rawSha256": "2" * 64,
            },
            "workspaceCandidates": [
                {
                    "knowledgeId": "1" * 32,
                    "matchMode": "raw-exact",
                    "sourceSha256": "3" * 64,
                    "sourceSeedEligible": True,
                }
            ],
            "aliasCandidates": [],
            "materialCandidates": [],
        }

    async def test_process_owned_active_project_prepares_once_and_exposes_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "active-project"
            mcp = _ActiveProjectFakeMCP(root)
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()
            self.assertTrue(engine._active_project_enabled)

            context = await engine.get_current_context()
            first = await engine._synchronise_replay_project(context)
            second = await engine._synchronise_replay_project(context)

            self.assertEqual(first, second)
            self.assertEqual(mcp.active_prepare_calls, 1)
            prompt = engine._project_prompt_data(context)
            self.assertEqual(prompt["mode"], "process-owned-active-project")
            self.assertEqual(
                prompt["activeProject"]["workspaceId"], "7" * 32
            )
            self.assertTrue(prompt["symbols"]["verifiedOnly"])
            self.assertTrue(prompt["symbols"]["attempted"])
            self.assertFalse(prompt["symbols"]["aiProvisionalPhysicalUse"])
            self.assertFalse(prompt["reuse"]["autoApplyAuthorized"])

            changed = json.loads(json.dumps(mcp.current_context))
            changed["capture"]["captureSessionId"] = "f" * 32
            changed["capture"]["captureId"] = "4" * 64
            mcp.current_context = changed
            await engine._synchronise_replay_project(changed)
            self.assertEqual(mcp.active_prepare_calls, 2)

    async def test_active_reverse_reuses_exact_workspace_without_overwriting_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "active-project"
            mcp = _ActiveProjectFakeMCP(root)
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=str(Path(temporary).resolve()),
                    organize_source=False,
                    watch_saved_shaders=False,
                ),
            )
            await engine.initialise()

            first = await engine.chat("反编译当前片元着色器")
            workspace = mcp._active_workspace()
            source = workspace / "source" / "shader.hlsl"
            edited = "float4 main() : SV_Target { return 0.25; }\n"
            source.write_text(edited, encoding="utf-8")
            second = await engine.chat("恢复当前像素着色器")

            self.assertIn(str(workspace), first)
            self.assertIn("reused without overwriting source", second)
            self.assertEqual(source.read_text(encoding="utf-8"), edited)
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("prepare_shader_workspace"), 1)
            self.assertEqual(names.count("get_active_shader_workspace_path"), 2)
            self.assertEqual(names.count("register_active_project_workspace"), 2)
            self.assertNotIn("register_replay_project_workspace", names)

    async def test_active_project_seed_uses_active_wrapper_and_fresh_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _ActiveProjectFakeMCP(Path(temporary) / "active-project")
            mcp.project_matches = [self._project_shader_match()]
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()
            context = await engine.get_current_context()
            await engine._synchronise_replay_project(context)
            workspace = mcp._active_workspace()
            (workspace / "source").mkdir(parents=True, exist_ok=True)
            source = workspace / "source" / "shader.hlsl"
            source.write_text(
                "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
            )

            result = await engine._seed_workspace_from_project(
                workspace, _intent("reverse-edit-selected-shader")
            )

            self.assertIsNotNone(result)
            self.assertTrue(result["seededCompiledAndAbiValidated"])
            names = [event[1] for event in mcp.events]
            self.assertIn("seed_active_shader_workspace", names)
            self.assertNotIn("seed_shader_workspace_from_replay_project", names)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

    async def test_active_project_rejects_ai_provisional_physical_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _ActiveProjectFakeMCP(Path(temporary) / "active-project")
            match = self._project_shader_match()
            match["workspaceCandidates"][0]["status"] = "AIProvisional"
            mcp.project_matches = [match]
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()
            context = await engine.get_current_context()
            with self.assertRaisesRegex(Exception, "AIProvisional"):
                await engine._synchronise_replay_project(context)

    async def test_pending_active_project_defers_registration_but_starts_live_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _PendingActiveLiveEditFakeMCP(
                Path(temporary) / "active-project"
            )
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                    organize_source=False,
                ),
            )
            await engine.initialise()
            try:
                summary = await engine.chat("反编译当前像素着色器")
                names = [event[1] for event in mcp.events]
                self.assertNotIn("register_active_project_workspace", names)
                self.assertIn("start_shader_workspace_live_edit", names)
                self.assertIn("project knowledge registration deferred", summary)
                self.assertIn("live edit: " + mcp.LIVE_ID, summary)
                self.assertTrue(mcp._active_workspace().joinpath("ai", "analysis.md").is_file())
            finally:
                await engine.close()

    async def test_reverse_prefers_host_live_edit_and_reports_rollback_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _LiveEditFakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                    organize_source=False,
                ),
            )
            await engine.initialise()
            try:
                summary = await engine.chat("反编译当前像素着色器")
                names = [event[1] for event in mcp.events]
                self.assertIn("start_shader_workspace_live_edit", names)
                self.assertNotIn("start_shader_workspace_watch", names)
                self.assertIn("live edit: " + mcp.LIVE_ID, summary)
                self.assertIn(
                    "rollback: pending first compatible saved revision", summary
                )
            finally:
                await engine.close()

    async def test_live_edit_poll_tracks_exact_shared_resource_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            notices = []
            mcp = _LiveEditFakeMCP()
            mcp.live_poll_applies = True
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                    organize_source=False,
                    intent_poll_seconds=0.2,
                ),
                notify=notices.append,
            )
            await engine.initialise()
            try:
                await engine.chat("反编译当前片元着色器")
                for unused in range(30):
                    if engine._latest_active_shared_replacement() is not None:
                        break
                    await asyncio.sleep(0.05)
                active = engine._latest_active_shared_replacement()
                self.assertIsNotNone(active)
                self.assertEqual(active.original_resource_id, 902)
                self.assertEqual(active.replacement_id, "a" * 32)
                self.assertTrue(any("Rollback:" in item for item in notices))

                rolled_back = await engine.rollback_last_workspace()
                self.assertIn("captured original shader is active", rolled_back)
                rollback_calls = [
                    event
                    for event in mcp.events
                    if event[1] == "rollback_shader_workspace_shared_resource"
                ]
                self.assertEqual(rollback_calls[-1][2]["replacement_id"], "a" * 32)
            finally:
                await engine.close()

    async def test_project_mode_imports_once_per_capture_session_without_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    project_root=temporary,
                    project="game",
                    project_game_id="game-id",
                    project_build_id="build-id",
                ),
            )
            await engine.initialise()
            context = await engine.get_current_context()
            first = await engine._synchronise_replay_project(context)
            second = await engine._synchronise_replay_project(context)
            self.assertEqual(first, second)
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("export_capture_symbol_catalog"), 1)
            self.assertEqual(names.count("import_replay_project_capture"), 1)
            self.assertEqual(names.count("get_replay_project_reuse_plan"), 1)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

            create = next(
                event for event in mcp.events if event[1] == "create_replay_project"
            )
            self.assertEqual(create[2]["game_id"], "game-id")
            self.assertEqual(create[2]["build_id"], "build-id")
            self.assertEqual(create[2]["api_family"], "D3D11")

            changed = json.loads(json.dumps(mcp.current_context))
            changed["capture"]["captureSessionId"] = "f" * 32
            changed["capture"]["captureId"] = "4" * 64
            mcp.current_context = changed
            await engine._synchronise_replay_project(changed)
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("export_capture_symbol_catalog"), 2)
            self.assertEqual(names.count("import_replay_project_capture"), 2)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

            conflicting = json.loads(json.dumps(changed))
            conflicting["capture"]["captureSessionId"] = "8" * 32
            conflicting["capture"]["captureId"] = "7" * 64
            conflicting["capture"]["api"] = "Vulkan"
            mcp.current_context = conflicting
            with self.assertRaisesRegex(Exception, "API family differs"):
                await engine._synchronise_replay_project(conflicting)
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("export_capture_symbol_catalog"), 2)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

    async def test_reverse_workflow_registers_hlsl_and_analysis_in_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=temporary,
                    project_root=temporary,
                    project="game",
                    project_game_id="game-id",
                    project_build_id="build-id",
                    organize_source=False,
                    watch_saved_shaders=False,
                ),
            )
            await engine.initialise()

            summary = await engine.chat("反编译当前片元着色器")

            self.assertIn("project knowledge: " + "6" * 32, summary)
            registration = next(
                event
                for event in mcp.events
                if event[1] == "register_replay_project_workspace"
            )
            self.assertEqual(registration[2]["project_path"], str(Path(temporary) / "game"))
            self.assertTrue(
                Path(registration[2]["workspace_path"], "ai", "analysis.md").is_file()
            )
            self.assertFalse(
                any(event[1].startswith("apply_shader") for event in mcp.events)
            )

    async def test_unique_project_source_seed_is_freshly_compiled_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            mcp.project_matches = [self._project_shader_match()]
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    project_root=temporary,
                    project="game",
                    project_game_id="game-id",
                    project_build_id="build-id",
                ),
            )
            await engine.initialise()
            context = await engine.get_current_context()
            await engine._synchronise_replay_project(context)
            workspace = Path(temporary) / "workspace"
            (workspace / "source").mkdir(parents=True)
            source = workspace / "source" / "shader.hlsl"
            source.write_text(
                "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
            )
            result = await engine._seed_workspace_from_project(
                workspace, _intent("reverse-edit-selected-shader")
            )
            self.assertIsNotNone(result)
            self.assertTrue(result["seededCompiledAndAbiValidated"])
            self.assertFalse(result["replayApplied"])
            self.assertEqual(source.read_text(encoding="utf-8"), mcp.project_source)
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("compile_shader_workspace"), 2)
            self.assertEqual(names.count("validate_shader_workspace_candidate"), 2)
            self.assertIn("seed_shader_workspace_from_replay_project", names)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

    async def test_compile_abi_validation_is_bound_to_exact_revision_and_raw_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            source_root = workspace / "source"
            source_root.mkdir(parents=True)
            (source_root / "shader.hlsl").write_text(
                "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
            )
            mcp = _FakeMCP()
            mcp.validation_raw_override = "f" * 64
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()

            with self.assertRaisesRegex(
                Exception, "ABI validation binding differs from the compiled candidate"
            ):
                await engine._compile_and_validate_workspace(
                    workspace, "TOCTOU regression", SESSION
                )

            compile_result = mcp.compiled_candidates[str(workspace.resolve())]
            validation_arguments = next(
                event[2]
                for event in mcp.events
                if event[1] == "validate_shader_workspace_candidate"
            )
            self.assertEqual(
                validation_arguments["expected_revision"],
                compile_result["revision"],
            )
            self.assertEqual(
                validation_arguments["expected_candidate_raw_sha256"],
                compile_result["rawSha256"],
            )
            self.assertFalse(
                any(event[1].startswith("apply_shader") for event in mcp.events)
            )

    async def test_compile_abi_validation_rejects_concurrent_source_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            source_root = workspace / "source"
            source_root.mkdir(parents=True)
            source = source_root / "shader.hlsl"
            source.write_text(
                "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
            )
            mcp = _FakeMCP()
            mcp.mutate_source_after_validation = (
                "float4 main() : SV_Target { return 0; }\n"
            )
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()

            with self.assertRaisesRegex(
                Exception, "workspace changed after compile/ABI validation"
            ):
                await engine._compile_and_validate_workspace(
                    workspace, "TOCTOU regression", SESSION
                )

            operations = [event[1] for event in mcp.events]
            self.assertLess(
                operations.index("validate_shader_workspace_candidate"),
                operations.index("get_shader_workspace_status"),
            )
            self.assertNotEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                mcp.compiled_candidates[str(workspace.resolve())]["sourceSha256"],
            )
            self.assertFalse(
                any(name.startswith("apply_shader") for name in operations)
            )

    async def test_compile_abi_validation_rejects_concurrent_latest_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            source_root = workspace / "source"
            source_root.mkdir(parents=True)
            (source_root / "shader.hlsl").write_text(
                "float4 main() : SV_Target { return 1; }\n", encoding="utf-8"
            )
            mcp = _FakeMCP()
            mcp.advance_candidate_after_validation = True
            engine = CompanionEngine(mcp, _FakeResponses([]))
            await engine.initialise()

            with self.assertRaisesRegex(
                Exception, "workspace changed after compile/ABI validation"
            ):
                await engine._compile_and_validate_workspace(
                    workspace, "TOCTOU regression", SESSION
                )

            operations = [event[1] for event in mcp.events]
            self.assertIn("get_shader_workspace_status", operations)
            self.assertFalse(
                any(name.startswith("apply_shader") for name in operations)
            )

    async def test_project_analysis_is_hash_verified_before_prompt_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "existing"
            analysis_path = project / "knowledge" / "workspaces" / "1" / "analysis.md"
            analysis_path.parent.mkdir(parents=True)
            content = "Prior explanatory analysis only.\n"
            analysis_path.write_text(content, encoding="utf-8")
            stored = analysis_path.read_bytes()
            match = self._project_shader_match()
            match["workspaceCandidates"][0]["analysisCandidate"] = {
                "path": str(analysis_path),
                "sha256": hashlib.sha256(stored).hexdigest(),
                "explanatoryOnly": True,
                "physicalEvidence": False,
                "applyAuthorization": False,
            }
            mcp = _FakeMCP()
            mcp.project_matches = [match]
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(project_root=temporary, project="existing"),
            )
            await engine.initialise()
            context = await engine.get_current_context()
            await engine._synchronise_replay_project(context)
            prompt = engine._project_prompt_data(context)
            verified = prompt["currentBoundShaderMatches"][0][
                "workspaceCandidates"
            ][0]["verifiedPriorAnalysis"]
            self.assertTrue(verified["hashVerified"])
            self.assertEqual(verified["preview"], stored.decode("utf-8"))
            self.assertFalse(verified["physicalEvidence"])
            self.assertFalse(verified["applyAuthorization"])

            analysis_path.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "hash verification failed"):
                engine._project_prompt_data(context)

    async def test_failed_project_seed_compile_restores_fresh_decompiler_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            mcp.project_matches = [self._project_shader_match()]
            mcp.fail_compile_attempt = 2
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    project_root=temporary,
                    project="game",
                    project_game_id="game-id",
                    project_build_id="build-id",
                ),
            )
            await engine.initialise()
            context = await engine.get_current_context()
            await engine._synchronise_replay_project(context)
            workspace = Path(temporary) / "workspace"
            (workspace / "source").mkdir(parents=True)
            source = workspace / "source" / "shader.hlsl"
            original = "float4 main() : SV_Target { return 1; }\n"
            source.write_text(original, encoding="utf-8")
            result = await engine._seed_workspace_from_project(
                workspace, _intent("reverse-edit-selected-shader")
            )
            self.assertIsNone(result)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertTrue((workspace / "project-source-seed-failure.json").is_file())
            names = [event[1] for event in mcp.events]
            self.assertEqual(names.count("compile_shader_workspace"), 3)
            self.assertFalse(any(name.startswith("apply_shader") for name in names))

    async def test_intent_requires_exact_allowed_operations(self):
        engine = CompanionEngine(_FakeMCP(), _FakeResponses([]))
        await engine.initialise()
        missing = _intent("reverse-edit-selected-shader")
        missing["recommendedWorkflow"]["allowedOperations"].remove(
            "decompile-shader"
        )
        with self.assertRaisesRegex(Exception, "allowedOperations"):
            await engine.process_intent(missing)
        unknown = _intent("explain-current-event")
        unknown["recommendedWorkflow"]["allowedOperations"].append(
            "apply-anything"
        )
        with self.assertRaisesRegex(Exception, "allowedOperations"):
            await engine.process_intent(unknown)

    async def test_chat_gets_programmatic_context_before_model_and_loops_tools(self):
        events = []
        mcp = _FakeMCP(events)
        responses = _FakeResponses(
            [
                _FakeResponse(
                    [
                        {"type": "reasoning", "id": "reasoning-1", "summary": []},
                        _call("get_draw_call_details", {"event_id": 142}),
                    ]
                ),
                _FakeResponse([{"type": "message", "content": []}], "This is a draw."),
            ],
            events,
        )
        engine = CompanionEngine(mcp, responses)
        await engine.initialise()
        answer = await engine.chat("这个事件做什么？")
        self.assertEqual(answer, "This is a draw.")
        self.assertEqual(events[0][0:2], ("mcp", "get_current_renderdoc_context"))
        self.assertEqual(events[1][0], "model")
        # A fresh capture-session check also precedes the requested capture tool.
        requested = [event[1] for event in events if event[0] == "mcp"]
        self.assertEqual(
            requested[:3],
            [
                "get_current_renderdoc_context",
                "get_current_renderdoc_context",
                "get_draw_call_details",
            ],
        )
        second_input = responses.requests[1]["input"]
        outputs = [item for item in second_input if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.assertEqual(outputs[-1]["call_id"], "call-1")
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("type") == "reasoning"
                and item.get("id") == "reasoning-1"
                for item in second_input
            )
        )
        exposed = {tool["name"] for tool in responses.requests[0]["tools"]}
        self.assertNotIn("apply_shader_workspace_candidate_family", exposed)

    async def test_explicit_apply_is_still_blocked_without_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = str(Path(temporary) / "shader")
            mcp = _FakeMCP()
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [
                            _call(
                                "apply_shader_workspace_candidate_family",
                                {"workspace_path": workspace, "event_ids": None, "scope": "all-exact-uses"},
                            )
                        ]
                    ),
                    _FakeResponse([], "Apply was rejected."),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            await engine.chat(
                "apply",
                {"apply_shader_workspace_candidate_family"},
            )
            self.assertFalse(
                any(call[1] == "apply_shader_workspace_candidate_family" for call in mcp.events)
            )
            tool_result = responses.requests[1]["input"][-1]["output"]
            self.assertIn("same-turn", tool_result)

    async def test_validate_then_explicit_family_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            workspace = str(Path(temporary) / "shader")
            responses = _FakeResponses(
                [
                    _FakeResponse([_call("validate_shader_workspace_candidate", {"workspace_path": workspace}, "v")]),
                    _FakeResponse([_call("apply_shader_workspace_candidate_family", {"workspace_path": workspace, "event_ids": None, "scope": "all-exact-uses"}, "a")]),
                    _FakeResponse([], "Applied after validation."),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            answer = await engine.chat(
                "validate and apply",
                {
                    "validate_shader_workspace_candidate",
                    "apply_shader_workspace_candidate_family",
                },
            )
            self.assertEqual(answer, "Applied after validation.")
            self.assertTrue(any(call[1] == "apply_shader_workspace_candidate_family" for call in mcp.events))

    async def test_large_tool_output_is_hash_marked_and_bounded(self):
        mcp = _FakeMCP()
        mcp.large_tool_output = "x" * 400_000
        responses = _FakeResponses(
            [
                _FakeResponse([_call("get_draw_call_details", {"event_id": 142})]),
                _FakeResponse([], "bounded"),
            ]
        )
        engine = CompanionEngine(mcp, responses)
        await engine.initialise()
        await engine.chat("details")
        tool_output = responses.requests[1]["input"][-1]["output"]
        marker = json.loads(tool_output)
        self.assertTrue(marker["truncated"])
        self.assertEqual(marker["originalCharacters"], 400_000)
        self.assertEqual(len(marker["sha256"]), 64)
        self.assertLessEqual(len(tool_output), 160_000)

    async def test_reverse_intent_prepares_review_without_applying(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            responses = _FakeResponses([_FakeResponse([], "AI review")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                    watch_saved_shaders=False,
                ),
            )
            await engine.initialise()
            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )
            self.assertIn("Shader workspace prepared", summary)
            operations = [call[1] for call in mcp.events]
            self.assertIn("prepare_shader_workspace", operations)
            self.assertNotIn("apply_shader_workspace_candidate_family", operations)
            analyses = list(Path(temporary).glob("*/ai/analysis.md"))
            self.assertEqual(len(analyses), 1)
            self.assertEqual(analyses[0].read_text(encoding="utf-8"), "AI review\n")

    async def test_short_reverse_chat_request_runs_full_fragment_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            responses = _FakeResponses([_FakeResponse([], "AI review")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=False,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            summary = await engine.chat("反编译当前像素着色器")

            self.assertIn("Shader workspace prepared", summary)
            operations = [event[1] for event in mcp.events if event[0] == "mcp"]
            self.assertIn("prepare_shader_workspace", operations)
            self.assertIn("start_shader_workspace_watch", operations)
            prepare = next(
                event for event in mcp.events if event[1] == "prepare_shader_workspace"
            )
            self.assertEqual(prepare[2]["event_id"], 142)
            self.assertEqual(prepare[2]["stage"], "fragment")
            self.assertFalse(
                any(name == "apply_shader_workspace_shared_resource" for name in operations)
            )

    def test_compact_stage_aliases_require_token_boundaries(self):
        self.assertTrue(
            CompanionEngine._message_mentions_stage_alias("decompile current PS shader", "ps")
        )
        self.assertFalse(
            CompanionEngine._message_mentions_stage_alias("restore maps shader", "ps")
        )
        self.assertTrue(
            CompanionEngine._message_mentions_stage_alias("反编译当前像素着色器", "像素")
        )

    async def test_reverse_intent_uses_server_owned_automatic_route_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            await engine.process_intent(_intent("reverse-edit-selected-shader"))
            prepare = next(
                event for event in mcp.events if event[1] == "prepare_shader_workspace"
            )
            self.assertEqual(prepare[2]["decompiler_tool_id"], "")
            self.assertEqual(prepare[2]["compiler_tool_id"], "")
            self.assertEqual(prepare[2]["editable_format"], "")
            self.assertFalse(
                any(event[1] == "plan_shader_toolchain_route" for event in mcp.events)
            )

    async def test_ai_hlsl_organizer_rejects_instruction_assembly_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "assembly-workspace"
            (workspace / "source").mkdir(parents=True)
            source = workspace / "source" / "shader.dxbc.asm"
            source.write_text("ps_5_0\ndcl_output o0.xyzw\nret\n", encoding="utf-8")
            engine = CompanionEngine(
                _FakeMCP(),
                _FakeResponses([]),
                CompanionConfig(workspace_root=temporary, organize_source=True),
            )
            with self.assertRaisesRegex(CompanionError, "HLSL-only"):
                await engine._organize_workspace_source(
                    workspace,
                    source,
                    source.read_text(encoding="utf-8"),
                    _intent("reverse-edit-selected-shader"),
                )
            self.assertFalse((workspace / "ai").exists())

    async def test_reverse_intent_falls_back_to_explanatory_lane_without_compile_watch_or_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "server-owned-shader.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _FakeMCP()
            mcp.prepare_representation_lane = "explanatory-ir"
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses([_FakeResponse([], "Instruction-flow review")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            self.assertIn(str(artifact), summary)
            self.assertIn("Compile, save-watch and replay apply are disabled", summary)
            operations = [call[1] for call in mcp.events]
            self.assertIn("get_current_shader_representation", operations)
            for forbidden in (
                "compile_shader_workspace",
                "validate_shader_workspace_candidate",
                "start_shader_workspace_watch",
                "apply_shader_workspace_shared_resource",
                "seed_shader_workspace_from_replay_project",
            ):
                self.assertNotIn(forbidden, operations)
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertFalse((workspace / "source" / "shader.hlsl").exists())
            self.assertEqual(
                (workspace / "ai" / "explanation-analysis.md").read_text(
                    encoding="utf-8"
                ),
                "Instruction-flow review\n",
            )
            self.assertIsNone(engine._last_prepared_workspace)
            prompt = responses.requests[0]["input"][0]["content"]
            self.assertIn('"representationLane": "explanatory-ir"', prompt)
            self.assertIn(artifact.name, prompt)
            with self.assertRaisesRegex(Exception, "No shader workspace"):
                await engine.apply_last_workspace()

    async def test_reverse_prepare_failure_falls_back_to_binary_only_current_event_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "server-owned-shader.binary.txt"
            artifact.write_text("rawSha256: %s\n" % ("2" * 64), encoding="utf-8")
            mcp = _FakeMCP()
            mcp.prepare_error = "no executable route for captured container"
            mcp.current_representation_lane = "binary-only"
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses([_FakeResponse([], "Only binary identity is available")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("binary-only lane", summary)
            self.assertIn("no executable route", summary)
            operations = [call[1] for call in mcp.events]
            self.assertLess(
                operations.index("prepare_shader_workspace"),
                operations.index("get_current_shader_representation"),
            )
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertNotIn("apply_shader_workspace_shared_resource", operations)
            self.assertIsNone(engine._last_prepared_workspace)
            prompt = responses.requests[0]["input"][0]["content"]
            self.assertIn('"representationLane": "binary-only"', prompt)
            self.assertIn("no executable route for captured container", prompt)

    async def test_ai_selects_only_registered_alternate_into_fresh_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _AlternateRecoveryFakeMCP()
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 1,
                                "compileCandidateIndex": 0,
                                "rationale": (
                                    "The fixed diagnostic permits the listed alternate."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "organizedSource": (
                                    "// Review-only organization of fixed output.\n"
                                    "float4 main() : SV_Target {\n"
                                    "  return float4(0.75, 0.5, 0.25, 1);\n"
                                    "}\n"
                                ),
                                "analysis": "The alternate fixed route produced HLSL.",
                                "flowchartMermaid": (
                                    "flowchart LR\n  A[Entry] --> B[Colour] "
                                    "--> C[Output]"
                                ),
                            }
                        ),
                    ),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("registered recovery route selected", summary)
            recovery_call = next(
                event
                for event in mcp.events
                if event[1] == "try_shader_workspace_recovery_candidate"
            )
            arguments = recovery_call[2]
            source_workspace = Path(arguments["source_workspace_path"]).resolve()
            target_workspace = Path(arguments["workspace_path"]).resolve()
            self.assertNotEqual(source_workspace, target_workspace)
            self.assertEqual(source_workspace.parent, target_workspace.parent)
            self.assertTrue(source_workspace.is_dir())
            self.assertTrue(target_workspace.is_dir())
            self.assertFalse((source_workspace / "source" / "shader.hlsl").exists())
            self.assertTrue((target_workspace / "source" / "shader.hlsl").is_file())
            self.assertEqual(arguments["expected_source_workspace_id"], "a" * 32)
            self.assertEqual(arguments["expected_baseline_raw_sha256"], "2" * 64)
            self.assertEqual(
                arguments["expected_capture_id"],
                mcp.current_context["capture"]["captureId"],
            )
            self.assertEqual(arguments["decompile_candidate_index"], 1)
            self.assertEqual(arguments["compile_candidate_index"], 0)
            operations = [event[1] for event in mcp.events]
            self.assertLess(
                operations.index("prepare_shader_workspace"),
                operations.index("try_shader_workspace_recovery_candidate"),
            )
            self.assertLess(
                operations.index("try_shader_workspace_recovery_candidate"),
                operations.index("compile_shader_workspace"),
            )
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            selector_request = responses.requests[0]
            self.assertNotIn("tools", selector_request)
            selector_schema = selector_request["text"]["format"]["schema"]
            self.assertEqual(
                selector_schema["properties"]["action"]["enum"],
                ["retry-registered-decompiler", "inspect-native-ir"],
            )
            self.assertEqual(
                set(selector_schema["properties"]),
                {
                    "action",
                    "decompileCandidateIndex",
                    "compileCandidateIndex",
                    "rationale",
                },
            )
            selector_prompt = selector_request["input"][0]["content"]
            self.assertIn("tool-rejected-input", selector_prompt)
            self.assertIn("try-registered-alternate-route", selector_prompt)
            self.assertIn('"toolRegistrySha256": "' + "8" * 64, selector_prompt)
            self.assertIn('"newWorkspaceIsProgramGenerated": true', selector_prompt)
            self.assertTrue(
                (source_workspace / "ai" / "recovery-route-attempt-01.json").is_file()
            )

    async def test_failed_alternate_is_preserved_before_second_fresh_route_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _AlternateRecoveryFakeMCP()
            mcp.additional_alternate_route_ids = [
                "fixed-second-alternate-dxil-hlsl"
            ]
            mcp.failed_recovery_candidate_indices = {1}
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 1,
                                "compileCandidateIndex": 0,
                                "rationale": (
                                    "Try the first fixed alternate permitted by the "
                                    "program classifier."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 2,
                                "compileCandidateIndex": 0,
                                "rationale": (
                                    "The first alternate retained an eligible fixed "
                                    "failure category; try the remaining listed route."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse([], "The second fixed route produced editable HLSL."),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("registered recovery route selected", summary)
            recovery_calls = [
                event
                for event in mcp.events
                if event[1] == "try_shader_workspace_recovery_candidate"
            ]
            self.assertEqual(len(recovery_calls), 2)
            source_workspace = Path(
                recovery_calls[0][2]["source_workspace_path"]
            ).resolve()
            first_target = Path(
                recovery_calls[0][2]["workspace_path"]
            ).resolve()
            second_target = Path(
                recovery_calls[1][2]["workspace_path"]
            ).resolve()
            self.assertEqual(
                [
                    call[2]["decompile_candidate_index"]
                    for call in recovery_calls
                ],
                [1, 2],
            )
            self.assertEqual(len({source_workspace, first_target, second_target}), 3)
            self.assertEqual(source_workspace.parent, first_target.parent)
            self.assertEqual(source_workspace.parent, second_target.parent)
            self.assertFalse((source_workspace / "source" / "shader.hlsl").exists())
            self.assertEqual(
                (
                    source_workspace / "explanation" / "native-ir.txt"
                ).read_text(encoding="utf-8"),
                "mov r0, r0\n",
            )
            self.assertFalse((first_target / "source" / "shader.hlsl").exists())
            first_failure = first_target / "explanation" / "native-ir.txt"
            self.assertTrue(first_failure.is_file())
            first_failure_hash = hashlib.sha256(first_failure.read_bytes()).hexdigest()
            self.assertTrue((second_target / "source" / "shader.hlsl").is_file())
            self.assertEqual(
                hashlib.sha256(first_failure.read_bytes()).hexdigest(),
                first_failure_hash,
            )
            for ordinal in (1, 2):
                self.assertTrue(
                    (
                        source_workspace
                        / "ai"
                        / ("recovery-route-attempt-%02d.json" % ordinal)
                    ).is_file()
                )
                self.assertTrue(
                    (
                        source_workspace
                        / "ai"
                        / ("recovery-route-attempt-%02d-response.txt" % ordinal)
                    ).is_file()
                )
            first_audit = json.loads(
                (
                    source_workspace / "ai" / "recovery-route-attempt-01.json"
                ).read_text(encoding="utf-8")
            )
            second_audit = json.loads(
                (
                    source_workspace / "ai" / "recovery-route-attempt-02.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(first_audit["representationLane"], "explanatory-ir")
            self.assertEqual(second_audit["representationLane"], "executable-editable")
            self.assertFalse(
                any(
                    name.startswith("apply_shader_workspace")
                    for _, name, _ in mcp.events
                )
            )

    async def test_failed_compile_probe_retries_same_decompiler_with_another_compiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _AlternateRecoveryFakeMCP()
            mcp.additional_compile_route_ids = [
                "fixed-second-hlsl-dxil",
                "fixed-third-hlsl-dxil",
            ]
            mcp.failed_recovery_candidate_pairs = {(0, 1)}
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 0,
                                "compileCandidateIndex": 1,
                                "rationale": (
                                    "Retry the primary decompiler with the first "
                                    "listed alternate compiler pair."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 0,
                                "compileCandidateIndex": 2,
                                "rationale": (
                                    "The first compiler pair failed; keep the same "
                                    "decompiler and try the remaining listed compiler."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse(
                        [], "The fixed route pair produced editable HLSL."
                    ),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("registered recovery route selected", summary)
            recovery_calls = [
                event
                for event in mcp.events
                if event[1] == "try_shader_workspace_recovery_candidate"
            ]
            self.assertEqual(
                [
                    (
                        call[2]["decompile_candidate_index"],
                        call[2]["compile_candidate_index"],
                    )
                    for call in recovery_calls
                ],
                [(0, 1), (0, 2)],
            )
            selector_evidence = [
                json.loads(request["input"][0]["content"].split("\n", 1)[1])
                for request in responses.requests[:2]
            ]
            first_pairs = {
                (
                    pair["decompileCandidateIndex"],
                    pair["compileCandidateIndex"],
                )
                for pair in selector_evidence[0]["availableRegisteredCandidates"][
                    "pairs"
                ]
            }
            second_pairs = {
                (
                    pair["decompileCandidateIndex"],
                    pair["compileCandidateIndex"],
                )
                for pair in selector_evidence[1]["availableRegisteredCandidates"][
                    "pairs"
                ]
            }
            self.assertIn((0, 1), first_pairs)
            self.assertNotIn((0, 1), second_pairs)
            self.assertIn((0, 2), second_pairs)
            self.assertFalse(
                any(
                    name.startswith("apply_shader_workspace")
                    for _, name, _ in mcp.events
                )
            )

    async def test_round_trip_failure_cannot_skip_routes_to_construct_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _AlternateRecoveryFakeMCP()
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "inspect-native-ir",
                                "decompileCandidateIndex": None,
                                "compileCandidateIndex": None,
                                "rationale": (
                                    "Retain the fixed explanatory evidence for review."
                                ),
                            }
                        ),
                    ),
                    _FakeResponse([], "Explanatory review after route decision"),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            self.assertIn("registered recovery stayed explanatory", summary)
            operations = [event[1] for event in mcp.events]
            self.assertNotIn("try_shader_workspace_recovery_candidate", operations)
            self.assertNotIn("promote_shader_workspace_explanation", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            first_schema = responses.requests[0]["text"]["format"]["schema"]
            self.assertNotIn(
                "construct-provisional-source",
                first_schema["properties"]["action"]["enum"],
            )
            self.assertEqual(len(responses.requests), 2)

    async def test_ai_recovery_selector_cannot_supply_download_instructions(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _AlternateRecoveryFakeMCP()
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 1,
                                "compileCandidateIndex": 0,
                                "rationale": "Download a provider before retrying.",
                            }
                        ),
                    ),
                    _FakeResponse([], "Explanation after unsafe selection rejection"),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary, watch_saved_shaders=True),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            operations = [event[1] for event in mcp.events]
            self.assertNotIn("try_shader_workspace_recovery_candidate", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            source_workspace = next(
                path
                for path in Path(temporary).iterdir()
                if path.name.startswith("e142-")
            )
            self.assertTrue(
                (
                    source_workspace
                    / "ai"
                    / "recovery-route-selection-01-response.txt"
                ).is_file()
            )

    async def test_program_failure_actions_can_block_alternate_route_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _AlternateRecoveryFakeMCP()
            mcp.prepare_failure_action_message = (
                "preserve-evidence, repair-process-owned-tool-registration"
            )
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses(
                [_FakeResponse([], "Explanation after fixed classifier refusal")]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary, watch_saved_shaders=True),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            self.assertIn("fixed failure classifier", summary)
            operations = [event[1] for event in mcp.events]
            self.assertNotIn("try_shader_workspace_recovery_candidate", operations)
            self.assertNotIn("promote_shader_workspace_explanation", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertEqual(len(responses.requests), 1)

    async def test_registered_recovery_response_wrong_source_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _AlternateRecoveryFakeMCP()
            mcp.returned_attempt_source_workspace_id = "c" * 32
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "retry-registered-decompiler",
                                "decompileCandidateIndex": 1,
                                "compileCandidateIndex": 0,
                                "rationale": "Use only the listed fixed alternate.",
                            }
                        ),
                    ),
                    _FakeResponse([], "Explanation after provenance rejection"),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary, watch_saved_shaders=True),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            self.assertIn("mismatched provenance", summary)
            operations = [event[1] for event in mcp.events]
            self.assertIn("try_shader_workspace_recovery_candidate", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            recovery_call = next(
                event
                for event in mcp.events
                if event[1] == "try_shader_workspace_recovery_candidate"
            )
            source_workspace = Path(
                recovery_call[2]["source_workspace_path"]
            ).resolve()
            target_workspace = Path(recovery_call[2]["workspace_path"]).resolve()
            self.assertNotEqual(source_workspace, target_workspace)
            self.assertTrue(target_workspace.is_dir())
            self.assertFalse((source_workspace / "source" / "shader.hlsl").exists())
            self.assertTrue(
                (
                    source_workspace
                    / "ai"
                    / "recovery-route-attempt-01-response.txt"
                ).is_file()
            )

    async def test_provisional_reconstruction_uses_strict_action_and_hash_bound_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n"
            mcp = _RecoveryFakeMCP()
            # Provisional/algorithm construction is not organize-only: changed
            # physical code is allowed, but compilation and fresh Replay ABI
            # validation below remain mandatory.
            mcp.compile_code_sha256s = ["7" * 64]
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "construct-provisional-source",
                                "provisionalSource": source,
                                "assumptions": [
                                    "The native instruction flow is sufficient for this review candidate."
                                ],
                                "rationale": (
                                    "Constructed a minimal self-contained review candidate; "
                                    "semantic equivalence remains unverified."
                                ),
                            }
                        ),
                    )
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                    watch_saved_shaders=False,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("provisional AI source hash-promoted", summary)
            self.assertEqual(len(responses.requests), 1)
            request = responses.requests[0]
            schema = request["text"]["format"]["schema"]
            self.assertTrue(request["text"]["format"]["strict"])
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(
                schema["properties"]["action"]["enum"],
                ["construct-provisional-source", "stay-explanatory"],
            )
            self.assertNotIn("tools", request)
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertEqual(
                (workspace / "source" / "shader.hlsl").read_text(encoding="utf-8"),
                source,
            )
            promotion = next(
                event
                for event in mcp.events
                if event[1] == "promote_shader_workspace_explanation"
            )
            self.assertEqual(
                promotion[2]["expected_source_sha256"],
                hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                promotion[2]["expected_representation_sha256"],
                hashlib.sha256(
                    (workspace / "explanation" / "native-ir.txt").read_bytes()
                ).hexdigest(),
            )
            self.assertTrue(
                promotion[2]["acknowledge_semantic_equivalence_unverified"]
            )
            provenance = json.loads(
                (workspace / "ai" / "reconstruction-provenance.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(provenance["compiledAndFreshAbiValidated"])
            self.assertFalse(provenance["semanticEquivalenceVerified"])
            self.assertFalse(provenance["replayApplied"])
            compiled_attempt = json.loads(
                (
                    workspace
                    / "ai"
                    / "reconstruction-attempt-01-compile.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(compiled_attempt["result"]["codeSha256"], "7" * 64)
            self.assertEqual(
                len(
                    [
                        event
                        for event in mcp.events
                        if event[1] == "validate_shader_workspace_candidate"
                    ]
                ),
                1,
            )
            self.assertFalse(
                any(
                    event[1].startswith("apply_shader_workspace")
                    for event in mcp.events
                )
            )

    async def test_provisional_reconstruction_allows_bounded_diagnostic_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n"
            repaired = "float4 main() : SV_Target { return float4(0, 1, 0, 1); }\n"
            mcp = _RecoveryFakeMCP()
            mcp.fail_compile_attempt = 1
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "construct-provisional-source",
                                "provisionalSource": first,
                                "assumptions": ["Initial evidence-bound candidate."],
                                "rationale": "Submit the first provisional candidate.",
                            }
                        ),
                    ),
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "construct-provisional-source",
                                "provisionalSource": repaired,
                                "assumptions": [
                                    "The program diagnostic requires a source-only correction."
                                ],
                                "rationale": (
                                    "Revised only the provisional source after the "
                                    "reported compile failure."
                                ),
                            }
                        ),
                    ),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("1 diagnostic repair(s)", summary)
            self.assertEqual(mcp.compile_attempts, 2)
            self.assertEqual(len(responses.requests), 2)
            repair_prompt = responses.requests[1]["input"][0]["content"]
            self.assertIn("intentional compile failure", repair_prompt)
            self.assertIn(first.strip(), repair_prompt)
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertEqual(
                (workspace / "source" / "shader.hlsl").read_text(encoding="utf-8"),
                repaired,
            )
            self.assertEqual(
                json.loads(
                    (workspace / "ai" / "reconstruction-provenance.json").read_text(
                        encoding="utf-8"
                    )
                )["diagnosticRepairs"],
                1,
            )

    async def test_invalid_provisional_action_is_audited_and_falls_back_without_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _RecoveryFakeMCP()
            mcp.current_representation_path = str(artifact)
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "apply-candidate",
                                "provisionalSource": "",
                                "assumptions": [],
                                "rationale": "Unsafe action should be rejected.",
                            }
                        ),
                    ),
                    _FakeResponse([], "Explanatory review after strict rejection"),
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary, watch_saved_shaders=True),
            )
            await engine.initialise()

            summary = await engine.process_intent(
                _intent("reverse-edit-selected-shader")
            )

            self.assertIn("explanatory-ir lane", summary)
            self.assertIn("provisional recovery stayed explanatory", summary)
            operations = [event[1] for event in mcp.events]
            self.assertNotIn("promote_shader_workspace_explanation", operations)
            self.assertNotIn("compile_shader_workspace", operations)
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            failure = json.loads(
                (workspace / "ai" / "reconstruction-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(failure["replayApplied"])
            self.assertIn("fields are invalid", failure["failures"][-1]["error"])
            self.assertTrue((workspace / "ai" / "explanation-analysis.md").is_file())

    async def test_provisional_reconstruction_stops_after_two_diagnostic_repairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "current-event.ir.txt"
            artifact.write_text("mov r0, r0\n", encoding="utf-8")
            mcp = _RecoveryFakeMCP()
            mcp.current_representation_path = str(artifact)
            decisions = []
            for index in range(3):
                decisions.append(
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "construct-provisional-source",
                                "provisionalSource": (
                                    "float4 main() : SV_Target { return %d; }\n" % index
                                ),
                                "assumptions": ["Bounded attempt %d." % (index + 1)],
                                "rationale": "Produce one bounded provisional revision.",
                            }
                        ),
                    )
                )
            decisions.append(_FakeResponse([], "Explanation after bounded failure"))
            responses = _FakeResponses(decisions)
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(workspace_root=temporary, watch_saved_shaders=True),
            )
            await engine.initialise()

            with mock.patch.object(
                engine,
                "_compile_and_validate_workspace",
                side_effect=CompanionError("persistent compiler diagnostic"),
            ) as compile_gate:
                summary = await engine.process_intent(
                    _intent("reverse-edit-selected-shader")
                )

            self.assertIn("explanatory-ir lane", summary)
            self.assertEqual(compile_gate.await_count, 3)
            self.assertEqual(len(responses.requests), 4)
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            failure = json.loads(
                (workspace / "ai" / "reconstruction-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(failure["failures"]), 3)
            self.assertEqual(failure["maximumDiagnosticRepairs"], 2)
            operations = [event[1] for event in mcp.events]
            self.assertNotIn("start_shader_workspace_watch", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )

    async def test_asset_recovery_preview_enters_only_strict_untrusted_reconstruction_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            preview = "float4 main() : SV_Target { return 0.25; }\n"
            report = _asset_shader_recovery_report(preview)
            mcp = _FakeMCP()
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "action": "stay-explanatory",
                                "provisionalSource": "",
                                "assumptions": [
                                    "Asset source remains untrusted recovery evidence."
                                ],
                                "rationale": "Keep the candidate explanatory.",
                            }
                        ),
                    )
                ]
            )
            engine = CompanionEngine(mcp, responses)
            await engine.initialise()
            engine._active_project_enabled = True
            engine._project_capture_session = SESSION
            engine._active_project_status = {
                "workspaceId": "9" * 32,
                "name": "Asset Project",
                "state": "bound",
                "scopeConfigured": True,
                "scope": {"gameId": "game", "buildId": "build"},
                "projectRoot": r"D:\projects\asset-project",
                "assetRoot": r"D:\unpacked",
                "assetRootConfigured": True,
            }
            engine._active_project_prepare = {
                "reusePlan": {"blockedReason": ""},
                "symbolResolution": {
                    "attempted": True,
                    "blocked": False,
                    "reason": "",
                    "scanCacheHit": False,
                    "policy": {
                        "verifiedAliasesOnly": True,
                        "aiProvisionalAutoApply": False,
                    },
                    "assetShaderRecovery": report,
                },
            }
            recovery_contract = {
                "recoveryPlan": {"mode": "compile-only-recovery"},
                "routePlan": {"editableFormat": "hlsl", "compile": [{}]},
                "representation": {
                    "lane": "explanatory-ir",
                    "sha256": "4" * 64,
                },
                "representationText": "ret\n",
                "editableFormat": "hlsl",
            }
            audit_path = Path(temporary) / "asset-response.txt"

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("candidate paths must never be read"),
            ):
                decision = await engine._request_provisional_source(
                    intent=_intent("reverse-edit-selected-shader"),
                    recovery_contract=recovery_contract,
                    reverse_inspection={"disassembly": "ret"},
                    response_audit_path=audit_path,
                    attempt=1,
                    construction_allowed=True,
                )

            self.assertEqual(decision["action"], "stay-explanatory")
            request = responses.requests[0]
            schema = request["text"]["format"]["schema"]
            self.assertEqual(
                schema["properties"]["action"]["enum"],
                ["construct-provisional-source", "stay-explanatory"],
            )
            prompt = request["input"][0]["content"]
            self.assertIn('"untrustedAssetShaderRecovery"', prompt)
            self.assertIn(preview.strip(), prompt)
            self.assertIn('"allCandidatesUntrusted": true', prompt)
            self.assertIn('"workspaceSeedAuthorized": false', prompt)
            self.assertIn('"applyAuthorized": false', prompt)
            self.assertIn(
                '"independentCompileAndAbiValidationRequired": true', prompt
            )
            self.assertNotIn(r"D:\unpacked\Shaders\main.hlsl", prompt)
            self.assertEqual([event for event in mcp.events if "apply" in event[1]], [])

    def test_asset_recovery_policy_and_relative_paths_fail_closed(self):
        engine = CompanionEngine(_FakeMCP(), _FakeResponses([]))
        engine._active_project_enabled = True
        engine._project_capture_session = SESSION
        report = _asset_shader_recovery_report("float4 main():SV_Target{return 1;}\n")
        engine._active_project_prepare = {
            "symbolResolution": {"assetShaderRecovery": report}
        }
        intent = _intent("reverse-edit-selected-shader")

        report["policy"]["workspaceSeedAuthorized"] = True
        with self.assertRaisesRegex(CompanionError, "policy is unsafe"):
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )

        report["policy"]["workspaceSeedAuthorized"] = False
        report["boundShaders"][0]["topCandidates"][0]["relativePath"] = (
            r"D:\outside\shader.hlsl"
        )
        with self.assertRaisesRegex(CompanionError, "bounded-relative"):
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )

        engine._active_project_prepare = {
            "symbolResolution": {"assetShaderRecovery": None}
        }
        self.assertIsNone(
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )
        )

    def test_asset_recovery_requires_exact_entry_point_when_capture_provides_it(self):
        engine = CompanionEngine(_FakeMCP(), _FakeResponses([]))
        engine._active_project_enabled = True
        engine._project_capture_session = SESSION
        report = _asset_shader_recovery_report(
            "float4 main():SV_Target{return 1;}\n"
        )
        matching = report["boundShaders"][0]
        wrong_entry = json.loads(json.dumps(matching))
        wrong_entry["entryPoint"] = "alternateMain"
        report["boundShaders"] = [wrong_entry, matching]
        engine._active_project_prepare = {
            "symbolResolution": {"assetShaderRecovery": report}
        }
        intent = _intent("reverse-edit-selected-shader")

        evidence = engine._active_asset_recovery_evidence(
            intent["context"], intent["arguments"]
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["entryPoint"], "main")

        report["boundShaders"] = [wrong_entry]
        self.assertIsNone(
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )
        )

    def test_boolean_schema_fields_reject_integer_aliases(self):
        with self.assertRaisesRegex(CompanionError, "invalid reuse plan"):
            CompanionEngine._active_reuse_plan(
                {
                    "schema": "replaylab-future-frame-load-plan/v1",
                    "schemaVersion": 1,
                    "scopeConfigured": 1,
                    "blocked": False,
                },
                "c" * 64,
            )

        engine = CompanionEngine(_FakeMCP(), _FakeResponses([]))
        engine._active_project_enabled = True
        engine._project_capture_session = SESSION
        intent = _intent("reverse-edit-selected-shader")

        preview_report = _asset_shader_recovery_report(
            "float4 main():SV_Target{return 1;}\n"
        )
        preview_report["sourcePreviews"][0]["sourcePreviewTruncated"] = 0
        engine._active_project_prepare = {
            "symbolResolution": {"assetShaderRecovery": preview_report}
        }
        with self.assertRaisesRegex(CompanionError, "preview identity is invalid"):
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )

        candidate_report = _asset_shader_recovery_report()
        candidate_report["boundShaders"][0]["candidatesTruncated"] = 1
        engine._active_project_prepare = {
            "symbolResolution": {"assetShaderRecovery": candidate_report}
        }
        with self.assertRaisesRegex(CompanionError, "candidates are invalid"):
            engine._active_asset_recovery_evidence(
                intent["context"], intent["arguments"]
            )

    async def test_non_directx_prepare_leaves_editable_format_to_program(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            mcp.current_context["capture"]["api"] = "Vulkan"
            intent = _intent("reverse-edit-selected-shader")
            intent["context"]["capture"]["api"] = "Vulkan"
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()

            await engine.process_intent(intent)

            prepare = next(
                event for event in mcp.events if event[1] == "prepare_shader_workspace"
            )
            self.assertEqual(prepare[2]["editable_format"], "")

    async def test_vulkan_glsl_routed_source_compiles_reviews_and_watches_without_hlsl_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _RoutedSourceFakeMCP("glsl")
            mcp.current_context["capture"]["api"] = "Vulkan"
            intent = _intent("reverse-edit-selected-shader")
            intent["context"]["capture"]["api"] = "Vulkan"
            responses = _FakeResponses([_FakeResponse([], "GLSL review")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            summary = await engine.process_intent(intent)

            self.assertIn("routed source format: glsl", summary)
            self.assertIn("baseline compiled and fresh ABI-validated", summary)
            operations = [event[1] for event in mcp.events]
            self.assertIn("compile_shader_workspace", operations)
            self.assertIn("validate_shader_workspace_candidate", operations)
            self.assertIn("start_shader_workspace_watch", operations)
            self.assertNotIn("promote_shader_workspace_explanation", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertTrue((workspace / "source" / "shader.glsl").is_file())
            self.assertFalse((workspace / "source" / "shader.hlsl").exists())
            self.assertEqual(len(responses.requests), 1)
            prompt = responses.requests[0]["input"][0]["content"]
            self.assertIn('"sourceFormat": "glsl"', prompt)
            self.assertIn('"aiSourceRewriteEligible": false', prompt)

    async def test_assembly_routed_source_skips_hlsl_organizer_but_keeps_compile_watch(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _RoutedSourceFakeMCP("dxbc-asm")
            responses = _FakeResponses([_FakeResponse([], "Assembly review")])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    organize_source=True,
                    watch_saved_shaders=True,
                ),
            )
            await engine.initialise()

            with mock.patch.object(
                engine,
                "_register_workspace_in_project",
                new_callable=mock.AsyncMock,
            ) as register_workspace:
                summary = await engine.process_intent(
                    _intent("reverse-edit-selected-shader")
                )
            register_workspace.assert_not_awaited()

            self.assertIn("routed source format: dxbc-asm", summary)
            operations = [event[1] for event in mcp.events]
            self.assertIn("compile_shader_workspace", operations)
            self.assertIn("validate_shader_workspace_candidate", operations)
            self.assertIn("start_shader_workspace_watch", operations)
            self.assertNotIn("promote_shader_workspace_explanation", operations)
            self.assertFalse(
                any(name.startswith("apply_shader_workspace") for name in operations)
            )
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertTrue((workspace / "source" / "shader.dxbc.asm").is_file())
            self.assertFalse((workspace / "source" / "shader.hlsl").exists())
            self.assertEqual(len(responses.requests), 1)
            prompt = responses.requests[0]["input"][0]["content"]
            self.assertIn('"sourceFormat": "dxbc-asm"', prompt)
            self.assertIn('"aiSourceRewriteEligible": false', prompt)

    async def test_apply_last_is_explicit_and_companion_runs_fresh_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([_FakeResponse([], "AI review")]),
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                ),
            )
            await engine.initialise()
            await engine.process_intent(_intent("reverse-edit-selected-shader"))
            before = len(
                [
                    event
                    for event in mcp.events
                    if event[1] == "apply_shader_workspace_shared_resource"
                ]
            )
            answer = await engine.apply_last_workspace()
            after = len(
                [
                    event
                    for event in mcp.events
                    if event[1] == "apply_shader_workspace_shared_resource"
                ]
            )
            self.assertEqual(before, 0)
            self.assertEqual(after, 1)
            self.assertIn("shared target", answer)
            rollback = await engine.rollback_last_workspace()
            self.assertIn("original shader is active", rollback)
            rollback_calls = [
                event
                for event in mcp.events
                if event[1] == "rollback_shader_workspace_shared_resource"
            ]
            self.assertEqual(
                rollback_calls[-1][2]["replacement_id"],
                "a" * 32,
            )
            with self.assertRaisesRegex(Exception, "no companion-applied"):
                await engine.rollback_last_workspace()

    async def test_multiple_shared_resources_rollback_in_reverse_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "shader-a"
            second = Path(temporary) / "shader-b"
            first.mkdir()
            second.mkdir()
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(first), SESSION, "1" * 32, 101)
            )
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(second), SESSION, "2" * 32, 202)
            )

            first_answer = await engine.rollback_last_workspace()
            second_answer = await engine.rollback_last_workspace()
            rollback_ids = [
                event[2]["replacement_id"]
                for event in mcp.events
                if event[1] == "rollback_shader_workspace_shared_resource"
            ]
            self.assertEqual(rollback_ids, ["2" * 32, "1" * 32])
            self.assertIn("1 earlier replacement", first_answer)
            self.assertIn("0 earlier replacement", second_answer)

    async def test_shared_resource_supersede_and_failed_rollback_keep_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "shader"
            workspace.mkdir()
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(workspace), SESSION, "1" * 32, 101)
            )
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(workspace), SESSION, "2" * 32, 101)
            )
            self.assertEqual(len(engine._active_shared_replacement_order), 1)
            self.assertEqual(
                engine._latest_active_shared_replacement().replacement_id,
                "2" * 32,
            )

            mcp.fail_shared_rollback = True
            with self.assertRaisesRegex(Exception, "rollback failure"):
                await engine.rollback_last_workspace()
            self.assertEqual(
                engine._latest_active_shared_replacement().replacement_id,
                "2" * 32,
            )

            engine._synchronise_active_shared_replacements("b" * 32)
            self.assertIsNone(engine._latest_active_shared_replacement())

    async def test_graceful_close_rolls_back_all_active_shared_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "shader-a"
            second = Path(temporary) / "shader-b"
            first.mkdir()
            second.mkdir()
            mcp = _FakeMCP()
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(workspace_root=temporary),
            )
            await engine.initialise()
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(first), SESSION, "1" * 32, 101)
            )
            engine._remember_active_shared_replacement(
                _AppliedWorkspace(str(second), SESSION, "2" * 32, 202)
            )

            await engine.close()
            rollback_ids = [
                event[2]["replacement_id"]
                for event in mcp.events
                if event[1] == "rollback_shader_workspace_shared_resource"
            ]
            self.assertEqual(rollback_ids, ["2" * 32, "1" * 32])
            self.assertFalse(engine._active_shared_replacement_order)

    async def test_opt_in_source_organization_is_compile_abi_gated_and_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            events = []
            mcp = _FakeMCP(events)
            organized = (
                "// AI-organized review candidate\n"
                "float4 main() : SV_Target { return float4(1, 1, 1, 1); }\n"
            )
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "organizedSource": organized,
                                "analysis": "Renamed locals and retained the entry point.",
                                "flowchartMermaid": "flowchart LR\n  A[Inputs] --> B[Shade] --> C[Output]",
                            }
                        ),
                    )
                ],
                events,
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                    organize_source=True,
                ),
            )
            await engine.initialise()
            await engine.process_intent(_intent("reverse-edit-selected-shader"))
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            original = (workspace / "ai" / "decompiler-original.hlsl").read_text(
                encoding="utf-8"
            )
            self.assertIn("return 1", original)
            self.assertEqual(
                (workspace / "source" / "shader.hlsl").read_text(encoding="utf-8"),
                organized,
            )
            sessions = list((workspace / "ai" / "organization-sessions").iterdir())
            self.assertEqual(len(sessions), 1)
            self.assertTrue((sessions[0] / "session.json").is_file())
            self.assertTrue((sessions[0] / "model-response.json").is_file())
            self.assertIn(
                "A[Inputs] --> B[Shade]",
                (workspace / "ai" / "flowchart.mmd").read_text(encoding="utf-8"),
            )
            gate = json.loads(
                (workspace / "ai" / "organize-code-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(gate["codeSha256Equal"])
            self.assertEqual(gate["decision"], "accepted-executable-editable")
            self.assertTrue(gate["finalBinding"]["complete"])
            self.assertFalse(gate["semanticEquivalenceVerified"])
            self.assertEqual(mcp.compile_attempts, 2)
            self.assertIn(
                "begin_shader_workspace_organization",
                [event[1] for event in events if event[0] == "mcp"],
            )
            self.assertIn(
                "review_shader_workspace_organization",
                [event[1] for event in events if event[0] == "mcp"],
            )
            first_compile = next(
                index
                for index, event in enumerate(events)
                if event[0] == "mcp" and event[1] == "compile_shader_workspace"
            )
            first_model = next(
                index for index, event in enumerate(events) if event[0] == "model"
            )
            self.assertLess(first_compile, first_model)
            prompt = responses.requests[0]["input"][0]["content"]
            prompt_payload = json.loads(prompt.split("\n", 1)[1])
            semantic_context = json.loads(
                (sessions[0] / "semantic-context.json").read_text(encoding="utf-8")
            )
            self.assertEqual(prompt_payload["semanticContext"], semantic_context)
            self.assertEqual(
                prompt_payload["semanticContext"]["interface"],
                semantic_context["interface"],
            )
            live_evidence = prompt_payload["semanticContext"]["liveBindingEvidence"]
            self.assertEqual(
                live_evidence["boundTextures"],
                semantic_context["liveBindingEvidence"]["boundTextures"],
            )
            self.assertEqual(
                live_evidence["pipelineState"],
                semantic_context["liveBindingEvidence"]["pipelineState"],
            )
            self.assertEqual(live_evidence["captureId"], "c" * 64)
            self.assertEqual(live_evidence["captureSessionId"], SESSION)
            fidelity = prompt_payload["reconstructionFidelity"]
            self.assertEqual(
                fidelity,
                prompt_payload["semanticContext"]["reconstructionFidelity"],
            )
            self.assertFalse(fidelity["codeSha256Equal"])
            self.assertTrue(fidelity["currentCandidateReplayAbiCompatible"])
            self.assertEqual(
                fidelity["classification"],
                "abi-compatible-executable-code-mismatch",
            )
            self.assertFalse(fidelity["semanticEquivalenceVerified"])

    async def test_organize_only_code_change_is_retained_provisional_and_raw_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = "float4 main() : SV_Target { return 1; }\n"
            organized = "float4 main() : SV_Target { return 2; }\n"
            mcp = _FakeMCP(workspace_source=original)
            mcp.compile_code_sha256s = ["5" * 64, "6" * 64, "5" * 64]
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "organizedSource": organized,
                                "analysis": "Attempted an organize-only rewrite.",
                                "flowchartMermaid": "flowchart TD\n  A[Input] --> B[Output]",
                            }
                        ),
                    )
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                    organize_source=True,
                ),
            )
            await engine.initialise()

            await engine.process_intent(_intent("reverse-edit-selected-shader"))

            workspace = next(
                path
                for path in Path(temporary).iterdir()
                if path.name.startswith("e142-")
            )
            source_path = workspace / "source" / "shader.hlsl"
            raw_backup = workspace / "ai" / "decompiler-original.hlsl"
            provisional = workspace / "ai" / "organized-provisional.hlsl"
            self.assertEqual(source_path.read_bytes(), raw_backup.read_bytes())
            self.assertEqual(source_path.read_text(encoding="utf-8"), original)
            self.assertEqual(provisional.read_text(encoding="utf-8"), organized)
            self.assertFalse((workspace / "ai" / "organized.hlsl").exists())
            self.assertEqual(
                (workspace / "ai" / "analysis.md").read_text(encoding="utf-8"),
                "Attempted an organize-only rewrite.",
            )

            gate = json.loads(
                (workspace / "ai" / "organize-code-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(gate["codeSha256Equal"])
            self.assertFalse(gate["organizedSourcePromoted"])
            self.assertEqual(
                gate["decision"], "retained-provisional-code-change"
            )
            self.assertFalse(gate["safeToStartLiveEdit"])
            self.assertEqual(gate["baselineCodeSha256"], "5" * 64)
            self.assertEqual(gate["organizedCodeSha256"], "6" * 64)
            self.assertEqual(gate["finalCodeSha256"], "5" * 64)
            self.assertEqual(mcp.compile_attempts, 3)
            self.assertEqual(
                len(
                    [
                        event
                        for event in mcp.events
                        if event[1] == "validate_shader_workspace_candidate"
                    ]
                ),
                3,
            )
            latest = mcp.compiled_candidates[str(workspace.resolve())]
            self.assertEqual(latest["codeSha256"], "5" * 64)
            self.assertEqual(
                latest["sourceSha256"],
                hashlib.sha256(source_path.read_bytes()).hexdigest(),
            )
            self.assertFalse(
                any(
                    event[1].startswith("apply_shader_workspace")
                    for event in mcp.events
                )
            )

    async def test_organization_compile_failure_restores_original_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP()
            mcp.fail_compile_attempt = 2
            original = "float4 main() : SV_Target { return 1; }\n"
            mcp.workspace_source = original
            responses = _FakeResponses(
                [
                    _FakeResponse(
                        [],
                        json.dumps(
                            {
                                "organizedSource": "float4 main() : SV_Target { return 2; }\n",
                                "analysis": "A review-only rewrite.",
                                "flowchartMermaid": "flowchart TD\n  A[Input] --> B[Output]",
                            }
                        ),
                    )
                ]
            )
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                    organize_source=True,
                ),
            )
            await engine.initialise()
            with self.assertRaisesRegex(Exception, "baseline source restored"):
                await engine.process_intent(_intent("reverse-edit-selected-shader"))
            workspace = next(
                path for path in Path(temporary).iterdir() if path.name.startswith("e142-")
            )
            self.assertEqual(
                (workspace / "source" / "shader.hlsl").read_text(encoding="utf-8"),
                original,
            )
            session_dir = next(
                (workspace / "ai" / "organization-sessions").iterdir()
            )
            failure = json.loads(
                (session_dir / "failure.json").read_text(encoding="utf-8")
            )
            self.assertTrue(failure["sourceRestored"])
            gate = json.loads(
                (workspace / "ai" / "organize-code-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(gate["safeToStartLiveEdit"])
            self.assertEqual(mcp.compile_attempts, 3)

    async def test_organization_rejects_external_include_before_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            mcp = _FakeMCP(workspace_source='#include "outside.hlsl"\nfloat4 main():SV_Target{return 1;}\n')
            responses = _FakeResponses([])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    decompiler_tool_id="dxbc-decompiler",
                    compiler_tool_id="fxc",
                    organize_source=True,
                ),
            )
            await engine.initialise()
            with self.assertRaisesRegex(Exception, "self-contained"):
                await engine.process_intent(_intent("reverse-edit-selected-shader"))
            self.assertEqual(responses.requests, [])

    async def test_intent_poller_claims_renews_contract_and_completes(self):
        stop = asyncio.Event()
        mcp = _FakeMCP()
        mcp.claims = [_intent("explain-current-event")]
        mcp.stop_event = stop
        responses = _FakeResponses([_FakeResponse([], "Explained")])
        engine = CompanionEngine(
            mcp,
            responses,
            CompanionConfig(intent_poll_seconds=0.2, intent_lease_seconds=5),
        )
        await engine.initialise()
        await asyncio.wait_for(engine.poll_intents(stop), timeout=2.0)
        self.assertEqual(mcp.completed[0]["outcome"], "completed")
        self.assertEqual(mcp.completed[0]["summary"], "Explained")

    async def test_long_intent_renews_lease_while_work_is_running(self):
        mcp = _FakeMCP()
        engine = CompanionEngine(
            mcp,
            _FakeResponses([]),
            CompanionConfig(intent_lease_seconds=5),
        )
        await engine.initialise()

        async def slow_process(_intent_value):
            await asyncio.sleep(2.1)
            return "done"

        engine.process_intent = slow_process
        result = await engine._process_with_live_lease(_intent())
        self.assertEqual(result, "done")
        self.assertTrue(
            any(event[1] == "renew_renderdoc_ui_intent" for event in mcp.events)
        )

    async def test_auto_apply_runs_fresh_context_validate_then_shared_resource_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            mcp = _FakeMCP()
            responses = _FakeResponses([])
            engine = CompanionEngine(
                mcp,
                responses,
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                    auto_apply=True,
                ),
            )
            await engine.initialise()
            await engine._auto_apply_watch(
                _TrackedWatch(
                    "f" * 32,
                    str(workspace),
                    SESSION,
                    compile_successes=1,
                    latest_candidate_revision=4,
                    latest_candidate_raw_sha256="4" * 64,
                )
            )
            names = [event[1] for event in mcp.events]
            self.assertEqual(
                names[-7:],
                [
                    "get_current_renderdoc_context",
                    "get_current_renderdoc_context",
                    "validate_shader_workspace_candidate",
                    "get_current_renderdoc_context",
                    "get_current_renderdoc_context",
                    "apply_shader_workspace_shared_resource",
                    "get_current_renderdoc_context",
                ],
            )
            self.assertLess(
                names.index("validate_shader_workspace_candidate"),
                names.index("apply_shader_workspace_shared_resource"),
            )
            validation_arguments = next(
                event[2]
                for event in mcp.events
                if event[1] == "validate_shader_workspace_candidate"
            )
            apply_arguments = next(
                event[2]
                for event in mcp.events
                if event[1] == "apply_shader_workspace_shared_resource"
            )
            for arguments in (validation_arguments, apply_arguments):
                self.assertEqual(arguments["expected_revision"], 4)
                self.assertEqual(
                    arguments["expected_candidate_raw_sha256"], "4" * 64
                )

    async def test_auto_apply_waits_for_latest_caught_up_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()

            class PollingMCP(_FakeMCP):
                def __init__(inner_self):
                    super().__init__()
                    inner_self.polls = [
                        {
                            "state": "running",
                            "compileSuccesses": 1,
                            "caughtUp": False,
                            "latestCandidateRevision": 1,
                            "latestCandidateRawSha256": "1" * 64,
                        },
                        {
                            "state": "running",
                            "compileSuccesses": 2,
                            "caughtUp": True,
                            "latestCandidateRevision": 2,
                            "latestCandidateRawSha256": "2" * 64,
                        },
                    ]

                async def call_tool(inner_self, name, arguments):
                    if name == "poll_shader_workspace_watch":
                        inner_self.events.append(("mcp", name, dict(arguments)))
                        state = inner_self.polls.pop(0)
                        return json.dumps({"result": state})
                    return await super().call_tool(name, arguments)

            mcp = PollingMCP()
            notices: list[str] = []
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    intent_poll_seconds=0.2,
                    watch_saved_shaders=True,
                    auto_apply=True,
                ),
                notify=notices.append,
            )
            await engine.initialise()
            tracked = _TrackedWatch("f" * 32, str(workspace), SESSION)
            task = asyncio.create_task(engine._monitor_workspace(tracked))
            deadline = asyncio.get_running_loop().time() + 3.0
            while not any("ABI/live gates passed" in item for item in notices):
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("latest caught-up revision was not auto-applied")
                await asyncio.sleep(0.02)
            engine._closing = True
            await asyncio.wait_for(task, timeout=2.0)

            validation_calls = [
                event[2]
                for event in mcp.events
                if event[1] == "validate_shader_workspace_candidate"
            ]
            apply_calls = [
                event[2]
                for event in mcp.events
                if event[1] == "apply_shader_workspace_shared_resource"
            ]
            self.assertEqual(len(validation_calls), 1)
            self.assertEqual(len(apply_calls), 1)
            self.assertEqual(validation_calls[0]["expected_revision"], 2)
            self.assertEqual(apply_calls[0]["expected_revision"], 2)
            self.assertEqual(
                apply_calls[0]["expected_candidate_raw_sha256"], "2" * 64
            )
            self.assertTrue(
                any(event[1] == "stop_shader_workspace_watch" for event in mcp.events)
            )

    async def test_capture_switch_stops_watcher_without_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            mcp = _FakeMCP()
            mcp.current_context = _context()
            mcp.current_context["capture"] = dict(mcp.current_context["capture"])
            mcp.current_context["capture"]["captureSessionId"] = "9" * 32
            notices: list[str] = []
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    intent_poll_seconds=0.2,
                    watch_saved_shaders=True,
                    auto_apply=True,
                ),
                notify=notices.append,
            )
            await engine.initialise()
            await asyncio.wait_for(
                engine._monitor_workspace(
                    _TrackedWatch("f" * 32, str(workspace), SESSION)
                ),
                timeout=2.0,
            )
            names = [event[1] for event in mcp.events]
            self.assertIn("stop_shader_workspace_watch", names)
            self.assertNotIn("validate_shader_workspace_candidate", names)
            self.assertNotIn("apply_shader_workspace_shared_resource", names)
            self.assertTrue(any("capture session changed" in item for item in notices))

    async def test_compile_failure_is_reported_without_replacing_last_good(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()

            class FailedPollMCP(_FakeMCP):
                async def call_tool(inner_self, name, arguments):
                    if name == "poll_shader_workspace_watch":
                        inner_self.events.append(("mcp", name, dict(arguments)))
                        return json.dumps(
                            {
                                "result": {
                                    "state": "running",
                                    "compileSuccesses": 0,
                                    "compileFailures": 1,
                                    "caughtUp": False,
                                    "latestCandidateRevision": None,
                                    "latestCandidateRawSha256": None,
                                    "lastError": {
                                        "message": "syntax error at line 4"
                                    },
                                }
                            }
                        )
                    return await super().call_tool(name, arguments)

            mcp = FailedPollMCP()
            notices: list[str] = []
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    intent_poll_seconds=0.2,
                    watch_saved_shaders=True,
                    auto_apply=True,
                ),
                notify=notices.append,
            )
            await engine.initialise()
            task = asyncio.create_task(
                engine._monitor_workspace(
                    _TrackedWatch("f" * 32, str(workspace), SESSION)
                )
            )
            deadline = asyncio.get_running_loop().time() + 2.0
            while not any("syntax error at line 4" in item for item in notices):
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("compile failure was not reported")
                await asyncio.sleep(0.02)
            engine._closing = True
            await asyncio.wait_for(task, timeout=2.0)
            names = [event[1] for event in mcp.events]
            self.assertNotIn("validate_shader_workspace_candidate", names)
            self.assertNotIn("apply_shader_workspace_shared_resource", names)
            self.assertIn("stop_shader_workspace_watch", names)

    async def test_failed_latest_gate_keeps_previous_shared_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            mcp = _FakeMCP()
            mcp.replay_abi_validated = False
            notices: list[str] = []
            engine = CompanionEngine(
                mcp,
                _FakeResponses([]),
                CompanionConfig(
                    workspace_root=temporary,
                    watch_saved_shaders=True,
                    auto_apply=True,
                ),
                notify=notices.append,
            )
            await engine.initialise()
            previous = _AppliedWorkspace(
                str(workspace), SESSION, "b" * 32, 902
            )
            engine._remember_active_shared_replacement(previous)
            await engine._auto_apply_watch(
                _TrackedWatch(
                    "f" * 32,
                    str(workspace),
                    SESSION,
                    compile_successes=2,
                    latest_candidate_revision=2,
                    latest_candidate_raw_sha256="2" * 64,
                )
            )
            self.assertEqual(engine._latest_active_shared_replacement(), previous)
            self.assertFalse(
                any(
                    event[1] == "apply_shader_workspace_shared_resource"
                    for event in mcp.events
                )
            )
            self.assertTrue(any("Auto-apply rejected" in item for item in notices))


class OrganisedReadabilityContractTests(unittest.TestCase):
    def test_organization_policy_requires_semantic_aliases_without_changing_cbuffer_abi(self):
        self.assertEqual(
            ORGANIZE_POLICY_VERSION,
            "renderdoc-ai-organize/v6",
        )
        self.assertIn("numeric slots, or byte-offset variables", ORGANIZE_INSTRUCTIONS)
        self.assertIn("role-derived #define aliases or typed accessors", ORGANIZE_INSTRUCTIONS)
        self.assertIn("keep every ABI declaration unchanged", ORGANIZE_INSTRUCTIONS)
        self.assertIn("register/space, packoffset, byte offset", ORGANIZE_INSTRUCTIONS)

    def test_organization_policy_requires_compact_lines_and_internal_chinese_comments(self):
        self.assertIn("at most 120 columns", ORGANIZE_INSTRUCTIONS)
        self.assertIn("counting leading indentation", ORGANIZE_INSTRUCTIONS)
        self.assertIn("keep it on one physical line", ORGANIZE_INSTRUCTIONS)
        self.assertIn("only at syntactically safe boundaries", ORGANIZE_INSTRUCTIONS)
        self.assertIn("Inside every long or core function", ORGANIZE_INSTRUCTIONS)
        self.assertIn("comments only before the function are insufficient", ORGANIZE_INSTRUCTIONS)
        self.assertIn("trivial declarations, assignments, braces", ORGANIZE_INSTRUCTIONS)
        self.assertIn("original operator association", ORGANIZE_INSTRUCTIONS)
        self.assertIn("side-effect order", ORGANIZE_INSTRUCTIONS)
        self.assertIn("Never use associativity, commutativity or distributivity", ORGANIZE_INSTRUCTIONS)

    def test_algorithm_body_rejects_long_lines_but_not_long_abi_declarations(self):
        source = "float4 MainPS() : SV_Target { return 0.0f; }\n"
        prefix = "    float "
        suffix = " = 1.0f;"
        exact_identifier = "v" * (120 - len(prefix) - len(suffix))
        exact_limit = (
            "float4 MainPS() : SV_Target {\n"
            + prefix
            + exact_identifier
            + suffix
            + "\n    return 0.0f;\n}\n"
        )
        self.assertEqual(len(exact_limit.splitlines()[1]), 120)
        CompanionEngine._validate_organized_readability(source, exact_limit)

        long_identifier = "algorithmValue" * 10
        long_algorithm = (
            "float4 MainPS() : SV_Target {\n"
            "    float %s = 1.0f;\n"
            "    return 0.0f;\n"
            "}\n" % long_identifier
        )
        self.assertGreater(len("    float %s = 1.0f;" % long_identifier), 120)
        with self.assertRaisesRegex(CompanionError, "exceeds 120 columns"):
            CompanionEngine._validate_organized_readability(source, long_algorithm)

        long_abi_name = "physicalCBufferMember" * 8
        organised = (
            "cbuffer PhysicalLayout : register(b0) { float4 %s; };\n"
            "float4 MainPS() : SV_Target { return 0.0f; }\n" % long_abi_name
        )
        self.assertGreater(len(organised.splitlines()[0]), 120)
        CompanionEngine._validate_organized_readability(source, organised)

    def test_short_logical_statement_must_remain_on_one_line(self):
        source = "float4 MainPS() : SV_Target { return 0.0f; }\n"
        organised = (
            "float4 MainPS() : SV_Target {\n"
            "    float2 rippleCoordinates =\n"
            "        float2(1.0f, 2.0f) + float2(3.0f, 4.0f);\n"
            "    return rippleCoordinates.xxyy;\n"
            "}\n"
        )
        with self.assertRaisesRegex(CompanionError, "split a <=120-column"):
            CompanionEngine._validate_organized_readability(source, organised)

    def test_long_function_requires_chinese_comments_inside_its_body(self):
        source = "float4 MainPS() : SV_Target { return 0.0f; }\n"
        calculations = "\n".join("    value += 1.0f;" for _ in range(12))
        header_only = (
            "// 中文函数说明\n"
            "float4 MainPS() : SV_Target {\n"
            "    float value = 0.0f;\n"
            + calculations
            + "\n    return value.xxxx;\n}\n"
        )
        with self.assertRaisesRegex(CompanionError, "lacks Chinese phase/dataflow"):
            CompanionEngine._validate_organized_readability(source, header_only)

        with_internal_phases = header_only.replace(
            "    float value = 0.0f;\n",
            "    // 中文：准备待累计的调试值。\n"
            "    float value = 0.0f;\n"
            "    // 中文：累计核心计算阶段。\n",
        )
        CompanionEngine._validate_organized_readability(source, with_internal_phases)

    def test_large_ssa_source_requires_all_numeric_locals_helpers_and_chinese_comments(self):
        numeric = "\n".join(
            "float _%d = %d.0f;" % (index, index) for index in range(16)
        )
        source = numeric + "\n" + ("// padding\n" * 300)
        with self.assertRaisesRegex(CompanionError, "underscore-number"):
            CompanionEngine._validate_organized_readability(
                source,
                "float _7 = 1.0f;\n" + ("// padding\n" * 300),
            )
        with self.assertRaisesRegex(CompanionError, "module functions"):
            CompanionEngine._validate_organized_readability(
                source,
                "float4 MainPS() : SV_Target { return 0.0f; }\n"
                + ("// 中文说明\n" * 300),
            )

        organised = "\n".join(
            [
                "// 中文数据准备",
                "float PrepareData(float x) { return x; }",
                "// 中文材质计算",
                "float EvaluateMaterial(float x) { return x; }",
                "// 中文光照计算",
                "float EvaluateLighting(float x) { return x; }",
                "// 中文输出打包",
                "float4 MainPS() : SV_Target {",
                "  float value = PrepareData(1.0f);",
                "  return EvaluateMaterial(value) + EvaluateLighting(value);",
                "}",
            ]
        )
        CompanionEngine._validate_organized_readability(source, organised)


class MCPResultShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fastmcp_string_structured_result_is_unwrapped(self):
        payload = json.dumps(_context())

        class Session:
            async def call_tool(self, name, arguments):
                return types.SimpleNamespace(
                    structuredContent={"result": payload},
                    content=[types.SimpleNamespace(text=payload)],
                    isError=False,
                )

        client = StdioMCPClient(Session())
        self.assertEqual(await client.call_tool("context", {}), payload)
        self.assertEqual(
            (await client.call_json("context", {}))["capture"]["captureSessionId"],
            SESSION,
        )

    async def test_real_fastmcp_string_result_shape_is_unwrapped(self):
        try:
            from mcp.server.fastmcp import FastMCP
            from mcp.shared.memory import create_connected_server_and_client_session
        except ImportError:
            self.skipTest("MCP SDK is not installed in this test environment")

        payload = json.dumps(_context())
        server = FastMCP("companion-result-shape-test")

        @server.tool()
        def context() -> str:
            return payload

        async with create_connected_server_and_client_session(server) as session:
            direct = await session.call_tool("context", {})
            self.assertEqual(direct.structuredContent, {"result": payload})
            client = StdioMCPClient(session)
            self.assertEqual(await client.call_tool("context", {}), payload)
            self.assertEqual(
                (await client.call_json("context", {}))["capture"][
                    "captureSessionId"
                ],
                SESSION,
            )


if __name__ == "__main__":
    unittest.main()
