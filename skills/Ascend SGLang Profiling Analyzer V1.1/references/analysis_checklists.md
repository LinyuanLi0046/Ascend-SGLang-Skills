# Analysis Checklists

This file is a final self-check only.
It does not redefine the principles or the pipeline.

Use these checklists to validate the final report before claiming strict mapping.

## A. Input contract and token window

Check:

- the required single input contract was provided
- the profiling input is a profiling root path rather than sliced or derived artifacts
- `window_start_ns` and `window_end_ns` were provided
- the model weight path, code repository path, and launch command were provided
- one complete token inference process is covered
- causal structure is preserved
- decode is the intended focus when applicable
- MTP / speculative cases include one complete `verify` cycle with matching draft work

Fail action:

- stop and ask the user to provide the missing contract fields or redefine the window

## B. Raw profiling discovery, fixed preprocessing, and launch context

Check:

- `ASCEND_PROFILER_OUTPUT` was located under the provided profiling root path
- full raw `trace_view.json` was located there
- full raw `kernel_details.csv` was located there
- the four preprocessing scripts were run in the fixed order on the full raw files using the provided ns window
- generated sliced trace, sliced kernel csv, and derived summaries were used as the evidence layer for later stages
- actual launch script or launch command is present
- relevant runtime flags for model path, MTP, graph, overlap, cache, and parallelism were extracted
- the provided launch actually produced this profiling run

If missing:

- stop when raw profiling discovery or fixed preprocessing did not happen
- allow structure analysis only when launch context is incomplete
- downgrade code localization when launch context is incomplete
- explicitly request the real launch parameters when needed

## C. Token-path reconstruction

Check:

- one token path was reconstructed in code step by step
- intermediate stages were kept
- each stage has expected kernel signatures
- each stage has candidate code regions
- when model-forward work exists, a repeated layer template or equivalent substage template was reconstructed
- output-head anchors were checked when the window may include final norm, lm_head, or sampling tail
- when target-model compute evidence comes from a contrast or reference window, the last-layer candidate neighborhood for that compute path was identified for row-level rendering

## D. Model structure and weight layout

When config files are available, check:

- `config.json` was read
- architecture, layer count, hidden size, FFN size, attention method, and MoE scale were extracted
- `quant_model_description.json` was checked when present
- representative submodule families such as `self_attn`, `mlp.experts`, `lm_head`, `embed_tokens`, and `model.norm` were sampled
- model-layout evidence was converted into expected kernel-family hints
- repeated-layer constraints were recorded
- model-layout evidence remained separate from runtime-control ownership evidence

## E. Runtime-control path

Check:

- scheduler and manager code around the token window was reconstructed
- overlap, future-map, buffer-resolution, copy, sync, and submission stages were checked
- pre-forward and post-forward runtime-control regions were identified
- semantic runtime subphases such as future resolution, metadata preparation, verify handoff, acceptance or sampling, tree/cache update, and scheduler writeback were explicitly checked
- future-slot allocation, future resolve, future-store, and delayed-sampling style paths were checked when overlap or speculative execution is enabled
- lightweight data-reorganization clusters were checked against runtime-control owners before speculative-prep owners
- host-side wait, sync, or copy spans were checked against runtime-control logic instead of model-forward logic
- code-side future-buffer patterns such as `future_indices`, `buf[indices]`, `index_select`, `record_stream(...)`, `resolve_future(...)`, or `store_to_map(...)` were checked as candidate owners
- `runtime_future_resolve` is explicitly marked as observed, absent, or partially localizable in overlap-heavy runs

## F. Kernel-to-code alignment

For every mapping claim, check:

- the claim uses a local kernel pattern rather than one kernel name alone
- neighboring kernels line up
- trace spans and phase context line up
- tensor-shape and dtype transitions line up when recoverable
- code-side buffer provenance supports the claim when recoverable
- the proposed code region belongs to the reconstructed token path
- the claimed line location is truly recoverable
- the owner class is clear: `model_forward`, `runtime_control`, `scheduler_overlap`, `spec_prepare`, `output_head`, or `insufficient_evidence`
- lightweight data-reorganization clusters were checked in the correct owner priority order
- mixed-owner clusters were split when necessary
- model-layout evidence actually supports claimed model-side owners
- dedicated runtime subphases were kept instead of collapsing everything into coarse `runtime_control`
- `spec_prepare` claims explicitly rejected `scheduler_overlap` / `runtime_future_resolve` when that competition existed
- model-forward rows did not stop at coarse `full forward` or `decoder body` when layer or substage evidence was recoverable
- exact layer ordinal was used only when evidence supports it; otherwise the narrowest honest layer range was used
- last-layer candidate claims were justified by nearby output-head anchors when such anchors were observed
- if a contrast or reference window is used for target-model compute interpretation, the relevant last-layer compute rows are rendered row by row rather than left in narrative-only form

Fail action:

- downgrade to `adjacent_stage_only` or `insufficient_evidence`

## G. Primary mapping table

Check:

