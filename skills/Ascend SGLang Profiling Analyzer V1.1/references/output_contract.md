# Output Contract

This file defines only the output schema and output validity rules.
It also records the reusable preprocessing artifacts produced by the fixed preprocessing pipeline and kept as the machine-readable evidence layer.

## 1. Reusable preprocessing artifacts

These artifacts are produced from the full raw profiling inputs discovered under `profiling_root_path/ASCEND_PROFILER_OUTPUT/` after slicing by the user-provided ns window.

### Trace artifacts from `scripts/process_profiling.py`

Expected files:

- `llm_trace_summary.md`
- `llm_trace_bundle.json`
- `stats.json`

Artifact notes:

- `llm_trace_summary.md`: human-readable trace summary
- `llm_trace_bundle.json`: structured trace evidence, schema `compact_llm_trace/v2`, time unit `relative_ns`
- `stats.json`: processing metadata and output index

Key top-level fields in `llm_trace_bundle.json`:

- `schema`
- `time_unit`
- `summary`
- `legend`
- `tracks`
- `names`
- `cats`
- `spans`
- `span_args`
- `markers`
- `bins`
- `anomalies`

### Kernel artifacts from `scripts/process_kernel.py`

Expected files:

- `kernel_analysis.md`
- `stream_summary.json`
- `top_kernels.json`
- `bubble_candidates.json`

Artifact notes:

- `stream_summary.json`: per-stream activity summary, time unit `us`
- `top_kernels.json`: ranked kernel list by duration and total cost, time unit `us`
- `bubble_candidates.json`: global busy-union and gap analysis, time unit `us`
- `kernel_analysis.md`: human-readable kernel-side summary

### Time-unit contract

Use consistently:

- trace structured outputs: `relative_ns`
- kernel structured outputs: `us`
- ratio fields such as `wait_ratio` and `global_gap_ratio`: unitless

## 2. Final deliverable

Every full run should generate one primary output:

- `kernel_code_mapping_report_<case_name>.md`

General rules:

- the final report is written in Chinese
- the final report is delivered as Markdown
- all human-facing section titles, table titles, column headers, notes, summaries, and follow-up items are written in Chinese
- kernel names, operator names, code symbols, and file paths may remain in their original language
- the final report is self-contained
- the final report is centered on a kernel-to-code mapping table
- the primary mapping table uses one row per kernel row
- the primary mapping table is keyed by concrete kernel `row_id`
- every required mapping table is rendered as a Markdown table inside the report
- CSV may exist only as a preprocessing artifact or optional machine-readable attachment and must not replace the human-facing Markdown tables in the final report
- no final synthesized JSON output is required

The reusable machine-readable evidence layer remains the preprocessing artifacts above.

## 3. Evidence-ref convention

Use explicit refs whenever recoverable:

- `launch_ref`
- `code_ref`
- `trace_ref`
- `kernel_ref`

## 4. Markdown report schema

The report should preserve these structured groups:

- configuration context
- token path summary
- kernel-to-code mapping table
- last-layer compute row mapping table when required
- coverage summary
- runtime subphase summary when required
- buffer provenance notes when required
- repeated-layer folding notes when used
- unresolved rows when required
- light performance notes
- follow-up inputs
- early missing-input notes when needed

### 4.1 Configuration context

Required content:

- profiling root path
- `ASCEND_PROFILER_OUTPUT` path used for raw discovery
- raw `trace_view.json` path
- raw `kernel_details.csv` path
- `window_start_ns`
- `window_end_ns`
- model weight path
- local code repository path
- launch command used for reasoning
- available evidence
- missing prerequisites / evidence
- preprocessing source confirmation
- whether strict code localization is allowed

Include when available:

- benchmark command / result
- model-structure constraints from `config.json`
- weight-layout constraints from `quant_model_description.json`

### 4.2 Token path summary

Keep this section short.

Role:

- this section is phase-level summary only
- it describes the expected token path, semantic stages, and candidate code regions
- it must not replace per-row ownership or per-row code localization

Recommended columns:

- `Step ID`
- `Phase`
- `Expected layer template / range`
- `Expected substages`
- `Expected semantics`
- `Expected kernel signature`
- `Candidate code region`
- `Owner class`
- `Primary match basis`
- `Output-head anchors`
- `Parallel with`
- `Refs`

