# ReplayLab AI architecture

This document describes the implemented ReplayLab AI boundary. Historical planner, reviewer, receipt, WorkflowGuard, and Custom Agent designs are not part of the runtime.

## 1. System boundary

```text
qrenderdoc
  ├─ captures a hash-bound current-context snapshot
  ├─ starts/cancels replaylab-ai
  └─ renders replaylab-ai-task-event/v1 JSONL
             │
             ▼
AgentHost (`replaylab_agent`)
  ├─ model profile ──► DeepAgentsEngine ──► deepagents main/Skill sub-agents
  └─ agent profile ──► NativeAgentEngine ──► ACP or Codex native single Agent
             │
             ▼
task-scoped ReplayLab MCP
  ├─ instance and frozen-scope binding
  ├─ tool allowlist/effect/call-budget enforcement
  ├─ RenderDoc and project tools
  ├─ artifact validation and publication
  └─ report_task_result + task report sidecars
             │
             ▼
first-party qrenderdoc Python bridge / replay controller / project services
```

The qrenderdoc bridge is bundled at the fixed first-party application path. It is not discovered as a Python extension and has no `extension.json`.

## 2. Engine selection

The profile kind is the only selector:

| Profile kind | Engine | Owner of the reasoning loop |
|---|---|---|
| `model` | `DeepAgentsEngine` (`deepagents/v1`) | the pinned GitHub deepagents runtime |
| `agent` | `NativeAgentEngine` (`vendor-native-agent/v1`) | the selected vendor CLI through ACP or Codex app-server |

`cli._run_once()` performs this mapping directly. There is no legacy selector, compatibility engine, pydantic-ai path, or silent fallback. Unsupported profile/protocol combinations fail before generation.

## 3. Shared run contracts

`agent_contracts.py` contains only bounded data structures and operational limits:

- `RunRequest`: prompt, profile, model, task id, frozen context, qrenderdoc instance, exact intent/Skill, and MCP executable;
- `OrchestratorConfig`: overall task timeout, effect ceiling, event/workspace roots, total MCP tool-call budget, and heartbeat interval;
- `RunResult`: terminal status, display text, optional clarification, child summaries, and structured report.

Despite the historical class name `OrchestratorConfig`, it is not a planner or workflow state machine. It is a set of deterministic resource limits loaded from `%LOCALAPPDATA%\ReplayLab\config\agent-engine.json`.

`max_tool_calls` is the shared task MCP budget for both API and vendor-native
execution. It defaults to 480 and accepts values from 1 to 1024; configure a
smaller value to limit a task's Host calls. The task MCP filter counts calls
across tools and API sub-agents, including calls whose upstream execution fails.
The removed `child_timeout`, `max_children`, `max_parallel`, and
`max_child_tool_calls` JSON fields are ignored. There is no separate Host
sub-agent timeout, count, or concurrency setting; `main_timeout` bounds the
whole task and the selected framework or vendor owns its internal execution.

Profiles live in `%APPDATA%\ReplayLab\ai\profiles.json` under `replaylab-ai-profiles/v2`. Raw API secrets are never stored there. On Windows they are held by Credential Manager and injected only into the selected provider process/model client.

## 4. Frozen context

For capture work qrenderdoc writes `renderdoc-ultra-current-context/v1` with a SHA-256 `snapshotId`. AgentHost recomputes the hash, binds the request to the selected live qrenderdoc instance, and rejects a mismatched or capture-free instance.

The snapshot is routing context only. A current EID, focused shader, texture, path, or warning in the snapshot is not sufficient evidence for a final observed claim. The Agent must call a ReplayLab Host tool and cite that exact tool in the task report.

## 5. Model API path

All model API presets are converted to LangChain chat models by `connectors/langchain_models.py`. The provider-specific protocol remains explicit: OpenAI Responses/chat, Anthropic Messages, Google GenAI/Vertex, Azure OpenAI, or a supported compatible endpoint.

`DeepAgentsEngine` then:

1. opens one task-scoped ReplayLab MCP session when a capture is attached;
2. builds one deepagents graph/Agent instance;
3. gives the main Agent `task`, `ask_user`, and `report_task_result`, but no direct RenderDoc tools;
4. creates bounded sub-agent definitions from the filesystem domain Skills plus a general read-only fallback;
5. gives each child only the ReplayLab tools allowed by the connection-level effect ceiling;
6. maps deepagents/LangGraph v2 stream events to public main/child/tool/reasoning events;
7. settles every unfinished child deterministically on timeout, cancellation, or stream failure;
8. performs the shared report verification loop before publishing the terminal event.

All children in one run share the task MCP session. Tool exceptions are converted to bounded `ERROR: ...` results so the model can correct arguments without bypassing Host policy.

