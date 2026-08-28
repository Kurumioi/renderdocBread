# RenderDoc AI configuration and orchestration

This document describes the product contract implemented by the native qrenderdoc AI surface.
ReplayLab is the hidden AgentHost inside that product; it is not a second desktop application.

## Built-in integration boundary

ReplayLab is a first-party qrenderdoc integration, not a Python extension. Its canonical bridge
source is `replaylab/renderdoc_mcp_bridge/` in this repository. Packaging copies that package to the
single runtime location
`<appdir>/integrations/replaylab/python/renderdoc_mcp_bridge/`, and qrenderdoc imports the exact
module from that application-owned directory.

The loader does not scan `extensions/`, read an `extension.json`, consult the user's `%APPDATA%`
extension directory, or accept a same-named user module. `integration.json` is first-party
version/entrypoint metadata for build and validation; it is not an extension discovery manifest.
Native qrenderdoc code owns lifecycle, availability, AI actions, and direct task dispatch. The
Python bridge remains an in-process implementation module for capture-context freezing and bridge
protocol v2, while AgentHost remains a hidden sidecar so network/provider dependencies do not enter
the qrenderdoc process. Model API and Codex specialists execute every active Shader and
Event/Pipeline contract through AgentHost's process-shared `InternalToolService`; this path never
starts ReplayLabMCP. ACP/Cursor receives the same contracts through one task-filtered MCP session
because ACP v1 has no equivalent Host dynamic-tool callback. The same executable is also the adapter
for explicitly connected external clients; it is never exposed as an unfiltered ambient inventory.

Ordinary qrenderdoc startup owns the private `ReplayLabInternal.exe` service and, by default, one
`ReplayLabMCP.exe` loopback for external Codex/IDE clients. Internal Agent, project-restore, and
Shader-save paths still use only the private service or AgentHost's in-process tools; they never
fall back to the external MCP adapter. ACP/Cursor capture specialists retain an isolated task-level
stdio MCP session. Deployments that do not expose external clients can opt out with
`REPLAYLAB_EXTERNAL_MCP_AUTOSTART=0` (also `false`, `no`, or `off`).

This boundary keeps product-facing behavior native without discarding the tested bridge protocol:

```text
qrenderdoc native UI/lifecycle
  -> typed ReplayLab integration API
  -> fixed renderdoc_mcp_bridge package
  -> bridge protocol v2
  -> pinned AgentHost
       -> shared InternalToolService (model API and Codex dynamicTools)

ACP / external IDE -> task-filtered ReplayLabMCP.exe -> the same first-party bridge
```

## Connection concepts

A **connection** is one saved provider configuration. It has two identities:

- `title` is the user-facing connection name, for example `Work OpenAI` or `Personal Codex`.
- `id` is an automatically generated stable internal key. It keeps session credentials, workers,
  tasks, and profile updates attached to the same connection after the title changes. The Settings
  UI deliberately does not ask users to invent or manage this ID.

Each connection also owns an allow-listed model set and one default model. The allow-list keeps the
chat picker short and prevents a stale UI selection from silently invoking an unapproved model.
Selecting another connection or model beside **Send** changes only the current qrenderdoc session;
it does not rewrite either saved default. Persistent defaults are changed only in **Settings → AI**.

### Model API connection

The normal setup path is:

1. Choose **Model API**, enter a connection name, and choose the provider adapter.
2. Confirm or paste the API base address and enter the API key.
3. Select **Fetch Models**. Enable only the models that should appear in qrenderdoc and choose the
   default.
4. Save the connection and optionally make it the default connection.

The key entered in qrenderdoc is passed to AgentHost over stdin. By default it is stored as a
Generic Credential in the current Windows user's Credential Manager, keyed by the connection's
stable internal id; `profiles.json` contains no raw secret. **Securely remember this key** can be
disabled for a session-only key, and **Forget Saved Key** removes the vault entry. Environment
variables remain an explicit automation/CI override and take precedence over the vault. Platforms
without an implemented OS vault fail closed instead of falling back to plaintext storage.

