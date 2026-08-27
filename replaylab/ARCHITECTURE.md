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
Native qrenderdoc code owns lifecycle, availability, AI actions, and typed intent dispatch. The
Python bridge remains an in-process implementation module for capture-context freezing and bridge
protocol v2, while AgentHost and MCP Host remain hidden sidecars so network/provider dependencies do
not enter the qrenderdoc process.

This boundary keeps product-facing behavior native without discarding the tested bridge protocol:

```text
qrenderdoc native UI/lifecycle
  -> typed ReplayLab integration API
  -> fixed renderdoc_mcp_bridge package
  -> bridge protocol v2
  -> pinned AgentHost / scoped MCP Host sidecars
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
  -> zero-tool main supervisor -> GoalSpec + initial decision
       -> ask_user / blocked (zero specialist tasks), or first StagePlan
  -> host validates task IDs, dependencies, Skill names and budgets
  -> isolated Skill specialist session(s), with only that Skill's tools
  -> deterministic evidence validator
       -> retryable: feedback to the same logical specialist context
       -> accepted: seal bounded SkillResult and release detailed child context
       -> failed/blocked: after cleanup is confirmed, seal a released failure receipt;
          dependent tasks are blocked
  -> main supervisor reads admitted results plus every terminal specialist receipt
       -> dispatch_next_stage / finish / ask_user / blocked
  -> one qrenderdoc answer and terminal TaskEvent
```

For model APIs, ReplayLab owns both the planner and specialist model loops. For Codex/ACP, the vendor
Agent owns each inner model/tool loop, but ReplayLab still owns the outer TaskPlan, the selected
Skill, scoped MCP grant, scheduling, evidence admission, and final result set. Thus the two paths
share task division and acceptance criteria without claiming that different models will produce
identical prose.

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

Button actions skip the planner because the user already selected an exact workflow. For example,
**Explain Event** directly invokes `renderdoc-explain-event`; its result is accepted only after both
draw details and pipeline state were read successfully. This is faster and more reliable than
asking the chat model to infer what the button means.

## Context windows and compaction

qrenderdoc sends a versioned JSON conversation envelope with stable, non-translatable field and role
identifiers. It contains the current request separately from ordered history, plus a conversation id
and an omitted-history count. This removes the old translated-prefix and `USER:` delimiter ambiguity.
The 2 MiB wire ceiling remains a transport safety gate, not a model-window estimate.

Before a provider call, `ContextManager` resolves `ModelContextSpec` in this order:

1. verified profile `context_window_tokens` and output reserve;
2. the installed `genai-prices` model metadata;
3. a conservative configurable fallback for unknown/self-hosted models.

ReplayLab reserves output, system/tool-schema space and a safety margin, then uses a high/low-water
policy (75%/55% by default). It never summarizes the current request, frozen capture identity, Goal
constraints, Skill contract or evidence references. Older turns are chunked into a strict
`ContextSummary`; recent turns remain verbatim. Each new summary is merged with earlier pinned fields
instead of replacing them, carries a source digest and `throughMessageId`, and is stored under the
hashed conversation id for 30 days. The next one-shot AgentHost process removes only the proven
summarized prefix. If model summarization fails, a bounded deterministic summary is used; if the
immutable payload itself cannot fit, the request fails explicitly rather than being silently cut.

Official OpenAI Responses and Anthropic API profiles on their exact vendor hosts additionally use
Pydantic AI's native automatic compaction inside longer tool loops. Other model APIs receive a
per-specialist `ProcessHistory`
controller. Before every model request—including requests after MCP returns—it treats each complete
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

## Agent, Skill, MCP, and acceptance

- The **Agent runtime** is executable orchestration code: model/Agent adapter, planner, state,
  budgets, tool loop, cancellation, scheduling, permissions, and stopping rules. It is not a file
  format that every model automatically recognizes.
- A **Skill** is a versioned domain procedure loaded for one task. Its frontmatter declares category,
  tool allow-list, read-only status, minimum successful calls, and required evidence tools; its body
  describes the analysis procedure.
- **MCP** is the process and capability boundary for model-selected RenderDoc reads. Native qrenderdoc
  code still handles UI state and capture lifetime directly. The Agent never receives an
  `ICaptureContext` pointer or unrestricted replay/file operations.
- The **acceptance validator** is deterministic host code. A fluent answer cannot substitute for
  required successful tools, matching EID/resource/pixel/shader identity, or a valid terminal state.

Skills are organized by evidence domain rather than by individual EID: event/pipeline, pixel,
shader, material bindings, performance, resources, and approval-gated export. Creating an Agent per
EID would duplicate context and increase cost without adding a distinct policy or tool boundary.

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
specialist context; neither adapter can change the Skill identity or bypass its MCP evidence gate.

`renderdoc-shader-workbench` is the first dedicated descriptor. Its staged flow binds shader
identity, recovers reflection/ABI, traces disassembly and control/data flow, maps resources and stage
interfaces, optionally generates a labelled high-level reconstruction, and finishes with consistency,
confidence, and unknown-item checks. Captured evidence, semantic inference, and generated source are
three different claim classes. The current path is read-only: generated source is not claimed to be
original, compiled, round-tripped, or replay-validated. Compile/apply/rollback remains unavailable
until qrenderdoc supplies a short-lived native capability ticket bound to the exact shader and action.

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
