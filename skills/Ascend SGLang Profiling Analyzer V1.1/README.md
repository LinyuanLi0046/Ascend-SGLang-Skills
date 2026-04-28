# Ascend SGLang Profiling Analyser

This skill reconstructs the runtime code path for one selected SGLang Ascend profiling window, aligns profiling evidence to that path, and outputs a table-centered kernel-to-code mapping report.

Its scope is intentionally narrow:

- profiling structure analysis
- model-structure and weight-layout checking when config files are available
- code-path reconstruction
- runtime-control path reconstruction
- profiling-to-code alignment
- per-kernel table generation
- light performance review only

It is intentionally not centered on:

- broad finding inventories
- broad host-bound diagnosis
- full TPOT prioritization
- broad recommendation writing

## Document map

- [SKILL.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/SKILL.md): primary normative spec
- [design_principles.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/design_principles.md): principles only
- [pipeline.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/pipeline.md): stage order and stage I/O
- [output_contract.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/output_contract.md): output schema and validity rules
- [analysis_checklists.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/analysis_checklists.md): final self-check

## Stable preprocessing layer

The preprocessing layer remains unchanged:

1. slice trace by window with `scripts/slice_profiling.py`
2. summarize sliced trace with `scripts/process_profiling.py`
3. slice `kernel_details.csv` by the same window with `scripts/slice_kernel_csv.py`
4. summarize sliced kernel csv with `scripts/process_kernel.py`

These scripts still produce the reusable evidence artifacts.

## Single input contract

This skill now supports exactly one input scheme.

Required inputs:

1. profiling root path
   - the agent must discover full raw profiling files under `profiling_root_path/ASCEND_PROFILER_OUTPUT/`
   - required raw files:
     - `trace_view.json`
     - `kernel_details.csv`
2. analysis window timestamps
   - `window_start_ns`
   - `window_end_ns`
3. model weight path
   - use it to locate `config.json`
   - use `quant_model_description.json` when present
4. local code repository path
5. launch command

Optional:

6. benchmark command and result

Window rule:

- do not analyze the full raw capture directly as final evidence
- always slice by the provided ns window first
- require one complete token inference window
- prefer decode windows
- for speculative / MTP cases, require a full `verify` cycle with matching draft work

## Minimum strict-mapping inputs

Required for strict code localization:

- profiling root path with `ASCEND_PROFILER_OUTPUT/trace_view.json` and `ASCEND_PROFILER_OUTPUT/kernel_details.csv`
- `window_start_ns` and `window_end_ns`
- model weight path
- actual launch script or launch command
- current SGLang source code

Helpful but optional:

- benchmark result
- benchmark command
- trace markers or operator-detail artifacts

If launch parameters are missing, the skill may still provide structure and tentative hints, but must not claim strict code localization.

## Core behavior summary

- reconstruct one token path in code before strict mapping
- reconstruct runtime-control stages together with model-forward stages
- always run the four preprocessing scripts in the fixed order on the full raw profiling files
- treat generic kernels such as `GatherV2`, `Slice`, `Index`, `Cast`, `ViewCopy`, `Arange`, `Transpose`, and `Copy` as owner-competition problems first
- explicitly check `runtime_future_resolve` when overlap, future-map, or speculative scheduling is present
- keep the primary mapping table at one row per kernel `row_id`
- keep `Token Path Summary` as phase-level summary only; it does not replace row-level mapping
- keep runtime-owner information at runtime-only row level; it may appear as a dedicated section or as explicit columns inside the primary mapping table
- when target-model compute ownership is justified by a contrast or reference window, render a row-level table for the last-layer compute neighborhood instead of keeping that compute evidence only in narrative form
- do not fold compute rows in the primary mapping table unless reduced detail is explicitly requested

## Final deliverable

The final deliverable is one Markdown report centered on a kernel-to-code mapping table.

Human-facing output rules:

- the final report is written in Chinese
- required mapping tables are rendered as Markdown tables inside the report
- CSV may remain as preprocessing evidence or optional machine-readable attachment, but it does not replace the final Markdown tables

The primary table must:

- use the fixed section title `Primary Kernel-to-Code Mapping Table`, or in Chinese `主 Kernel Row 到代码映射表`
- map one kernel `row_id` per row
- include stream id and overlap / parallel context when recoverable
- include code file, function / region, and exact line number when recoverable
- otherwise include the narrowest honest line range

When speculative, overlap-heavy, or scheduler-driven runtime rows are present, their runtime-owner information must remain explicit row by row in the final report.

When a contrast or reference window is used to justify target-model compute behavior, double-stream evidence, or a last-layer candidate, the report must also include `Last-Layer Compute Row Mapping Table` / `最后一层计算 Kernel Row 到代码映射表`.

If grouped summaries are useful, they may appear only as secondary overview tables.
