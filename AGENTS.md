# RenderDoc Ultra development rules

> **EID performance worktree:** When working in `renderdocUltra-eid-perf`, read [`WORKTREE_ISOLATION.md`](./WORKTREE_ISOLATION.md) before building, deploying, or running it. Keep that worktree's build and test output isolated. Explicitly requested source integration into the main branch must preserve the main checkout's existing work; these isolation rules do not prohibit that integration.

> **Read BOTH this file and [`CLAUDE.md`](./CLAUDE.md) before making changes — together they are one unified rule set.** OpenAI Codex and other agents read `AGENTS.md`; Claude Code reads `CLAUDE.md`; each points at the other so neither agent misses a rule. They are complementary, not duplicates: **`AGENTS.md`** (this file) is canonical for the UI/ReplayLab **localization (i18n) rules** below; **`CLAUDE.md`** is canonical for project structure, the build/release pipeline, upstream sync, and pitfalls. If a rule's home is the other file, follow it there and keep the two in sync.

## Tooling

- Prefer command-line, API, MCP, and background workflows. Avoid controlling desktop applications unless no reliable non-interactive path exists.

## UI localization (mandatory)

These rules apply to every new or modified qrenderdoc user interface. English source text remains the canonical source; Simplified Chinese is supplied by Qt TS/QM catalogs.

- Before changing qrenderdoc UI, read `util/i18n/README.md` and `qrenderdoc/Translations/glossary.zh_CN.yaml`.
- Every user-visible C++ string must use `tr()`, `QCoreApplication::translate()`, or the documented deferred-translation pattern. Qt Designer `.ui` strings must remain translatable. Never build a sentence from separately translated fragments.
- Do not hide source literals behind convenience wrappers such as `uiText("...")`; Qt `lupdate` cannot discover those calls. Use a directly recognized translation call, or register every deferred source with `QT_TRANSLATE_NOOP` and translate it later under the identical stable context.
- Backend/runtime data shown in the UI must carry a stable message or preset ID. Localize known messages in qrenderdoc through an explicitly extractable ID/source mapping and preserve unknown values as diagnostics; do not make raw backend English the normal label, hint, or button path.
- Non-user-facing protocol values, JSON keys, CSS/HTML implementation text, API identifiers, paths, resource names, and serialization values must not be translated. A new literal that intentionally bypasses extraction must have a specific `i18n-ignore` reason; do not expand `util/i18n/source_literals_baseline.json` to hide new findings.
- Compact graphics-pipeline nodes are identifiers and must always use their API abbreviations. For D3D use `IA`, `VS`, `HS`, `DS`, `GS`, `RS`, `PS`, `OM`, and `CS`; preserve the corresponding OpenGL/Vulkan abbreviations such as `VTX`, `TCS`, `TES`, `FS`, `FB`, `TS`, `MS`, and `AS`. Do not translate or expand these node labels. Full stage names may be translated in tabs, tooltips, accessible descriptions, help, and explanatory prose.
- Preserve established technical tokens such as `SRV`, `UAV`, `CBV`, `RTV`, `DSV`, API names, shader semantics, format names, and function names unless the surrounding prose explicitly explains them.
- Translate ambiguous graphics terms by context. In particular, `Normal` is “法线” for geometry/vector data, “常规” for a mode or setting, and “正常” only for status/health. Add a translator comment or disambiguation context when the English source alone is ambiguous.
- Preserve placeholders, rich-text structure, keyboard accelerators, line breaks with semantic meaning, and code/command text. Prefer complete, natural Chinese sentences over word-for-word substitution.
- When a UI feature is added or changed, update and review the catalog, then run the synchronization and quality gates below. New entries may not remain `unfinished` in a distributable build.

```powershell
$qtBin = "C:\Qt\5.15.2\msvc2019_64\bin"
./util/i18n/update_translations.ps1 -QtBin $qtBin
./util/i18n/update_translations.ps1 -QtBin $qtBin -Check
./util/i18n/build_translations.ps1 -QtBin $qtBin -RequireComplete
python ./util/i18n/lint_ts.py ./qrenderdoc/Translations/qrenderdoc_zh_CN.ts --glossary ./qrenderdoc/Translations/glossary.zh_CN.yaml --source-root . --source-baseline ./util/i18n/source_literals_baseline.json --require-complete
```

- On memory-constrained Windows machines, build qrenderdoc with `MSBuild ... /m:2`; do not run multiple full builds concurrently.

## ReplayLab built-in Python integration localization (mandatory)

- The canonical ReplayLab bridge source is `replaylab/renderdoc_mcp_bridge/` in this repository. It is packaged at `x64/<Configuration>/integrations/replaylab/python/renderdoc_mcp_bridge/` and loaded by qrenderdoc from that fixed first-party path. It must not be discovered through the Python extension scanner, shadowed from `%APPDATA%`, or given an `extension.json` manifest.
- Python-created user-visible text must call `QtCore.QCoreApplication.translate("ReplayLabProjects", "English source")` directly. Do not hide source literals behind a custom translation wrapper. Use positional placeholders such as `%1` and `%2`, then insert project names, paths, diagnostics, API values, and identifiers after translation. ReplayLab's Python extractor does not support Qt numerus, so `%n` is forbidden until that pipeline explicitly implements plural catalogs.
- ReplayLab reuses qrenderdoc's installed translator; do not install a second `QTranslator` or infer the active language from the raw `UILanguage` value.
- `qrenderdoc/Translations/ReplayLabProjects.cpp` is generated by `util/i18n/extract_replaylab_translations.py`. Never edit it by hand. Reviewed Simplified Chinese text is maintained in `util/i18n/replaylab_zh_CN.json` and synchronized into the main TS catalog.
- Put split project-UI modules under the canonical bridge as `project_*.py`; they are discovered recursively for extraction and packaged automatically. Project UI kept in `__init__.py` must remain inside the `replaylab-project-i18n-begin` / `replaylab-project-i18n-end` markers.
- New or changed ReplayLab project UI must pass `util/i18n/update_translations.ps1 -Check`; the AST gate rejects raw English or Chinese labels, buttons, titles, item/tab text, menus, tooltips, file-dialog text, status text, message-box text, indirect local constants, unsupported `%n`, and the wrong translation context.
- Preserve `ReplayLab`, `RenderDoc`, `RDC`, `CBuffer`, `EID`, `API`, `ABI`, paths, project names, file names, format names, and shader/API identifiers. Translate `Shader` as “着色器” in prose and keep compact graphics-stage abbreviations unchanged.
- A distributable change is not complete after `lrelease`: relink qrenderdoc with `/m:2` and an explicit `SolutionDir`, verify the canonical Python integration at its fixed application path, and restart qrenderdoc:

```powershell
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:Build /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
./util/i18n/deploy_replaylab_integration.ps1 -Configuration Development
```
