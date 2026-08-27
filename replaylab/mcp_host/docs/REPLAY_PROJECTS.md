# Multi-capture replay projects

A replay project is a bounded portable index. Launcher-created projects use
their immutable workspace ID as a private project sharing boundary; lower-level
MCP-created projects may instead bind an explicit game build. It lets a later
capture discover reusable shader source, verified display aliases and selected
material values without reusing capture-local replay authority. Each project
also keeps a capture library whose RDC entries are references, not owned copies.

## qrenderdoc project home

An ordinary idle ReplayLab/qrenderdoc launch opens `ReplayLab Projects`. The
same home is available later from `File -> ReplayLab Projects…`. It supports
creating a project at a user-selected path, adding an existing portable project,
forgetting a local registry entry without deleting its directory, configuring
an unpacked-asset root, adding/removing RDC references without deleting the RDC,
and opening a frame from its bounded thumbnail list. Direct RDC/file-association launches and replay-host,
target-control, Python, remote, crash-handler, and other special startup modes
continue directly to their requested operation.

Launcher projects are bound automatically to their own workspace, so the New
Project dialog needs only a name, project path, optional unpacked-asset path and
initial RDC references. Capture catalogs, Shader interfaces/layouts and fresh
ABI validation remain the physical compatibility gates inside that project.
Legacy pending projects remain readable and can still be bound once from
Project Settings. The lower-level `create_replay_project` MCP operation still
requires an explicit game/build boundary.

All project-home host calls and capture association run on a bounded background
pool. The Qt UI is updated only on its own thread, and owner/generation guards
discard results after the dialog closes or a different capture is loaded.
Opening a project card activates that project before qrenderdoc opens the RDC.
Opening an RDC directly performs a metadata-only background association check
and changes the active project only if exactly one registered project contains
that normalized absolute path.

Capture-library additions record the canonical path, display name and stable
path-derived reference ID without reading the full RDC. Embedded thumbnails are
then read lazily from container metadata, so adding multi-gigabyte captures does
not wait for an unrelated full-file hash. Older version-2 content identities
remain readable but are no longer used for project association. Zero or
multiple path matches leave the current project alone.

The machine-local sealed project registry stores portable roots outside the
default projects home. Its first load migrates existing valid default projects
once; after that, it is authoritative, so forgetting a record does not make the
unchanged directory reappear. Project creation first constructs and validates a complete hidden staging tree,
then atomically publishes it and commits the active-project selection under the
same lock. A failed selection commit restores the prior selection and moves any
published tree to a hidden quarantine before cleanup, so it cannot appear as a
usable half-project. Settings and pinned active-project operations share the
same selection lock and preserve snapshot semantics.

## Safety model

- Launcher projects use a unique private workspace scope. Explicit lower-level
  projects still use `game_id` and `build_id`. Optional `platform` and
  `api_family` further narrow either boundary; the first imported catalog pins
  the API when omitted.
- A later catalog with a conflicting declared game/build/platform/API is
  rejected. Catalogs that do not carry game/build metadata remain bound to the
  explicit project scope supplied by the user; the capture path is never used
  to guess a build.
- Raw matches use physically read shader bytes. A code-hash match is accepted
  only when stage, entry point, complete interface hash and CBuffer layout hash
  also match.
- Old workspace manifests, compiled candidates, ABI reports, replay handles,
  replacement IDs, material snapshots and capture-session IDs are never reused.
- Seeding writes only `source/shader.hlsl` in a freshly validated, unedited
  destination workspace. It does not compile or apply anything. Normal compile,
  fresh replay reflection/ABI validation and live capture checks remain required.
- Alias and material entries are candidates. Material values require a new live
  material session and exact block-layout validation before use.
- `captureId` identifies a capture-path/API/shader-byte family. It is not a
  hash of the complete reflection and `uses` payload. Each normalized catalog
  payload therefore has its own `catalogSha256` revision. Workspaces and
  knowledge records bind both values; if a capture has multiple revisions and
  that provenance is missing, registration fails closed as ambiguous.
- If the source workspace contains fixed `ai/analysis.md`, it is copied as
  size-limited UTF-8, content-addressed explanatory text. The reuse plan labels
  it `physicalEvidence: false` and `applyAuthorization: false`; it can guide a
  review but cannot establish a register, offset, ABI fact or permission.

## MCP workflow

1. Create a project with
   `create_replay_project(project_path, game_id, build_id, name, platform, api_family)`.
2. Export the current frame's strict shader catalog with
   `export_capture_symbol_catalog`, then call
   `import_replay_project_capture(project_path, catalog_path)`.
3. After decompiling/editing a validated workspace, call
   `register_replay_project_workspace`. If available, register a verified alias
   export with `register_replay_project_alias_table`. Save only explicitly
   selected live parameters with `save_replay_project_material_knowledge`.
4. For another frame of the same build, export/import its catalog. Import returns
   the same result as `get_replay_project_reuse_plan`: matches grouped per live
   shader, with their exact match mode and remaining validation requirements.
5. Create and decompile a new workspace for that frame. For a chosen source
   candidate, call `seed_shader_workspace_from_replay_project`. Review the diff,
   compile, call `validate_shader_workspace_candidate`, and only then use an
   explicitly authorized apply tool.

In the Companion, status and reuse-plan reads are available by default inside
the configured workspace root. Every create/import/register/save/seed operation
is effectful and requires an exact same-turn `/allow` tool name. Shader apply
still requires its separate same-turn validation and authorization.

## Stored data and limits

The atomic `project.json` manifest indexes at most 256 captures, 65,536 shaders
and 4,096 catalog revisions or records of each knowledge kind. A newly seen
revision is normalized, copied into the project-owned immutable path
`captures/catalogs/<captureId>/revisions/<catalogSha256>/catalog.json`, and its
adjacent shader blobs are copied by content address. The external import path
is no longer authoritative after import. Re-importing a known historical
revision is idempotent and does not move the active/head revision backward.
Every load verifies each owned catalog and artifact hash, the active-revision
pointer, and its duplicated head fields.

HLSL seeds are limited to 16 MiB, analysis text to 1 MiB, and alias artifacts
to 64 MiB. Operations use fail-closed project/workspace lock files; a stale
lock is never guessed safe. Version-1 single-catalog projects remain readable;
their first later import migrates the legacy catalog into owned immutable
storage before adding another revision.

The project home can explicitly open a selected capture, and a directly opened
capture can activate one exact unique-path project match. It does not
create workspaces for every shader match, apply aliases to RenderDoc, create
material sessions, choose among multiple candidates, or authorize Shader Apply.
Those remain explicit program/AI/user orchestration steps.