The real-framework integration test imports the installed pinned deepagents package and runs a compiled graph. Fake-framework tests remain useful for precise event and failure cases, but are not accepted as the only compatibility signal.

## 6. Vendor-native subscription path

Subscription credentials remain inside the official vendor runtime. AgentHost provides one constrained environment and lets that runtime own the single-Agent loop.

### 6.1 Task workspace and MCP

For a capture-bound run, `build_native_mcp_route()` creates a unique workspace-local route containing:

- the exact qrenderdoc instance id and frozen scope JSON;
- the task id and task-event root;
- an explicit tool allowlist including `report_task_result`;
- allowed `workspace`, `project`, `ui`, and/or `replay` effects;
- a maximum tool-call count;
- a unique live-edit registry path.

The route launches the narrow `mcp_proxy.py`, which starts the first-party MCP Host and enforces those values. The proxy contains no tool receipts or workflow-state decisions.

### 6.2 ACP

ACP sessions receive the single ReplayLab stdio MCP declaration during session creation. Tool permission requests must resolve to an exact admitted ReplayLab tool; unknown, ambiguous, built-in, shell, or external tool requests fail closed.

Claude Code additionally receives a strict project MCP configuration and a copy of the selected filesystem Skills in the vendor-supported project location. A Claude `AskUserQuestion` form is projected through ACP elicitation into ReplayLab's `ClarificationRequest` and qrenderdoc buttons.

Layer-3 follow-ups are sent as another prompt on the same ACP session.

### 6.3 Codex app-server

Codex is launched with a single `mcp_servers.ReplayLab` configuration override. AgentHost hardening disables shell/terminal execution, Apps, generic computer use, and multi-agent delegation for this task. The selected Skill is passed as an instruction source; the vendor Agent still calls only the task-scoped MCP for capture facts and changes.

Codex app-server has no ReplayLab-native elicitation channel, so a material question uses the strictly delimited `[REPLAYLAB QUESTION]` numbered-choice block. The Host parses it into the same UI contract.

Each Codex layer-3 follow-up starts a new thread. Correctness does not depend on hidden conversation state because the follow-up includes the original request, previous answer, Host facts/tool names, and outstanding validation reasons.

## 7. Filesystem Skills

Skills are loaded from `.agents/skills/<id>/SKILL.md`. `replaylab-core` supplies the mandatory shared template/evidence/review/report contract to API main Agents, every child and every native Agent, including follow-up threads. Missing, invalid or disabled core blocks execution; optional domain Skills can be quarantined individually. Domain Skills add procedures, not another report schema or correction policy.

There is no executable `.agents/agents` registry in the runtime, no Custom Agents settings page, and no external MCP extension list. Skill text cannot widen the MCP allowlist or effect ceiling.

## 8. Deterministic output layers

### 8.1 Artifact layer

Host tools own artifact bytes and acceptance. Examples include shader compilation and fresh ABI checks, bounded output comparison, typed analysis/module/flow templates, fixed rendering, export manifests, project registration, and rollback ownership. Models fill tool-returned fields; the Host renders the document or diagram. New AI submissions cannot override the renderer with raw Mermaid.

`mcp_host/src/review_contract.py` defines the common `replaylab-review-verdict/v1`, blocker/warning reasons, locations, repair targets/examples and bounded attempts. `REPLAYLAB_REVIEW_MAX_ROUNDS` defaults to 3 and is clamped to 1–10; `REPLAYLAB_REPORT_MAX_ROUNDS` and local `review.json` settings remain fallbacks. Minor issues may pass with explicit notes; blocking errors remain rejected at exhaustion. A shader fallback can publish only a previously validated baseline and accepted documentation, not certify the failed proposal.

### 8.2 Report layer

The canonical implementation is `mcp_host/src/task_report.py`. `replaylab_agent/agent_report.py` imports that implementation; it must remain a thin adapter.

`report_task_result` accepts `replaylab-task-report/v1`:

- summary and completed/partial/failed status;
- observed or inferred findings;
- confidence for inferences;
- exact Host tool evidence for observations;
- artifacts, unknowns, and next steps.

The MCP Host instruments admitted task tools and persists tool names, bounded sanitized result summaries and report-gate state in task-id sidecars under the event root. Both engines use this same store; framework virtual file-tool events are not Host evidence. Reopening a store cannot reset attempts. The gate validates artifact paths, status consistency and schema; these blocking errors cannot soft-accept after retries. A called tool name is provenance, not a proof that every prose claim matches its result.

### 8.3 Verification layer

After a working turn, both engines invoke the same `ReportGate.verify(worked=...)`. If work occurred without an accepted report, the Host builds `verification_follow_up_prompt()` with exact reason codes, locations, repair directions, prior answer, existing child/Host evidence, and tool names.

