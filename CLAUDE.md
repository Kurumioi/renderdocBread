# RenderDoc Custom Fork — AI Reference

> **Read BOTH this file and [`AGENTS.md`](./AGENTS.md) before making changes — together they are one unified rule set.** Claude Code reads `CLAUDE.md`; OpenAI Codex and other agents read `AGENTS.md`; each points at the other so neither agent misses a rule. They are complementary, not duplicates: **`CLAUDE.md`** is canonical for project structure, the build/release pipeline, upstream sync, and pitfalls; **`AGENTS.md`** is canonical for the UI/Bread **localization (i18n) rules**. If a rule's home is the other file, follow it there and keep the two in sync.

This is a custom fork of [RenderDoc](https://github.com/baldurk/renderdoc) with feature-hiding modifications. The local `upstream` remote points to `crossous/renderdoc`; our `origin` is [Straw1997/renderdocBread](https://github.com/Straw1997/renderdocBread). Renaming the GitHub repository does not require renaming an existing checkout directory or recreating its GitHub Desktop entry.

The user-visible AI product brand is **Bread** / **Bread AI**. Legacy `ReplayLab` protocol, storage, executable and translation-context identifiers remain compatible; existing user project names and paths are preserved. The canonical branding rules are in [AGENTS.md](./AGENTS.md#product-name-and-compatibility).

Pytest scratch data must stay outside every source checkout. Do not pass a repository-relative or repository-contained `--basetemp`; normally omit it and let pytest use the operating-system temp directory. Named scratch paths belong below the absolute system temp directory and must be cleaned after the run.

## Persistent data compatibility

Changes to projects, captures, Shader workspaces, shared names, navigation or
debug snapshots must retain backward compatibility. Read an older record using
its original schema before completing newer fields from matching original
Shader bytes, physical layout, saved source and existing verified artifacts.
Missing new fields alone are not corruption and must not force a new project,
workspace, source rewrite or recompilation. Do not treat a legacy display or
interface hash as a newer physical identity proof.

Preserve existing names, paths, user edits, snapshot order/anchors and immutable
history. Path identity follows the path's platform: Windows drive/UNC spellings
are equivalent where appropriate, Linux paths remain case-sensitive, and remote
URIs retain their scheme. A changed path-key algorithm must still discover old
keys and upgrade them atomically without discarding the original record. Keep
ambiguous or damaged records for diagnosis rather than guessing a replacement.

Metadata reads and recovery must not depend on unrelated compiler executables.
Execution still checks the selected tool and actual current capture/Shader;
saved receipts do not authorize another capture session. Reuse exact current
evidence, and keep manual save/debug paths free of repeated AI organization
validation. Persistence changes need focused old-format and relevant platform
round-trip tests, including multiple RDCs sharing knowledge when applicable.

## Project Structure Overview

This fork renames/hides RenderDoc identifiers to avoid detection. Key customizations sit on top of the official codebase as separate commits (viewable via `git log HEAD --not upstream/v1.x`).

### Custom Output Names (vs official names)

| Official Name | Custom Name | Source Project |
|---|---|---|
| `qrenderdoc.exe` | `RenderDocBread.exe` | `qrenderdoc/qrenderdoc_local.vcxproj` |
| `renderdoccmd.exe` | `render_soco_doccmd.exe` | `renderdoccmd/renderdoccmd.vcxproj` |
| `renderdocshim32.dll` | `render_soco_docshim32.dll` | `renderdocshim/renderdocshim.vcxproj` |
| `renderdocshim64.dll` | `render_soco_docshim64.dll` | (same) |
| `renderdoc.dll` | `renderdoc.dll` (unchanged) | `renderdoc/renderdoc.vcxproj` |
| *(no equivalent)* | `system_load.dll` | `renderdoc/renderdoc_no_export.vcxproj` |
| `librenderdoc.so` | `libVIVO50_SystemLoad.so` | (cmake, see `renderdoc/common/globalconfig.h`) |

**`system_load.dll`** is the `renderdoc_no_export` project — a copy of renderdoc.dll built without COM exports, used for injection into target processes while hiding the renderdoc identity. Its vcxproj must be kept in sync with `renderdoc.vcxproj` when upstream adds new source files (this has caused link errors before, e.g. `controlflow.cpp` was missing after v1.43 rebase).

### Other Custom Modifications

- **Detours inline hooking** (`renderdoc/3rdparty/Detours/`) — replaces IAT hooking for DX11/DX12 (`acb33f563`)
- **EGLImage support on Android** — custom GL extension wrappers (`df84c23fc`)
- **UI customizations** — LiveCapture window style changes, Diagnostic_Log window
- **PE parsing for injection** — `renderdoc/os/win32/win32_pe_parse.cpp`
- **AGS suppression** — `renderdoc/driver/d3d12/d3d12_device_wrap.cpp`

---

## Release Build Guide

**Before every packaging/release task, read
[`replaylab/packaging/RELEASE_REQUIREMENTS.md`](./replaylab/packaging/RELEASE_REQUIREMENTS.md),
[`replaylab/packaging/README.md`](./replaylab/packaging/README.md), and
[`replaylab/dependencies/README.md`](./replaylab/dependencies/README.md).**
They define the standing edition/dependency policy, source and binary consistency,
isolated build routes, required checks and delivery evidence. Follow the checked-in
requirements without asking the user to repeat decisions recorded there. This section
is the release entry point; keep detailed requirements in `RELEASE_REQUIREMENTS.md`
and executable commands/options in the packaging README and scripts.

The Windows application and release archive are named **RenderDocBread**. Bread AI remains
its assistant brand. Existing ReplayLab protocol/storage identifiers, RenderDoc core DLL and
capture/injection helper executable names remain compatible.

Run `PACKAGE_RENDERDOCBREAD.cmd` at the repository root, or:

```powershell
./replaylab/packaging/Build-RenderDocBread.ps1
```

After an ordinary clone, run `./replaylab/packaging/Restore-BuildEnvironment.ps1`.
Sealed inputs are included as ordinary Git binary parts under
`replaylab/dependencies/archives`; the build assembles and verifies them offline
against `inputs.lock.json`. No Git LFS pull or Release dependency downloads are needed.
The sealed build inputs and
redistribution inventory are in `replaylab/dependencies`; `Restore-BuildEnvironment.ps1`
creates the offline locked build environment without a pre-existing Development install.
The build keeps the running application's deployed files untouched, builds the GUI in
`output/RenderDocBread-native`, freezes the AI/MCP sidecars, publishes self-contained UEX,
and assembles a private temporary staging directory. It validates translations, runtime
hashes, pinned Shader compilers/language server, complete skill folders, relative paths,
VC runtime DLLs and relocated executable startup before writing
the standard `dist/RenderDocBread-<version>-win-x64.zip` plus SHA-256. It never stops live processes.
Read `replaylab/packaging/README.md` for dependencies, overrides and verification limits.
The package configuration is `replaylab/packaging/RenderDocBread.package.json`.

No credentials, profiles, conversations, capture data, user projects, build objects or
caches are release inputs. The source application is read-only; package assembly must not
run deployment scripts against it. Normal local deployment still requires closed sessions.
Use `/m:2` and an explicit `SolutionDir` for the GUI; do not run concurrent native builds.

---

## Branch and release completion

For completed implementation/release work, committing and pushing a task branch
is an intermediate step. The user's standing instruction is to merge the work
into the GitHub default branch and then delete that task branch, without another
confirmation. An explicit request to keep a branch, prepare only a draft, or defer
merging takes precedence. Discussion or read-only inspection alone does not
authorize a merge or a release.

1. Verify `origin` and query its GitHub repository's `default_branch`; this project
   uses `Straw1997/renderdocBread` and currently `v1.x`. Do not assume `main` or
   `master`, or use the `upstream` fork as the publishing destination. Pass
   `--repo Straw1997/renderdocBread` to every repository-scoped `gh` command;
   `gh api` paths must name the same verified repository. Fetch `origin` and tags,
   inspect current releases before choosing a version, and base new task branches
   on the current default branch unless the user selected another baseline.
2. Preserve unrelated user work. Use an isolated worktree when the original
   checkout is dirty. Commit only the task's intended changes; do not reset,
   auto-stash, force-push or include another task's uncommitted edits to finish
   delivery. Integrate intervening default-branch changes and resolve conflicts
   while retaining both intended behaviors. Recheck affected code after a repair.
3. Complete relevant validation, commit and push, then open a PR targeting the
   verified default branch. Wait for required checks and fix actionable failures;
   respect branch protection. Merge when the work and checks are ready. Prefer a
   merge commit so existing release/source commits stay in the default branch's
   history. Do not bypass checks with an administrator override.
4. For releases, build and validate a concrete candidate and use a draft while
   checks/uploads are pending. Before public publication, verify that the default
   branch contains the released implementation and that the tag/package identify
   the tested source. Rebuild affected artifacts if integration changes their
   inputs. Verify uploaded file sizes and SHA-256 before publishing. Never move
   an existing published tag or silently replace a published archive. If a release
   was already published before a documentation-only follow-up, merge that
   follow-up while preserving the release, its tag and its checked binaries.
5. After GitHub reports the PR merged, fetch the default branch and verify that
   the task head is included (normally `git merge-base --is-ancestor <task-head>
   origin/<default-branch>`). Only then delete this task's remote and local branch.
   Switch its clean worktree to the updated default branch or detach at that
   commit before deleting a checked-out branch. Keep deliverables and reports;
   preserve any worktree with user changes and every unrelated branch. Never use
   forced branch/worktree deletion to conceal unmerged or unsaved work.
6. Verify the remote default-branch commit, PR merged state, and absence of both
   branch refs. Report the PR/merge and release links, branch-cleanup outcome and
   any actual remaining blocker. A branch push, published release or queued merge
   alone does not complete the requested merge-and-cleanup workflow.

Release content/build rules remain in
[RELEASE_REQUIREMENTS.md](./replaylab/packaging/RELEASE_REQUIREMENTS.md).
Documentation-only workflow updates do not require a version bump or repackaging
an already verified release.

## Development update delivery

The user has authorized routine updates of the current Development application:
finish the change and checks, gracefully close the application, deploy into its
existing entry point, reopen it, and restore the capture/EID/RT selection. Do not
ask again just to perform those same deployment/restart steps. Preserve stored
projects, conversations and source files; save recoverable work first. The
authorization includes discarding transient annotation drafts when the old
version cannot save them, as explicitly confirmed on 2026-11-05. It does not
authorize discarding unsaved Shader/source edits or overwriting conflicting
user-authored files. Packaging remains isolated from a running application.

## Upstream Sync Guide

When syncing to a new upstream release:

1. `git fetch upstream --tags`
2. Create backup: `git branch backup-before-<version>`
3. Find custom commits: `git log --oneline HEAD --not upstream/v1.x`
4. Rebase: `git rebase --onto <new_tag> <old_merge_base> v1.x`
5. Resolve conflicts (common in: `core/core.h`, `.rc` binary files, `.vcxproj.filters`)
6. **Critical:** After rebase, diff `renderdoc.vcxproj` vs `renderdoc_no_export.vcxproj` to find any new source files that need to be added to `renderdoc_no_export`. Missing files cause link errors.

---

## Pitfalls & Troubleshooting

### Android cmake STL error (`gnustl_static include directory not found`)
MSYS2's cmake (4.x) has built-in Android platform modules that conflict with NDK r14b. **Fix:** always pass these two cmake flags:
```
-DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake"
-DCMAKE_ANDROID_STL_TYPE=c++_static
```

### Android cmake can't find `java`
MSYS2 bash doesn't inherit Windows user env vars by default. **Fix:** explicitly export at the top of the MSYS2 session:
```bash
export JAVA_HOME="/c/Program Files/Java/jdk8u422-b05"
export PATH="$JAVA_HOME/bin:$PATH"
```

### `renderdoc_no_export` link errors after upstream sync
Upstream added new .cpp/.h files to `renderdoc.vcxproj` that are missing from `renderdoc_no_export.vcxproj`. **Fix:** compare the two vcxproj files and add missing entries to both `.vcxproj` and `.vcxproj.filters`.

### Binary `.rc` file conflicts during rebase
The `.rc` files (e.g. `qrenderdoc.rc`, `renderdoccmd.rc`) may be UTF-16LE binary. Git can't merge them. **Fix:** take the upstream version and re-apply the name changes (e.g. `"RenderDoc"` → `"RenderSocoDocWOW"`) using python byte-level replacement or manual editing.

---

## Development replay profiling

Packaging and `build-dev.ps1` enable `/p:ReplayOptimizeDevelopment=true` by default
for interactive D3D11, D3D12, OpenGL and Vulkan replay. Direct MSBuild invocations
must pass this property explicitly; use `-DisableReplayOptimization` on the scripts
for an explicit `/Od` diagnostic control.
This optimizes each graphics driver project, including its precompiled header, while retaining
Development diagnostics and PDB symbols. The shared core keeps its normal flags;
the raw vcxproj default remains `/Od`. Always record this flag when comparing EID timing.
Use `/m:2` and an explicit `SolutionDir` as usual. Profiling and A/B controls are documented in
`util/perf/README.md`.

## Internationalization (i18n)

**The full i18n / localization rules live in [`AGENTS.md`](./AGENTS.md)** ("UI localization" and "Bread built-in Python integration localization") plus `util/i18n/README.md` and `qrenderdoc/Translations/glossary.zh_CN.yaml`. **Read them before adding or changing any user-facing string** — every UI string must be `tr()`-wrapped/registered and translated, or it renders in English (and `build_translations.ps1 -RequireComplete` fails).

One build-adjacent reminder that bites when adding AI providers: Bread provider-catalog text (`title` / `note` / `install_hint` / `subscription_instruction` from `replaylab/src/replaylab_agent/catalog.py`) is runtime JSON that `lupdate` cannot see, so each string must be registered with its **exact English source** in `providerCatalogTranslations[]` in `qrenderdoc/Windows/AI/AIAssistant.cpp`. Changing the Python English without updating that table's source silently falls back to English. Also prefer building UI result text from a stable status in C++ with `tr()` over displaying raw backend English (see AGENTS.md rule on backend/runtime data).