### 4.3 Kernel-to-code mapping table

This is the mandatory core section.

Fixed section title:

- English: `Primary Kernel-to-Code Mapping Table`
- Chinese: `主 Kernel Row 到代码映射表`

Do not use hotspot-only titles such as `Hot Kernel To Code Mapping` for this mandatory primary section.
Do not replace this mandatory primary section with a CSV file or CSV-looking plain-text dump.

Required columns:

- `Seq`
- `Kernel row id`
- `Kernel name`
- `Stream`
- `Concurrent rows / parallel group`
- `Time window`
- `Phase`
- `Layer ordinal / range`
- `Substage`
- `Owner class`
- `Semantic subphase`
- `Layer instance role`
- `Last-layer candidate`
- `Mapped file`
- `Mapped function / region`
- `Mapped line / range`
- `Localization ceiling`
- `Match basis`
- `Why this owner wins`
- `Why nearby owners do not win`
- `Mapping strength`
- `Model-layout evidence`
- `Buffer-provenance evidence`
- `Refs`
- `Confidence`
- `Notes`

Required behavior:

- keep all kernels in the selected window when strict mapping is available
- use one primary table row per kernel row
- key the primary mapping table by concrete kernel `row_id`
- do not merge multiple kernel rows into one primary row just because they share the same kernel name, operator family, code owner, or coarse profiling label
- do not merge compute kernel rows in the primary table even when they belong to the same layer, same substage, or same repeated pattern
- do not summarize runtime-control rows only at phase level; runtime-control, scheduler-overlap, speculative-preparation, copy, sync, metadata, and writeback rows must remain explicit row by row
- if exact line number is unavailable, keep the strongest recoverable narrow line range and mark the missing precision
- keep `Mapped line / range` empty or marked `not_recoverable` when the honest localization ceiling is only `function_region_only`, `owner_stage_only`, or `unresolved`
- if no reliable mapping can be made, keep the kernel row and mark `insufficient_evidence`
- if neighboring rows belong to different owner classes, split them rather than merging them into one row group
- for lightweight data-reorganization clusters, describe the match basis using phase context, shape transitions, and buffer provenance before using kernel names
- record the kernel's stage / phase, stream id, and overlap / parallel context when recoverable
- if a row participates in double-stream or multi-stream execution, explicitly record the concurrent rows or parallel group when recoverable
- for model-forward rows, keep the strongest honest layer ordinal, or otherwise the narrowest honest layer range
- for model-forward rows, keep substage ownership when recoverable instead of stopping at coarse `full forward` or `decoder body`
- when output-head anchors are observed nearby, explicitly record whether the row belongs to a last-layer candidate neighborhood
- for runtime-related rows, include explicit owner-competition rationale in `Why this owner wins` and `Why nearby owners do not win`
- if a repetitive sample is used as the detailed example, explicitly mark whether it is the first or the last repetitive layer

Primary-table validity rules:

- every primary row must contain exactly one kernel `row_id`
- if the same kernel name appears many times in the window, those rows must remain separate in the primary table
- compute rows must remain expanded row by row in the primary table and must not be folded or merged away
- runtime rows must remain expanded row by row in the primary table and must not be replaced by a phase-only narrative
- runtime owner information may be rendered inside the primary mapping table and does not require a separate `Runtime Owner Ledger` section as long as all required runtime-owner fields remain explicit row by row
- grouped summaries by kernel family, operator family, or coarse profiling evidence are allowed only as secondary overview sections
- the primary mapping table must be joinable back to the sliced kernel csv by `row_id` without ambiguity
- a report without the primary mapping table is invalid
- a report where the mandatory primary mapping table is not rendered as a Markdown table is invalid
- the primary mapping table has higher rendering priority than narrative analysis
- user-highlighted hot regions must remain unfolded in the primary table

Allowed values for `Localization ceiling`:

- `exact_line`
- `narrow_line_range`
- `function_region_only`
- `owner_stage_only`
- `unresolved`

### 4.3b Last-layer compute row mapping table