- deepagents reruns the same compiled graph/Agent instance;
- ACP prompts the same session;
- Codex creates a new thread with the self-contained follow-up.

`REPLAYLAB_VERIFY_MAX_ROUNDS` is bounded to 0–5 and defaults to 2. Zero disables layer 3. Exhaustion preserves the current results and `verification.accepted=false`, without falsely claiming completed work. An existing failure remains failed; a missing form alone yields partial. Codex follow-ups repeat the original system/core contract and use the persisted Host evidence. Truncated evidence permits narrow reads, not repeated completed mutations.

## 9. Effects and mutation

`policy.py` is an explicit first-party tool manifest:

- read-only tools have no effect class;
- mutations declare one or more of `workspace`, `project`, `ui`, `replay`;
- registered tools missing a declaration fail validation;
- parameter-sensitive tools calculate their actual effects from bounded boolean arguments.

The connection establishes a ceiling. A Skill or model request may use less authority but cannot gain more. Replay mutations still require the tool's own fresh capture/resource/ABI checks and rollback ownership.

## 10. User questions and terminal states

`ClarificationRequest` carries a bounded question, two to six choices, single/multiple selection, and optional free text. It is emitted as `needs-user-input`, which takes precedence over report status and layer-3 follow-up. A later UI turn supplies a `clarificationResponse` conversation envelope.

Other terminal results are `completed`, `partial`, or `failed` at the UI/event boundary. A partial artifact/report or exhausted verification can produce `partial`; a missing report alone is never rewritten to failed.

## 11. Task events and UI projection

Events are append-only JSONL under `%LOCALAPPDATA%\ReplayLab\ai-tasks\<taskId>.jsonl`. They expose bounded public information:

- queue/start/heartbeat and cancellation;
- main and deepagents child lifecycle;
- scrubbed tool/timeline activity and reasoning summaries;
- final answer or clarification;
- terminal report, artifacts, and optional verification payload.

qrenderdoc renders the structured report first. Provider prose is secondary Notes. The UI uses main/child terminology and does not expose the removed planner/reviewer/specialist runtime roles.

## 12. Alias storage

Constant-buffer block and member aliases use `replaylab-cbuffer-aliases/v2` under `%LOCALAPPDATA%\ReplayLab\aliases\cbuffer\<layoutSha256>.json`. C++ and Python calculate the same UTF-8 layout signature. User entries win over AI entries, malformed files fail closed, and writes are atomic.

Aliases are display metadata. They never alter reflection identifiers, material field keys, or compiled shader bytes. Shader Workspace keeps its actual compile/replace source as the primary editable document; organized source and alias maps remain review artifacts. A future alias view should be explicitly read-only unless a complete diagnostic/watch/CAS/rollback mapping is designed.

## 13. Packaging

Python 3.12 is the supported AgentHost runtime. The build produces the frozen AgentHost, ReplayLab MCP Host, and internal qrenderdoc service with a versioned hash manifest. The deploy script validates the source and target, owns only marker-listed files, rejects unsafe reparse-point layouts, and never adopts or removes unrelated application files.

The pinned deepagents, LangGraph, LangChain providers, ACP, and MCP packages are normal runtime dependencies and must be collected by the frozen build. Legacy pydantic-ai/FastMCP Agent packages are not collected.

## 14. Invariants for future changes

1. Do not add an engine selector or fallback: profile kind determines the engine.
2. Do not recreate planner/reviewer/receipt workflow logic around vendor or deepagents loops.
3. Keep the report schema and validator single-sourced in the MCP Host.
4. Keep layer-3 feedback factual, located, directional, bounded, and self-contained.
5. Never treat frozen context or provider narration as observed evidence.
6. Do not expose shell, generic file mutation, Apps, computer use, or vendor sub-agents to subscription capture tasks.
7. The unused `docs/handoff/wip-layer3/` Python drafts have been removed. The current engines and Host are the only runtime implementation.
8. Preserve `replaylab-ai-task-event/v1` compatibility unless UI and Host migrate atomically.
9. Keep all qrenderdoc UI strings extractable and the Simplified Chinese catalog complete.
10. Use `/m:2` for qrenderdoc builds on memory-constrained Windows hosts.

## 15. Verification matrix

Automated validation covers unit/contract tests, a real offline deepagents graph, ACP/Codex fixtures, report sidecars, verification rounds, effect enforcement, packaging/deployment safety, translation gates, and qrenderdoc compilation.

Real provider validation is a separate release activity because it requires user-owned accounts and may consume quota. For each supported release path, compare the same capture tasks by Host report/artifact/verdict output—not by identical prose—and then build/deploy the frozen runtime into the target x64 Release layout.
