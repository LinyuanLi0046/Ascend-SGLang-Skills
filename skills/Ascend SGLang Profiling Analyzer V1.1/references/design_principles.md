# Design Principles

This file defines only the design principles behind the skill.
For stage order, read [pipeline.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/pipeline.md).
For report schema, read [output_contract.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/output_contract.md).
For self-check rules, read [analysis_checklists.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/analysis_checklists.md).

## P1. Stable evidence-preparation layer

The four preprocessing scripts remain unchanged and lossless:

- slice trace by window
- summarize sliced trace into trace summary + bundle
- slice kernel csv by the same window
- summarize sliced kernel csv into stream / top-kernel / bubble evidence

These scripts prepare evidence; they are not the final reasoning layer.
For every full analysis, run them in a fixed order on the full raw `trace_view.json` and `kernel_details.csv` discovered under `profiling_root_path/ASCEND_PROFILER_OUTPUT/`, using the user-provided ns window.

## P2. Code-path grounding first

When code and launch flags are available, reconstruct the runtime code path before attempting kernel mapping.
Profiling validates and localizes that path; it does not replace code-path reconstruction.

## P3. Local-pattern alignment, not name matching

Do not map code using a single kernel name alone.
The matching unit is a local execution pattern built from:

- neighboring kernels
- adjacent trace spans
- relative ordering
- phase or layer context
- tensor-shape and dtype transitions when recoverable
- buffer provenance from code when recoverable

Kernel names are weak hints only.
The target is the underlying dataflow and execution role.

If strict alignment fails, downgrade to `insufficient_evidence`.

## P4. Owner competition before assignment

For ambiguous local clusters, compare candidate owners instead of assigning the first plausible one.

Minimum comparison dimensions:

- phase match
- neighboring-pattern match
- tensor-shape and dtype match when recoverable
- buffer-provenance match
- code-path reachability

Prefer the owner that explains the whole local pattern.
If multiple owners remain plausible, split the cluster or downgrade confidence rather than collapsing everything into one coarse runtime bucket.

## P5. Generic clusters are dataflow problems first

When a local cluster is dominated by lightweight data movement or metadata construction, treat it as a dataflow-resolution problem first.
This includes indexing, slicing, gathering, scattering, copying, view changes, range construction, metadata materialization, and similar work even if operator names differ across backends.

For such generic clusters, owner priority is:

1. scheduler / overlap / future-map owners
2. speculative-preparation owners
3. model-forward owners

The presence of a later neighboring kernel with a clearer name is not enough reason to skip earlier runtime-control owners.

## P6. Runtime-control coverage is semantic

For speculative, overlap-heavy, or scheduler-driven runs, explicitly check semantic runtime subphases such as:

- future resolution
- metadata preparation
- verify handoff
- acceptance or sampling
- tree or cache update
- scheduler writeback

These labels are semantic rather than name-based.
They must be checked even when implementations use different symbol names or low-level operators.

If overlap, speculative scheduling, or future-buffer usage is present, `runtime_future_resolve` is mandatory and the final report must state whether it was observed, absent after checking, or only partially localizable.

## P7. Future-map evidence is first-class

Patterns such as the following are first-class ownership evidence for nearby generic runtime clusters:

- `future_indices`
- `buf[indices]`
- `index_select`
- `record_stream(...)`
- `resolve_future(...)`
- `store_to_map(...)`

They must be treated as semantic future-map evidence, not as incidental implementation details.

## P8. Table-first output

The main deliverable is a Markdown report whose core is a kernel-to-code mapping table.
Prefer:

- exact file
- exact function or code region
- exact line number when recoverable
- otherwise the narrowest honest line range

The primary mapping granularity is one output row per kernel row.
Grouped summaries are allowed only as clearly labeled secondary overviews.

## P8a. Row-complete mapping is mandatory

The primary mapping table is not only the core deliverable; it is also the completeness contract.

- every selected kernel row must remain visible in the final report
- runtime-control rows must be covered row by row rather than only summarized by phase
- compute rows must also remain separated row by row and must not be merged into one coarse explanation

If row-complete mapping is not achieved, the report must downgrade unresolved rows explicitly rather than compressing them away.

## P8b. Parallel context is part of the mapping

When double-stream or multi-stream execution exists, parallelism is part of the kernel-to-code mapping rather than optional commentary.

- keep per-row stream identity
- explicitly record concurrent rows or parallel groups when recoverable
- distinguish true device overlap from mere trace adjacency when recoverable

If a row belongs to a parallel neighborhood, the mapping is incomplete unless that parallel context is stated.

## P9. Repeated-layer folding is strict

If many model-forward layers are structurally identical, only the middle repeated layers may be folded.

Required safeguards:

- keep the first repetitive layer in full
- keep the last repetitive layer in full
- include the embedding-preface region in the first kept model-forward reference when it belongs to the modeled entry path
- explicitly state which middle layers were folded
- never fold non-repetitive regions
- never fold non-model-forward runtime or scheduler regions

When the task requires per-kernel localization or the user asks for detailed row-level mapping, compute rows in the primary mapping table must remain fully expanded and must not be folded.

When repeated model-forward structure exists and only the final repeated layer is needed for localization, it is acceptable to avoid expanding every repeated layer only if the last-layer candidate neighborhood is still rendered row by row with its nearby output-head anchors.

## P10. Missing evidence narrows claims

If launch context or code is missing:

- structure analysis may continue
- mapping confidence must drop
- strict line-level localization must be withheld

Missing evidence narrows claims; it does not stop all reasoning.

## P11. Light performance commentary only

Performance commentary is allowed only when it helps code alignment.
Typical valid examples are:

- a bubble separating two code stages
- a serialized region that explains an execution path
- a missing fused path that explains many tiny kernels
- a missing overlap that changes path interpretation

Avoid broad finding inventories, wide host-bound diagnosis, and full TPOT ranking.

## P12. Lossless evidence remains outside the report

Keep the generated sliced trace and sliced kernel csv as the lossless evidence layer.
The report cites them; it does not replace them.