Provider-specific exceptions are progressively disclosed. For example, legacy Azure OpenAI needs
an API version and uses a deployment name, while Azure v1/Foundry endpoints do not use that field.
Vertex uses ADC plus project/location rather than a normal API key. These exceptions cannot safely
be hidden behind arbitrary string guessing.

### Agent subscription connection

Subscriptions are not generic API keys. They are connected only through a provider's public Agent
protocol:

- **Codex app-server** is a local child process using JSONL over stdin/stdout. qrenderdoc asks it for
  account state and starts the official browser or device-code login. Login tokens remain owned by
  Codex; ReplayLab neither reads nor copies its local token files. `model/list` supplies the model
  picker.
- **ACP** is normally a local vendor Agent command using the Agent Client Protocol. Authentication
  is vendor-specific and usually performed by that CLI. ACP does not define a universal address,
  API key, login, or pre-session model-list endpoint.
- qrenderdoc therefore drives a small audited vendor catalog: Copilot and Kimi expose bounded
  browser/device-code login commands; Gemini and Qwen require their official interactive terminal;
  custom ACP performs only an initialize/session handshake and never guesses a login command.
  Every operation runs in a fresh empty temporary workspace and removes it afterward. The CLI owns
  its credential cache; ReplayLab retains only safe authorization URLs/codes and bounded model ids.
- Some products called “subscriptions” are still key based. Alibaba Model Studio Coding Plan, for
  example, uses a dedicated endpoint plus a plan key and is represented as a Model API preset, not
  as a fabricated OAuth/ACP login.
- A future remote app-server transport may expose an address and a transport credential. That is a
  server transport secret, not the model-provider API key, and must be modelled separately.

An account subscription can be used only when its vendor explicitly exposes a supported app-server,
ACP server, or Agent SDK. The integration does not scrape browser cookies, local storage, or private
CLI credential files.

## Safe endpoint compatibility

OpenAI-compatible services commonly document one of a service root, a `/v1` base, `/models`,
`/chat/completions`, or `/responses`. The setup probe normalizes a known terminal operation and
tries at most two candidates: the entered base and the same path with one terminal `/v1` segment
toggled.

The probe follows these host-enforced rules:

- scheme, host, and port never change;
- HTTPS is never downgraded;
- redirects are not followed;
- credentials, query strings, and URL fragments are rejected in the base address;
- HTTP 401/403 stops immediately instead of replaying the credential;
- response size, attempt count, timeout, and model count are bounded;
- the resolved base is shown and saved only after a successful model-list response.

Anthropic and Google use their native model-list paths and authentication headers. Azure deployment
enumeration and Vertex model discovery require separate management-plane/IAM contracts, so the UI
allows an exact deployment/model name instead of pretending that a data-plane key can enumerate
them. No finite probe can guarantee every unofficial “compatible” server; failed discovery remains
actionable and manual rather than becoming unbounded URL guessing.

## Unified execution contract

Both model APIs and managed subscriptions enter the same outer workflow for capture-aware free
chat:

```text
typed conversation + frozen environment
  -> Host resolves clarification response and origin/current snapshot candidates
       -> if the user's target is ambiguous, ask_user before compaction or model execution
  -> zero-tool main supervisor -> GoalSpec + initial decision
       -> direct_answer / ask_user / blocked (zero specialist tasks), or first StagePlan
  -> host validates task IDs, dependencies, Skill names and budgets
  -> isolated Skill specialist session(s), with discovered tools matching that Skill and outcome
  -> deterministic evidence validator
       -> retryable: feedback to the same logical specialist context
       -> accepted: seal bounded SkillResult and release detailed child context
       -> failed/blocked: after cleanup is confirmed, seal a released failure receipt;
          dependent tasks are blocked
  -> main supervisor reads admitted results plus every terminal specialist receipt
       -> dispatch_next_stage / finish / ask_user / blocked
  -> one qrenderdoc answer and terminal TaskEvent
```

