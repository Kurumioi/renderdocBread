# Event evidence and acceptance

The shared `replaylab-core` defines role-specific reporting, Host verdicts,
severity and correction limits. These criteria guide Event analysis; they do
not introduce another runtime ledger, phase machine or supervisor protocol.

## Scope and evidence

- Bind capture/session, EID or range, participating stages and relevant
  resources to the actual Host results. Do not follow later UI selection.
- Keep narrow questions narrow. For a broad frame/pass question, use a bounded
  composite inventory, then targeted reads for material gaps.
- Preserve explicit requests for GBuffer interpretation, RT/channel/depth
  views, rendered diagrams, foldable UI and durable project saving. Missing
  requested artifacts remain visible in the final report.
- Preserve chronological EID coverage, real workload, input/output/state,
  resource identities and truncation. Markers are context, not draw calls.
- Only observed contribution evidence establishes that an action changed an
  output. Bindings and a nonzero draw establish a candidate, not contribution.
- Only definite compatible resource handoffs become graph arrows. Unknown or
  overlapping/ambiguous ranges remain reported uncertainty.
- Shader meanings require shader evidence. Inspect representatives grouped
  by exact identity and materially different context; do not repeat the same
  physical shader question merely because its wording changed.

## Visual and UI outcomes

Use the contracts in `visual-evidence.md` for exact image subresources,
channels, depth ranges, GBuffer interpretation and Host-rendered diagrams.
Each artifact must match the requested EID/resource/view; one R-channel export
does not satisfy an additional G-channel request. A rendered graph is accepted
through its Host artifact receipt, not through model-authored Mermaid.

A requested foldable panel normally authorizes a session-bound UI publication:
`captureBindingMode=display-session`, `effectMode=display-only`,
`projectKnowledge.projectPrepared=false` and
`panel.publicationMode=display-only`.

Durable saving or cross-capture reuse is a separate project outcome. Require
`captureBindingMode=project-catalog`, `effectMode=project-persisted` and
`projectKnowledge.persisted=true`. A `project-staged` receipt proves
preparation only. A display-only classification cannot gain persistence by
attaching a later persistent proposal. Preserve the original classification
identity and the fixed pass tree/category order.

## Completion and bounded partial results

A completed result needs:

- exact requested scope and traceable primary Host observations;
- consistent participation states, workload counts and resource handoffs;
- integrated shader evidence for any claimed semantic interpretation;
- matching receipts for requested artifacts and authorized UI/project effects;
- visible truncation, skipped outputs and inference confidence; and
- no unresolved required outcome or uncertain mutation.

An evidence-backed unknown is a valid analysis finding; it is not permission
to omit an available, necessary read or to invent a missing result. Return a
useful partial result when only part of the request is supported. A missing
rendered artifact does not erase an otherwise sound event explanation.

Use the Host's `blocker`/`warning` feedback and remaining budget. Correct
only failed requirements and reuse accepted results. Do not reopen sessions,
rerun successful writes or invent continuation hashes to reset validation.
For a genuine user-owned choice, an API child returns the exact boundary and
evidence-backed options to the main Agent; native Agents use their supplied
question interface directly.
