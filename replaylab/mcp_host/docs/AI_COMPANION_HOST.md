# External AI Companion Host

`renderdoc-ai-companion` is a Python 3.10+ sidecar for RenderDocUltra. It owns
the OpenAI Responses connection, a private stdio MCP client, chat history and
the RenderDoc UI-intent worker. Neither the API key nor the OpenAI SDK enters
qrenderdoc, the Python 3.6 bridge, or the MCP child process.

The companion is the caller of MCP functions. In the normal UI flow the user
does not call `apply_shader_workspace_candidate*` or other raw functions.

## Model and account support

The standalone executable currently has one model backend: the official OpenAI
Python SDK and Responses API.

| Entry point | Supported now | Authentication / limitation |
|---|---:|---|
| `renderdoc-ai-companion` | Yes | OpenAI Platform `OPENAI_API_KEY`; usage is billed to that API project |
| ChatGPT desktop / Codex CLI / Codex IDE with this local MCP | Yes, as an alternative UI | Sign in with ChatGPT for subscription access, then configure this package as the local `replaylab` STDIO MCP server |
| ChatGPT/Codex subscription inside `renderdoc-ai-companion` | No | The sidecar does not implement Codex login or read cached Codex credentials |
| Other AI APIs or an OpenAI-compatible base URL | No | `--model` changes only the OpenAI API model name; it is not a provider switch |