For model APIs, ReplayLab owns both the planner and specialist model loops. For Codex, the vendor
Agent owns each inner model/tool loop and calls host-provided `dynamicTools`. All active high-level
contracts are routed to the same `InternalToolService` used by model APIs. ACP v1 has no equivalent
dynamic-tool callback, so a capture-aware ACP specialist receives one filtered
first-party MCP server containing only that task's tools. ReplayLab still owns the same outer
supervisor, typed context, selected Skill, semantic workflow effects, scheduling, evidence admission,
acceptance and final result set on every transport. This is a shared quality contract, not a promise
that different models or vendor Agent harnesses will produce identical prose.

A ReplayLab child Agent is one host-accounted, isolated specialist session. It keeps its bounded
context across acceptance retries, returns a terminal receipt, and is released only after cleanup is
confirmed. Requests that need no capture evidence may be answered directly. The active catalog has
only the Shader and Event/Pipeline specialists; an unmatched request is handled by the main Agent or
clarified instead of being routed to an invented generic domain specialist.

The outer workflow is `adaptive-supervisor-specialists/langgraph-v2`. LangGraph provides typed state
transitions and the dynamic loop; Pydantic AI remains the project's model/tool-loop adapter. A stage
is still a bounded `TaskPlan` with at most three tasks. The host defaults to three stages, eight total
tasks and two specialist acceptance attempts. Models propose state; only host validation mutates it.
A failed independent specialist no longer cancels its siblings: the stage records the accepted-result
subset and exactly one released receipt for every planned task. Failed tasks still consume the total
budget. A `finish` proposal is blocked while the latest stage still has a failed/blocked receipt;
the supervisor can recover only by dispatching a later corrective stage whose specialists are all
accepted. If a child transport cannot prove cleanup, the lifecycle error is fatal and no released
receipt is fabricated.
The reviewer receives exact remaining stage/task budgets before deciding whether to continue.
A legacy `TaskPlan` response is accepted by a single-stage v1 adapter so older/local models fail soft
rather than losing the existing safe path; partial legacy execution returns a blocked workflow receipt
instead of entering the strict complete-result synthesizer.

Button actions skip domain selection because the user already chose a workflow. **Explain Event**
directly invokes `renderdoc-explain-event` with the bundled `inspect_event` transaction, and an
explicit picked-pixel action uses `analyze_pixel`. **Reverse/Edit with AI** selects
`renderdoc-shader-workbench`; it is not artificially reduced to one read call, because compile,
project publication, replay replacement and rollback may be legitimate parts of the requested
outcome. qrenderdoc passes a stable native action ID and frozen target to AgentHost; the Host still
enforces the selected workflow's ordering and postconditions.

## Context windows and compaction

qrenderdoc sends a versioned JSON conversation envelope with stable, non-translatable field and role
identifiers. It contains the current request separately from ordered history, plus a conversation id
and an omitted-history count. This removes the old translated-prefix and `USER:` delimiter ambiguity.
The native prompt wire ceiling is 16 MiB and remains a transport safety gate, not a model-window
estimate.

Structured clarification is resolved before budgeting or compaction. A selection reply carries the
stable `clarificationId`, selected `optionIds`, and optional `otherText`. qrenderdoc preserves the
snapshot that produced the question and freezes a new current snapshot when the answer is sent. The
Host compares the two content-addressed candidates, chooses origin or current from the user's intent,
and asks a single-choice follow-up when that intent is ambiguous. Candidate snapshots never become
conversation prose; only the chosen snapshot reaches tool scope and model context.

Before a provider call, `ContextManager` resolves `ModelContextSpec` for the currently selected model
without guessing a generic window:

1. a verified profile `context_window_tokens`, when configured;
2. exact installed `genai-prices` metadata for the selected model;
3. otherwise a configuration error requiring verified `context_window_tokens` for the unknown or
   self-hosted model.