This section is mandatory when a contrast or reference window is used to justify target-model compute behavior, double-stream evidence, output-head anchoring, or a last-layer candidate outside the selected primary window.

Purpose:

- make target-model compute evidence row-level instead of narrative-only
- show the last repeated layer candidate neighborhood with the same row-level discipline as the primary table
- connect the last repeated layer candidate to nearby final norm, lm_head, and sampling-tail anchors when observed

Fixed section title:

- English: `Last-Layer Compute Row Mapping Table`
- Chinese: `最后一层计算 Kernel Row 到代码映射表`

Required behavior:

- keep one row per kernel `row_id`
- include only the last repeated layer candidate neighborhood plus the immediately adjacent post-layer output-head neighborhood when recoverable
- do not expand every repeated layer when the model-forward structure is repetitive and only the last repeated layer is needed for localization
- preserve stream and concurrent-row context, including double-stream or multi-stream execution when recoverable
- if the report cites a contrast or reference window for target-model compute ownership, the relevant compute rows must appear here rather than remaining only in narrative summary

Recommended columns:

- reuse the same columns as the primary mapping table whenever practical
- at minimum keep `Kernel row id`, `Kernel name`, `Stream`, `Concurrent rows / parallel group`, `Layer ordinal / range`, `Substage`, `Owner class`, `Last-layer candidate`, `Mapped file`, `Mapped function / region`, `Mapped line / range`, `Localization ceiling`, `Match basis`, `Refs`, `Confidence`

### 4.3a Runtime owner information

This runtime-only row-level ownership information is mandatory for speculative, overlap-heavy, or scheduler-driven runs.

Purpose:

- make runtime ownership explicit before or alongside per-row code localization
- ensure runtime-control rows are not silently compressed into coarse phase narratives
- show why one runtime owner was accepted over nearby competing owners

Required fields:

- `Kernel row id`
- `Owner class`
- `Semantic subphase`
- `Stream`
- `Concurrent rows / parallel group`
- `Mapped file`
- `Mapped function / region`
- `Mapped line / range`
- `Why this owner wins`
- `Why nearby owners do not win`
- `Refs`
- `Confidence`

Required behavior:

- include every runtime-control, scheduler-overlap, or speculative-preparation row in the selected window
- keep one runtime-owner record per kernel `row_id`
- preserve double-stream or multi-stream runtime overlap context when recoverable
- if line-level localization is not honest, keep the strongest honest function region or owner-stage claim
- if a runtime row cannot be localized beyond `insufficient_evidence`, it must still remain visible in the primary mapping table

Rendering options:

- render the runtime-owner information as a dedicated `Runtime Owner Ledger` section
- or render it directly as explicit columns inside the primary mapping table

Either option is valid, but hotspot-only runtime samples are never valid.

### 4.4 Coverage summary

This section is mandatory.

Required fields:

- `Selected window row count`
- `Primary-table mapped row count`
- `Primary-table insufficient-evidence row count`
- `Folded row ranges`
- `Coverage reconciliation note`

Rules:

- `Selected window row count` must refer to the sliced-kernel window produced by the fixed preprocessing run
- `Primary-table mapped row count` must count rows present in the primary mapping table
- `Primary-table insufficient-evidence row count` must count rows kept in the primary table with `insufficient_evidence`
- if no rows are folded, `Folded row ranges` must explicitly say `none`
- if rows are folded, each folded range must be listed explicitly
- the reconciliation note must make it possible to explain every selected kernel row as either explicitly mapped in the primary table or explicitly folded
- if runtime-owner information is required, the reconciliation note must also state whether all runtime rows are represented there
- if a last-layer compute row mapping table is required, the reconciliation note must also state which contrast or reference rows are represented there

Enum guidance:

- `Owner class`
  - `model_forward`
  - `runtime_control`
  - `scheduler_overlap`
  - `spec_prepare`
  - `output_head`
  - `insufficient_evidence`
- `Layer instance role`
  - `first_repeated_layer`
  - `middle_repeated_layer`
  - `last_repeated_layer`
  - `post_layer_output_head`
  - `non_repeated_runtime_region`
  - `not_applicable`
- `Last-layer candidate`
  - `yes`
  - `no`
  - `unknown`