- the final report is written in Chinese for all human-facing content
- the final report is delivered as Markdown rather than CSV or plain-text table dump
- the main mapping table uses one primary row per kernel
- the main mapping table title is `Primary Kernel-to-Code Mapping Table` or `主 Kernel Row 到代码映射表`
- the main mapping table is rendered as a Markdown table inside the report
- each primary row has exactly one kernel `row_id`
- the main mapping table is keyed by concrete kernel `row_id`
- repeated kernel names such as multiple `GatherV2` or `Cast` rows remain expanded instead of merged by family name
- each row includes phase / stage context
- each row includes layer ordinal or layer range for model-forward rows when recoverable
- each row includes substage for model-forward rows when recoverable
- each row includes stream id
- each row includes overlap / parallel context when recoverable
- compute rows remain separated row by row and were not merged by layer, operator family, or repeated-pattern summary
- runtime rows remain separated row by row and were not replaced by a phase-only narrative
- each row includes mapped file, function / region, and exact line or narrowest honest range
- each row includes an honest localization ceiling
- each row includes last-layer-candidate status when output-head ambiguity exists nearby
- the primary table can be joined back to the sliced kernel csv by `row_id` without ambiguity
- the report is invalid if the primary mapping table is missing
- the report is invalid if the primary mapping table is emitted only as CSV or CSV-like plain text
- the primary mapping table was preserved even if narrative sections had to be shortened
- hotspot-only titles such as `Hot Kernel To Code Mapping` are not used for the mandatory primary section

Fail action:

- do not deliver the report; regenerate with the primary mapping table preserved first

## G1. Last-layer compute row mapping table

When a contrast or reference window is used to justify target-model compute behavior, check:

- a `Last-Layer Compute Row Mapping Table` is present
- the table covers the last repeated layer candidate neighborhood row by row
- the table includes immediately adjacent final norm, lm_head, or sampling-tail anchors when observed
- double-stream or multi-stream compute context is preserved when recoverable
- the compute evidence is not left only in a contrast-window narrative paragraph

Fail action:

- do not deliver the report; regenerate with row-level last-layer compute mapping

## H. Coverage summary

Check:

- the report explicitly states the selected-window row count
- the report explicitly states the primary-table mapped row count
- the report explicitly states the primary-table `insufficient_evidence` row count
- the report explicitly states folded row ranges, or says `none`
- the row accounting is reconcilable without silent omissions
- when runtime-owner information is required, its row accounting is also reconcilable against the selected window
- when a last-layer compute row mapping table is required, its referenced contrast or reference rows are also reconciled explicitly

Fail action:

- do not deliver the report until coverage accounting is explicit and reconcilable

## I. Repeated-layer folding

Only fold middle repeated model-forward layers if:

- the layer structure is truly repetitive
- the kernel sequence pattern is identical enough for strict reuse
- the code owner region is identical enough for strict reuse
- the first repetitive layer remains fully shown, including the embedding-preface region when it belongs to the same modeled path preface
- the last repetitive layer remains fully shown
- all non-repeated regions remain fully shown
- all non-model-forward runtime and scheduler regions remain fully shown
- compute rows in the primary mapping table were not folded unless the user explicitly requested reduced detail
- every expanded repetitive sample explicitly states whether it is the first or the last repetitive layer
- the task does not explicitly require layer-by-layer compute breakdown, layer numbering, or last-layer judgment for the folded rows
- no user-highlighted hot region was folded in the primary table

If any of the above fails:

- do not fold, or downgrade the folded claim

## I1. Runtime owner information

For speculative, overlap-heavy, or scheduler-driven runs, check:

- runtime-owner information is present either as a dedicated `Runtime Owner Ledger` section or as explicit primary-table columns
- every runtime-control, scheduler-overlap, speculative-preparation, copy, sync, metadata, and writeback row in the selected window appears in that runtime-owner information
- each runtime-owner record contains exactly one kernel `row_id`
- each runtime-owner record states the winning owner and why nearby runtime owners did not win
- stream and concurrent-row context are preserved when runtime overlap exists
- unresolved runtime rows remain visible in the primary mapping table

Fail action:

- do not deliver the report; regenerate with full runtime row coverage

## J. Runtime subphase coverage

For speculative or overlap-heavy windows, check whether the report explicitly marks each semantic phase as observed, absent after checking, or partially localizable:

- `runtime_future_resolve`
- `runtime_metadata_prepare`
- `target_verify_body`
- `draft_cache_prepare`
- `verify_accept_or_sample`
- `draft_extend_v2_body`
- `tree_cache_update`
- `scheduler_postprocess_or_writeback`
- `final_norm_lm_head`

## K. Unresolved rows

Check:

- any unresolved kernel rows are explicitly listed
- unresolved rows have a concrete reason and missing-evidence statement
- rows marked `insufficient_evidence` in the primary table did not disappear from the report
- if the `Unresolved Rows` section is absent, every selected kernel row is localized to a concrete code line or the narrowest honest line range

Fail action:

- do not deliver the report if unresolved rows exist but are not declared

## K1. Parallel-context completeness

Check:

- if double-stream or multi-stream execution exists, each affected kernel row records stream identity
- if concurrent rows are recoverable, the report explicitly records concurrent rows or parallel groups
- the report distinguishes true device overlap from mere trace adjacency when recoverable
- no parallel neighborhood is explained as if it were a single serial stream

Fail action:

- do not deliver the report until per-row parallel context is explicit

## K2. Section-role separation

Check:

- `Token Path Summary` remains phase-level summary and does not substitute for row-level mapping
- `Token Path Summary` does not claim to be the full primary mapping table
- runtime-owner information is runtime-only row-level ownership evidence rather than a mixed token-path narrative
- runtime-owner information does not contain hotspot-only samples when the selected window requires full row coverage

Fail action:

- do not deliver the report until section roles are explicit and non-overlapping

## L. Light performance notes

Check:

- every performance note directly supports mapping
- only small observations are kept
- no broad diagnosis inventory was produced
- no large host-bound finding set was produced
- no TPOT prioritization section was produced