ReplayLab first reserves output, system-instruction space, tool-schema space, and a safety margin.
The default policy triggers when usable input reaches 100% and compacts it to about 25%. It never
summarizes the current request, selected frozen capture identity, Goal constraints, Skill contract,
or evidence references. Older turns are converted into bounded `MemoryFact` records and a
`ContextSummary`; recent turns remain verbatim and therefore have the strongest short-term weight.
Each fact carries source lineage, category, importance, creation/confirmation time, general or
capture scope, expiry and `active`/`superseded`/`expired` state. A newer correction with the same
semantic key explicitly supersedes the older fact, and capture-scoped facts do not silently survive
an incompatible capture session. Ranking retains active, important and recently confirmed facts
first rather than merely truncating the oldest text. Each summary also carries a source digest and
`throughMessageId` and is stored under the hashed conversation id for 30 days. The next one-shot
AgentHost process removes only the proven summarized prefix. If model summarization fails, a bounded
deterministic summary is used; if the immutable payload itself cannot fit, the request fails
explicitly rather than being silently cut.

Official OpenAI Responses and Anthropic API profiles on their exact vendor hosts additionally use
Pydantic AI's native automatic compaction inside longer tool loops. Other model APIs receive a
per-specialist `ProcessHistory`
controller. Before every model request—including requests after an internal tool returns—it treats each complete
tool-call/tool-return exchange as an atomic block, pins unknown/native message shapes, retains the
newest live exchange, and compacts older exchanges to a host-labelled memory record. Tool facts in
that record are admitted only when they match the specialist's host-owned call-id/name and
argument/result SHA-256 ledger. The host `ProcessHistory` generation is written into the released
specialist receipt (provider-native compaction remains provider-observed and reports generation zero).
If the immutable request plus newest atomic exchange still cannot fit, the child fails
explicitly and the main supervisor receives that failure receipt. External Codex/ACP agents may also
compact internally, but their timeline event is observability only and never substitutes for
ReplayLab's host-side budget.

The semantic workflow terminal is carried separately from the append-only TaskEvent transport kind:
`completed`, `needs-user-input`, or `blocked`. qrenderdoc preserves that state in both TaskEvent and
CLI-fallback paths, so a clarification question or partial blocked report is not labelled as a
success. CLI JSON exposes a bounded lifecycle/evidence receipt and deliberately excludes specialist
answers and model histories.

## Agent, Skill, ToolService, MCP, and acceptance

- The **Agent runtime** is executable orchestration code: model/Agent adapter, planner, state,
  budgets, tool loop, cancellation, scheduling, workflow effects, and stopping rules. It is not a file
  format that every model automatically recognizes.
- A **Skill** is a versioned domain procedure loaded for one task. Its frontmatter declares category,
  candidate workflow inventory, read-only/effectful shape, minimum successful calls, and required
  evidence tools; its body describes the analysis procedure. The inventory is routing input, not a
  permanent per-command authorization wall.
- **InternalToolService** is the process-shared, serialized capability boundary for internal
  model-selected RenderDoc reads. It owns the exact instance/capture scope, typed schemas, audit
  ledger, cancellation containment, and direct high-level bridge calls. The Agent never receives an
  `ICaptureContext` pointer or unrestricted replay/file operations.
- **ReplayLabMCP** provides the same first-party bridge contracts to ACP and explicit external
  clients. Every Agent-facing session is filtered to the exact task inventory and rechecks the live
  manifest before dispatch; model API and Codex internal turns do not start it.
- The **CapabilityMatrix** is descriptive, not a static permission list. `ready` and `conditional`
  routes are attemptable; unprobed model support and conditional sidecar availability do not become
  self-reported permission failures. Only verified Host facts—for example an explicitly tool-less
  model profile, no loaded capture, no pinned qrenderdoc instance, or no usable tool transport—are
  hard blockers for a capture-dependent specialist; they do not block an unrelated direct answer.
- The **WorkflowGuard** constrains semantic effects (`workspace`, `project`, `ui`, `replay`) and
  observable outcomes rather than permanently enumerating every allowed call. It requires ordering,
  machine-verifiable receipts, postconditions and recovery: for example validate the same Shader
  workspace before applying it, bind a UI proposal to its classification ID, and roll back when a
  replay mutation cannot be verified.