- `Mapping strength`
  - `strict_owner`
  - `probable_owner`
  - `adjacent_stage_only`
  - `insufficient_evidence`

### 4.5 Runtime subphase summary

This section is mandatory for speculative, overlap-heavy, or scheduler-driven runs.

Recommended columns:

- `Runtime subphase`
- `Observed in window`
- `Primary owner candidates`
- `Primary match basis`
- `Refs`

Minimum semantic labels to check:

- `runtime_future_resolve`
- `runtime_metadata_prepare`
- `target_verify_body`
- `verify_accept_or_sample`
- `tree_or_cache_update`
- `scheduler_postprocess_or_writeback`

If a phase is absent after checking, say so explicitly.
If a phase is only partially localizable, keep it and downgrade confidence instead of collapsing it into one coarse runtime bucket.

### 4.6 Buffer provenance notes

This section is mandatory when generic runtime clusters are assigned to scheduler / overlap / future-map owners.
It is strongly recommended whenever lightweight data-reorganization clusters are central to the mapping.

Recommended fields:

- `Buffer family`
- `Producer region`
- `Consumer region`
- `Expected shape pattern`
- `Observed kernel rows`
- `Refs`

### 4.7 Repeated-layer folding notes

This section is optional and applies only when repetitive layers are proven structurally identical.

Required fields:

- `Folded layer range`
- `Why folding is safe`
- `Reference layers kept in full`
- `Instance role of each kept reference layer`
- `What remains identical`
- `What was not folded`
- `Why task requirements still allow folding`

Rules:

- only the middle strictly repetitive model-forward layers may be folded
- the first repetitive layer and the last repetitive layer must remain fully expanded
- the first kept model-forward reference should include the embedding-preface region when it belongs to the same modeled path entry
- all non-repetitive regions must remain fully expanded
- when the task explicitly asks for compute-process breakdown, layer numbering, or last-layer judgment, do not fold the relevant model-forward rows in the primary table
- when the user highlights a hot region for detailed mapping, do not fold that region in the primary table
- if only one repetitive sample is shown, the report must justify whether it is the first or the last repetitive layer
- compute rows must not be folded in the primary mapping table unless the user explicitly requests reduced detail
- when a contrast or reference window is used only to justify the last repeated layer candidate, expand that last-layer candidate neighborhood row by row instead of expanding all repeated layers

### 4.8 Unresolved rows

This section is mandatory whenever any selected kernel rows are not localized beyond `insufficient_evidence` or are otherwise omitted from detailed line-level claims.

Required fields:

- `Kernel row id or range`
- `Why unresolved`
- `Strongest honest owner`
- `Strongest honest code region`
- `Missing evidence`
- `Refs`

Rules:

- unresolved rows must never be silent
- if this section is absent, every selected kernel row must be mapped to a concrete code line or the narrowest honest line range
- rows kept in the primary table as `insufficient_evidence` may still be repeated here for summary, but they must not disappear from the primary table

### 4.9 Light performance notes

Keep this section short.

Allowed rows:

- `Note ID`
- `Observation`
- `Why it matters for mapping`
- `Refs`

Allowed examples:

- obvious bubble between two kernel groups
- obvious serialized region that explains a code-path transition
- obvious missing fused path that explains why many small kernels appear
- obvious missing overlap that changes path interpretation

### 4.10 Follow-up inputs

Only include inputs that would materially improve kernel-to-code mapping, such as:

- corrected profiling root path
- missing `trace_view.json` or `kernel_details.csv` under `ASCEND_PROFILER_OUTPUT`
- corrected `window_start_ns` / `window_end_ns`
- real launch script
- missing source files
- missing trace markers around the selected window

## 5. Model-layout evidence guidance

Model-layout evidence is optional but strongly recommended when config files are available.

Allowed examples:

- `architecture=LongcatFlashForCausalLM`
- `num_layers=28`
- `attention_method=MLA`
- `self_attn.*=FLOAT`
- `mlp.experts.*=W4A4_MXFP4`
- `not_applicable`

Rule:

- use model-layout evidence to strengthen model-side mapping only
- do not use model-layout evidence to override stronger runtime-control ownership evidence