Do not copy `~/.codex/auth.json`, browser cookies, or a Codex access token into
the sidecar. OpenAI documents ChatGPT sign-in as a supported authentication path
for its own ChatGPT desktop, Codex CLI and IDE clients, while general Responses
API applications authenticate with an API key. See the official
[Codex authentication](https://learn.chatgpt.com/docs/auth) and
[OpenAI API quickstart](https://platform.openai.com/docs/quickstart/make-your-first-api-request).
Codex documents local STDIO MCP support and shared desktop/CLI/IDE configuration
in [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

If you already have a ChatGPT/Codex subscription, you can use those supported
clients directly with the local MCP server. They can answer user-initiated
RenderDoc questions, but they do not replace this sidecar's always-on intent
poller: a RenderDoc button remains queued until an agent turn claims it. A
future Codex app-server adapter could add subscription-backed deep integration
without taking ownership of Codex credentials; it is not implemented here.
Codex documents app-server as the protocol for product integrations with
authentication, conversations, approvals and streamed events:
[Codex app-server](https://learn.chatgpt.com/docs/app-server).

### Subscription-backed alternate client

To use a ChatGPT/Codex subscription today, sign in through a supported Codex
client (`codex login` for the CLI) and configure the already-installed MCP
instead of starting `renderdoc-ai-companion`. For example in
`~/.codex/config.toml` on this Windows checkout:

```toml
[mcp_servers.replaylab]
command = '<REPOSITORY>\replaylab\mcp_host\.venv\Scripts\python.exe'
args = ["-m", "src.server"]
cwd = '<REPOSITORY>\replaylab\mcp_host'
startup_timeout_sec = 30
tool_timeout_sec = 300
default_tools_approval_mode = "prompt"

[mcp_servers.replaylab.env]
RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG = 'D:\RenderDocUltra\shader-tools.v1.json'
RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG = 'D:\RenderDocUltra\shader-toolchains.v1.json'
```

Enable/review this repository's `renderdoc-Skill` so capture-dependent turns
start with `get_current_renderdoc_context`. This route uses the Codex client's
own approvals and agent loop, not Companion Core's narrower chat allow-list,
programmatic per-turn context injection, or background intent worker. Prefer a
single replay-mutation owner and keep write tools approval-gated. Official
STDIO MCP configuration is documented at
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

## Install and start

Build qrenderdoc from this repository first. The build packages the canonical
bridge source from `replaylab/renderdoc_mcp_bridge` into the fixed first-party
application path
`<appdir>/integrations/replaylab/python/renderdoc_mcp_bridge`. qrenderdoc loads
that package directly; it is not discovered, enabled or overridden as a user
extension. The adjacent `integration.json` records the built-in package version
and `start`/`stop` entry points, but is not a discovery manifest.

For a local Development tree, the repository's explicit deployment/verification
entry point is:

```powershell
.\util\i18n\deploy_replaylab_integration.ps1 -Configuration Development
```

Restart the freshly built RenderDocUltra, load a capture, and confirm the
ReplayLab UI can report the built-in bridge status. The bridge and companion
must run as the same OS user so both resolve the same
`%LOCALAPPDATA%\ReplayLab\ipc-v2\instances` directory.

Then install and start the sidecar:

```powershell
Set-Location .\replaylab\mcp_host
py -3.10 -m venv .venv   # or another installed system Python >= 3.10
.\.venv\Scripts\python.exe -m pip install -e '.[companion]'

New-Item -ItemType Directory -Force D:\RenderDocUltraWorkspaces | Out-Null
$env:OPENAI_API_KEY = '<set only in this companion shell>'
$env:RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG = 'D:\RenderDocUltra\shader-tools.v1.json'
$env:RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG = 'D:\RenderDocUltra\shader-toolchains.v1.json'

.\.venv\Scripts\renderdoc-ai-companion.exe gui `
  --workspace-root D:\RenderDocUltraWorkspaces
```

Use `console` instead of `gui` for a terminal REPL. The default MCP command is
the current virtual environment's Python running `-m src.server`, so the shown
full-path launch works even when the venv has not been activated. Do not start a
second MCP server or configure a separate MCP client for this mode: the
companion owns a private STDIO child. Override it only as a fixed process-owner
setting with `--mcp-command`/`--mcp-arg`. `OPENAI_API_KEY` is removed from the
MCP and editor child environments. Startup fails with a clear error when the
key or optional OpenAI SDK is absent.

The trusted tool and route catalogs must be generated first as described in
[Shader toolchain routing](SHADER_TOOLCHAINS.md). When the two legacy
`--decompiler-tool-id`/`--compiler-tool-id` options are omitted, the Host
physically detects the captured shader and binds the unique complete,
hash-pinned route chain. This supports multi-hop routes; the companion never
guesses a first/last tool from a route. A missing or ambiguous route fails
closed for executable editing, then falls back to a non-executable current-event
representation so the user receives RenderDoc disassembly or hash-bound binary
metadata instead of a blank editor.

## Optional multi-capture project mode

Project mode is an explicit process-owner option. It imports a bounded symbol
catalog whenever `captureSessionId` changes and gives chat/Reverse Edit the
fresh hash-based reuse plan. A new project also requires an explicit game and
build boundary; the safe `--project` value is only its directory name:

```powershell
New-Item -ItemType Directory -Force D:\RenderDocUltraProjects | Out-Null

.\.venv\Scripts\renderdoc-ai-companion.exe gui `
  --workspace-root D:\RenderDocUltraWorkspaces `
  --project-root D:\RenderDocUltraProjects `
  --project MyGame-build-2026.08-d3d11 `
  --project-game-id MyGame `
  --project-build-id 2026.08 `
  --project-platform windows-x64 `
  --project-api-family d3d11
```

For an existing project, `--project-game-id` and `--project-build-id` may be
omitted; the Host reads and validates the scope already stored in that project.
If supplied, they must match. For a new project, API family defaults to the API
reported by the first capture when `--project-api-family` is omitted. A later
capture with a conflicting API fails closed. Use a distinct project directory
for a different game/build/platform/API boundary.

The Companion automatically performs only these cross-capture actions:

1. export and import the current capture's physically verified shader catalog;
2. request a fresh reuse plan for the exact capture ID;
3. expose alias/material records as candidates and inject only bounded prior
   analysis whose project path, SHA-256 and explanatory-only policy verify;
4. when Reverse/Edit has created a fresh, unmodified workspace and exactly one
   eligible source matches, compile/ABI-validate the decompiler baseline, seed
   that source, then compile and ABI-validate it again.

Alias and material entries remain candidate-only because the current project
API has no safe automatic live-binding operation for them. Prior analysis is
explanatory evidence, not physical shader evidence. Old workspace manifests,
ABI reports, material sessions, replay handles and Apply authorization are
never restored. Project import or source seeding never applies a shader;
`/apply-last` or the separate process-owner auto-apply option is still required
after all current-capture gates pass. Each workspace records project seeding in
`project-source-seed.json` or `project-source-seed-failure.json`.

## Normal workflow

1. Open one capture in RenderDocUltra and start the companion.
2. Ask ordinary questions in Chinese or English. Before every user turn the
   companion programmatically calls `get_current_renderdoc_context`; the model
   cannot omit or forge this step.
3. In RenderDoc, select an event and use **RenderDoc AI > Reverse/Edit with AI
   > stage**. The companion claims the immutable intent in the background,
   renews its lease during long work, verifies capture session plus
   ResourceId/stage/entry point/raw SHA-256, and prepares one shared shader
   workspace.
4. Check the reported representation lane. For `executable-editable`, review
   `source/shader.hlsl` and `ai/analysis.md` in the configured editor or any
   IDE. If preparation failed or produced no executable source, the Companion
   automatically calls `get_current_shader_representation`, reports the
   `explanatory-ir`/`binary-only` artifact path to the AI and UI, and may persist
   the AI's named pseudo-functions/control-flow/data-flow commentary as
   `ai/explanation-analysis.md`. This is explanatory Markdown/pseudocode, not
   compilable HLSL.
5. Only in the `executable-editable` lane, `--watch-saved-shaders` turns a stable
   IDE save into a new candidate without mutating replay. Explanation lanes do
   not start a watcher and clear the Companion's `/apply-last` target.
6. For an executable workspace, type `/apply-last` to explicitly authorize fresh ABI/live validation and a
   shared-resource apply. The companion calls validation and apply; RenderDoc
   replaces the original captured shader ResourceId with one generated target,
   so every use (including draws outside a previously enumerated event list)
   sees the same replacement. The captured original shader is retained. Type
   `/rollback-last` to pass the exact opaque replacement ID returned by that
   apply to shared-resource rollback and restore the original; repeat it to
   unwind earlier active Shader replacements. A graceful Companion shutdown
   also rolls back all tracked replacements in reverse order. The model never
   creates those IDs. Event-family overrides remain available only for advanced
   selective debugging.

For unattended edit iteration, the process owner may instead add
`--auto-apply-compatible-saves`. This implies save watching and is a separate
authorization from the RenderDoc Reverse/Edit button. Every save still has to
compile, pass a fresh complete ABI comparison, and pass the live
capture/event/original-byte gates. Failure keeps the previously active result
and is reported; a Reverse/Edit click alone never authorizes replay mutation.
Rapid consecutive saves converge before mutation: the Companion waits until
the watcher reports its newest observed source as compiled, then binds both the
exact candidate revision and raw SHA-256 into validation and shared-resource
apply. A newer revision published during either gate causes a fail-closed
rejection and is considered on the next monitor poll. Atomic IDE replace saves
are debounced as complete content, not compiled from a temporary partial file.
Changing or closing the capture stops that workspace watcher and never carries
its candidate, ABI report, replacement handle, or apply authorization into the
new capture session.

An absolute editor path can be supplied with `--editor-executable`. The
companion opens only the fixed `source/shader.hlsl` and review file in its own
workspace; the model cannot choose a path or executable. Explanation-only
fallback artifacts are reported but are not launched as editable Shader source.

## AI source organization paths

The standalone `renderdoc-ai-companion` CLI enables HLSL organization by
default: `--organize-source` is a Boolean optional argument whose CLI default is
true. Passing `--organize-source` explicitly therefore does not opt in to a
normally disabled feature; use `--no-organize-source` to disable source
rewriting and keep the Reverse/Edit model turn analysis-only. Programmatic
callers that construct `CompanionConfig` directly retain the library default of
`organize_source=False` unless they set it. In either case the organizer runs
only for an `executable-editable` HLSL source; assembly and explanatory lanes
are reviewed but are not rewritten as HLSL.

The `renderdoc-ai-organize/v6` readability contract formats executable
algorithm bodies to a maximum of 120 columns, including leading indentation.
A logical statement that fits after whitespace normalization remains on one
physical line; a longer statement may wrap only at syntax-safe boundaries while
preserving explicit parentheses, operator association, evaluation order and
side-effect order. The organizer may not use algebraic reassociation,
commutation or distribution to shorten floating-point code. Long and core
functions contain concise Chinese comments at their actual calculation phases
to explain important dataflow, formulas, input meaning and intermediate/debug
values. A header-only function comment is insufficient, while trivial syntax
does not receive line-by-line commentary. These rules apply only to executable
algorithm bodies: resource declarations, register/space bindings, cbuffer
layout, pack offsets, types and all other ABI facts remain unchanged.

### Companion-managed automatic organizer

The Reverse/Edit worker owns one fixed transaction:

1. preserve `ai/decompiler-original.hlsl` byte-for-byte and bind its SHA-256;
2. compile and freshly replay-ABI-validate the untouched reconstructed HLSL;
3. request strict structured output containing `organizedSource`, `analysis`
   and `flowchartMermaid`;
4. reject empty/NUL/oversized output, Markdown-wrapped HLSL, unsafe Mermaid,
   external `#include`/`#line`, filesystem paths and tool/shell instructions;
5. verify that `source/shader.hlsl` still has the preserved baseline hash, then
   temporarily write the organized HLSL;
6. compile it and run a fresh complete replay ABI validation;
7. compare the organized executable `codeSha256` with the executable hash from
   the untouched reconstructed-HLSL compile;
8. promote the organized text as `source/shader.hlsl` only when the hashes are
   exactly equal. Otherwise retain it as provisional explanatory HLSL, restore
   the untouched source, and recompile/revalidate that baseline.

Successful code-identical runs retain original/organized source, analysis,
safe Mermaid, a unified `organized.patch`, model response, compile and ABI
reports, the fixed policy version/hash, `ai/organize-code-gate.json` and
`ai/provenance.json`. A code mismatch or any exception preserves diagnostics
and the proposed source where possible, restores the baseline, and verifies
that the same compile route reproduces the baseline executable hash. Recovery
failure is reported rather than hidden.

### MCP-managed proposal session

ChatGPT, Codex or another MCP client can use the same policy without the
Companion's automatic model call:

1. call `begin_shader_workspace_organization(workspace_path, event_id)`;
2. use only the returned fixed files below
   `ai/organization-sessions/<organizationId>/`: `organized-proposal.hlsl`,
   `analysis.md` and `flowchart.mmd`. The AI must not edit the baseline source,
   session/provenance JSON or semantic-context file;
3. call `review_shader_workspace_organization(workspace_path,
   organization_id)` after those three files are complete;
4. the Host binds the proposal to its bytes, temporarily installs it, compiles
   it, performs fresh replay ABI validation, and requires exact executable
   `codeSha256` equality with the baseline compiled during `begin`;
5. `review` promotes the proposal into `source/shader.hlsl` only on that exact
   gate. There is no separate promotion or replay-apply call. A mismatch remains
   provisional and the baseline is restored and freshly validated; exceptions
   follow the same recovery path, while a concurrent source edit fails closed
   instead of being overwritten.

`rawSha256` is deliberately not the organize-only identity gate. Identifier
renames can change reflection/resource-name strings and therefore the complete
DXIL container hash while leaving its executable chunk unchanged.
`codeSha256` equality proves only that the fixed compiler emitted the same
executable code for the organized proposal and the reconstructed HLSL baseline.
It does **not** prove that the reconstructed baseline is semantically identical
to the captured original shader, nor recover original names, comments, macros,
function boundaries or behavior for every possible input. Both paths record
`semanticEquivalenceVerified=false`.

Source promotion is not RenderDoc mutation. Both organization paths finish with
`applied=false` and never call a Shader apply function. `/apply-last` remains a
separate explicit authorization after current-capture gates pass. The
process-owner `--auto-apply-compatible-saves` option is also separate and must
never be inferred or enabled by organization success.

## Buttons and authorization

The background worker supports the current immutable UI intents:

- Explain Current Event: bounded reads only;
- Inspect Picked Pixel: bounded pixel-history/object evidence only;
- Reverse/Edit with AI: export, fixed workspace creation, trusted
  decompilation, AI review/organization, optional editor launch, and optional
  watch-and-compile only.

The worker requires the exact `allowedOperations` set for each intent, rejects
unknown/missing operations, renews a 5–300 second lease, and stops further work
if renewal fails. Capture-bound calls re-read the session before and after the
operation.

Ordinary chat exposes only an allow-list of bounded read tools. Raw bulk
buffer/texture downloads, directory enumeration and file-writing analysis are
not default tools. `validate_shader_workspace_candidate` is effectful because
it writes ABI/audit state. Advanced one-turn effects require exact
`/allow tool[,tool] | request` authorization, are confined to the configured
workspace root where applicable, and shader apply additionally requires a
successful exact validation in that same turn. `/apply-last` is the preferred
user-facing apply confirmation.

MCP output is bounded per call and cumulatively per turn. Oversized output is
replaced by a structured truncation marker with SHA-256 and a preview. Active
and cross-turn histories also have hard limits.

## Current boundaries

- The included UI is an external tkinter window/console, not a native
  qrenderdoc dock.
- No VS/Rider plugin or clickable CFG/DFG view is included; any editor can edit
  the fixed workspace file.
- Default shared-resource Shader apply supports local D3D11/native DXBC and
  D3D12/native DXBC or DXIL. D3D12 uses a checked transaction that rebuilds
  every affected graphics/compute PSO with its captured root signature and
  commits no mapping if one fails. Event/family Shader overrides remain
  D3D11-only. Shadow-CBuffer/material sessions separately support local D3D11
  and bounded local D3D12 Root/descriptor-table CBVs; root-signature changes,
  root constants, ray state objects, indirect Root-CBV mutation, SPIR-V/Vulkan,
  OpenGL and remote replay do not gain an apply path from external round-trip support.
- A decompiler route is deterministic and provenance-bound, not a guarantee
  that every optimized binary has a correct high-level reconstruction.
- Every representation has an explicit lane. `explanatory-ir` and
  `binary-only` always remain compile/watch/apply-ineligible; a user may review
  their pseudo-code, but the Companion never silently promotes it into the
  executable lane.
- There is no custom user CBuffer ABI exception or native material slider panel
  in this companion.
- There is no model-provider abstraction yet. Anthropic, Gemini, local models
  and generic OpenAI-compatible endpoints require explicit adapters that
  preserve the same bounded tool loop, structured-output validation and
  mutation policy; changing `--model` alone cannot enable them.

## Recommended dedicated-agent shape

Keep the tested Companion Core as the authority boundary and add user surfaces
around it:

1. qrenderdoc remains a deterministic context/intent producer and replay
   executor; it never receives a model SDK or credential.
2. One local Companion Core owns MCP, fresh-context injection, intent leases,
   tool policy, workspaces, validation, apply/rollback and audit state.
3. Model backends are adapters outside qrenderdoc: Responses API first, then an
   independently tested Codex app-server adapter or other providers.
4. The current tkinter/console UI remains the fallback. A later qrenderdoc dock
   should be a thin localhost IPC client showing chat, active context, intent
   progress and explicit apply/rollback controls. It must not run model calls,
   compilers or replay work on the Qt UI thread.

A native dock is worthwhile for the final interactive product because it makes
the selected event/stage/pixel and queued-button result visible in one place.
It is not the right policy or execution host: embedding the current Python 3.10
model stack in qrenderdoc would couple secrets, networking and long-running work
to RenderDoc's Python/Qt lifecycle. Stabilize the external Core protocol first,
then implement the dock as a replaceable view.

The Responses function-call loop follows the official protocol: all response
output items are retained, each function result is returned as a
`function_call_output` with the same `call_id`, and another response is
requested until text is produced. See the
[OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling).

Capture context, tool evidence and (for Reverse/Edit) reconstructed shader text
are sent to the configured OpenAI model. Run the sidecar only when that transfer
is permitted for the capture; qrenderdoc and the local MCP child still receive
no model credential.