- Effects are classified per invocation when arguments change behavior. The current
  `classify_current_frame_passes` implementation always carries `project` because it stages the
  capture catalog, and adds `ui` only for `publish_to_ui=true`;
  `apply_current_frame_pass_ai_proposals` derives `project`/`ui` from its explicit persistence and
  publication flags. Neither call can bypass the guard by being labelled globally read-only.
- The **acceptance validator** is deterministic host code. A fluent answer cannot substitute for
  required successful tools, matching EID/resource/pixel/shader identity, or a valid terminal state.

The current catalog intentionally has only two composable Skills:

- **Shader Workbench** binds exact Shader identity and evidence, reconstructs clearly labelled
  readable HLSL, explains traced algorithms, manages generated source in a workspace/project library,
  compiles and validates ABI/interface consistency, and can apply a requested replay replacement only
  with a verified postcondition and usable rollback record. Generated HLSL is never called original
  source without provenance evidence.
- **Event/Pipeline** analyzes one EID, a pass, a range, or a frame; traces draw/dispatch inputs and
  outputs, builds evidence-linked flow diagrams, and can publish a foldable semantic pass view. It may
  request a bounded Shader specialist result when Shader behavior is material. The current UI tool
  does not rewrite native Event Browser markers or captured names, and documentation must not claim
  otherwise.

## Multi-Agent extension point

The adaptive default is closest to the “manager with specialists as tools” pattern: qrenderdoc keeps
ownership of the user turn and the final answer, while specialists perform bounded work. Independent,
read-only tasks from different Skill categories may run in parallel; dependencies, repeated
categories, or effectful work remain sequential. `bounded-supervisor-specialist-reviewer/v1` remains
the compatibility strategy for a model that returns only the old `TaskPlan` contract.

The `OrchestrationStrategy` interface deliberately separates safe scheduling/review policy from the
provider adapters. Future strategies can add dedicated model-backed planner, shader, performance,
or reviewer Agents, different models and context budgets, or a graph executor. They must still
return the same `SkillResult` records and pass the deterministic evidence gate. Vendor ambient
subagents stay disabled unless a future adapter can prove equivalent tool scoping, accounting,
cancellation, and evidence attribution.

The interface now also returns a provider-neutral `SpecialistDescriptor` for each Skill. It defines
an isolated-per-task role, dedicated system instructions, advisory model/reasoning hints, explicit
input/output fields, and acceptance criteria. Hints never silently replace the model selected in the
UI. Both the direct model-API adapter and Codex/ACP adapters render the same descriptor into a fresh
specialist context; neither adapter can change the Skill identity or bypass its internal-tool
evidence gate.

Both `renderdoc-shader-workbench` and `renderdoc-explain-event` have dedicated descriptors. Their
staged flows keep captured evidence, semantic inference, and generated artifacts as separate claim
classes. Availability of an effectful step is discovered at runtime; completion is admitted by its
workflow preconditions, receipts, postconditions and rollback evidence, not by a blanket read-only
gate or by the model asserting that it has permission.

Use a true subagent only when at least one boundary is genuinely different: another model,
independent context, distinct tools/permissions, a separate cost budget, or transactional approval.
Otherwise a Skill is the smaller and more testable abstraction.

This corresponds to a custom/dedicated subagent role in products such as Codex, but ReplayLab owns the
role contract instead of asking a provider's ambient coordinator to invent it. Codex's built-in
multi-agent implementation can coordinate concurrent workers; a configured custom subagent adds a
repeatable role/model/instruction profile. ReplayLab's Shader Specialist uses the same useful idea—an
isolated role with explicit inputs and acceptance—but stays portable across API, Codex app-server,
and ACP. A future adapter may map a descriptor onto a native provider subagent only after it can
preserve tool scope, cancellation, cost attribution, and host-side evidence validation.

References: [Codex app-server](https://developers.openai.com/codex/app-server/),
[LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview),
[Pydantic AI multi-agent applications](https://ai.pydantic.dev/multi-agent-applications/), and
[OpenAI Agents SDK orchestration patterns](https://openai.github.io/openai-agents-python/multi_agent/).
