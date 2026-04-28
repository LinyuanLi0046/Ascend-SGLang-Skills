---
name: ascend sglang profiling analyser
description: >
  Reconstruct the code path for one selected SGLang Ascend profiling window and output a
  kernel-to-code mapping table. Best for profiling + code-path analysis, profiling-to-code
  alignment, per-kernel file/line localization, and only light performance commentary.
---

# Ascend SGLang Profiling Analyser

## Purpose

This skill reconstructs the runtime code path for one selected SGLang Ascend profiling window, aligns profiling evidence to that path, and produces a table-centered kernel-to-code report with only light performance commentary.

The reasoning target is:

- code-path reconstruction
- profiling-to-code strict alignment
- per-kernel file / function / line localization
- runtime-control ownership recovery
- only small performance notes that directly help mapping

This skill is not centered on broad finding inventories, full host-bound diagnosis, or full TPOT prioritization.

## Single input contract

This skill supports exactly one input scheme.

Required user inputs:

- profiling root path
  - the agent must look under `profiling_root_path/ASCEND_PROFILER_OUTPUT/`
  - the primary raw evidence must be:
    - `trace_view.json`
    - `kernel_details.csv`
- analysis window timestamps
  - `window_start_ns`
  - `window_end_ns`
  - these timestamps define one complete token-level inference window
  - the window may be one step, or the tail of one step plus the head of the next step, as long as the causal token process remains complete enough for mapping
- model weight path
  - use it to locate `config.json`
  - use `quant_model_description.json` when present
- local code repository path
- launch command

Optional user inputs:

- benchmark command
- benchmark result

## Single Source Of Truth

`SKILL.md` is the normative entry point.
The files under `references/` are also normative, but each one has exactly one role:

- `references/design_principles.md`: principles and rule definitions
- `references/pipeline.md`: stage order, inputs, outputs, and stage gates
- `references/output_contract.md`: report schema and validity rules
- `references/analysis_checklists.md`: final self-check and downgrade conditions

`README.md` is only a usage guide and summary.

## Fixed preprocessing pipeline

These four scripts remain unchanged as the evidence-preparation layer, but they now run in one fixed order for every full analysis:

| Role | Script |
|---|---|
| trace slicing | `scripts/slice_profiling.py` |
| trace summarization | `scripts/process_profiling.py` |
| kernel slicing | `scripts/slice_kernel_csv.py` |
| kernel summarization | `scripts/process_kernel.py` |

They must not lose functionality.
The skill narrows the reasoning target and output shape, not the preprocessing layer.

Fixed preprocessing order:

1. locate full raw `trace_view.json` and `kernel_details.csv` under `profiling_root_path/ASCEND_PROFILER_OUTPUT/`
2. slice the raw trace by `window_start_ns` and `window_end_ns`
3. summarize the sliced trace
4. slice the raw kernel csv by the same window
5. summarize the sliced kernel csv

The generated sliced trace, sliced kernel csv, and their summaries remain the lossless evidence layer for the later reasoning stages.

## Window contract

- never analyze the full raw profiling capture directly as the final evidence
- always slice from the full raw `trace_view.json` and `kernel_details.csv` using the user-provided ns window
- require one complete token inference window
- prefer decode-time windows
- for MTP / speculative cases, require one complete `verify` cycle together with matching draft work
- if timestamps are missing, stop and ask for them

## Main questions

The skill should answer:

1. What code path is expected for the selected window?
2. What kernel and trace structure is actually observed?
3. Which concrete file / function / line best matches each kernel row?
4. Which rows belong to model-forward, runtime-control, scheduler-overlap, spec-prepare, or output-head ownership?
5. For model-forward rows, which layer or layer-range and which substage best match each row?
6. When output-head anchors appear, does the observed structure support a last-layer candidate?
7. Does the observed kernel family agree with model structure and weight layout?
8. Which small performance facts matter for understanding that mapping?

## Normative rules

Use these rule IDs consistently across the reference documents.

### R1. Code path first

When launch context and code are available, reconstruct one token path in code before strict mapping.

That token path must include both:

- the model semantic path
- the runtime-control path

When the token path contains model-forward work, the reconstructed model path must go beyond coarse labels such as `full forward` or `decoder body` and include:

- the repeated layer template when repetition exists
- expected per-layer substages when recoverable
- output-head anchor expectations when present

### R2. Profiling validates the path

Trace and kernel evidence validate, localize, and boundary-check the reconstructed path.
Profiling does not replace code-path reconstruction.

### R3. Local-pattern alignment only

Never align code by a single kernel name alone.
The matching unit is a local execution pattern composed from:

- neighboring kernels
- adjacent trace spans
- relative order
- phase or layer context
- tensor-shape and dtype transitions when recoverable
- code-side buffer provenance when recoverable

Kernel names are weak hints only.

### R4. Runtime path is first-class

Scheduler, overlap, future-map, copy, sync, buffer-resolution, and submission logic are part of the token path.
They must be reconstructed together with model-forward logic, especially for speculative, overlap-heavy, or scheduler-driven runs.

For overlap-heavy runs, semantic runtime subphases are mandatory coverage items, including:

- future resolution into the current batch
- metadata or block-table preparation
- verify body handoff
- acceptance or sampling
- tree, cache, or scheduler writeback

If launch flags or code indicate overlap, speculative scheduling, or future-buffer usage, the final report must explicitly say whether required runtime subphases were:

- observed
- absent after checking
- partially localizable

If `runtime_future_resolve` is not explicitly checked, strict ownership claims for nearby generic runtime clusters are not allowed.

### R5. Generic-cluster owner competition

For lightweight data-reorganization clusters, treat alignment as a dataflow-resolution problem first.
This includes indexing, slicing, gathering, scattering, copying, view changes, range construction, metadata materialization, and similar work even when operator names differ across backends.

Owner priority is:

1. scheduler / overlap / future-map owners
2. speculative-preparation owners
3. model-forward owners

Candidate-owner comparison must consider:

- phase match
- neighboring-pattern match
- tensor-shape and dtype match when recoverable
- buffer-provenance match
- code-path reachability

If no owner explains the whole cluster clearly enough, split the cluster or downgrade it instead of collapsing it into one coarse label.

### R6. Future-map checks are semantic

When launch flags or code indicate overlap scheduling, speculative decoding, delayed sampling, or future-buffer usage, explicitly check for semantic stages equivalent to:

- future-slot or future-index allocation
- future-buffer resolution into the current batch
- result storeback to future buffers
- delayed sampling or delayed materialization
- stream or host handoff points guarding these stages

Patterns such as the following are first-class owner evidence for nearby generic runtime clusters:

- `future_indices`
- `buf[indices]`
- `index_select`
- `record_stream(...)`
- `resolve_future(...)`
- `store_to_map(...)`

### R7. Model layout constrains mapping

When available, use `config.json` and `quant_model_description.json` to constrain:

- repeated-layer count
- attention / MLA path
- dense MLP path
- MoE path
- quantized vs non-quantized submodule families
- output-head expectations
- layer ordinal expectations when repeated decoder structure exists

Model-layout evidence strengthens model-side localization only.
It must not override stronger runtime-control evidence.

### R8. Primary table is per-kernel-row

The final report is table-centered.
Its primary mapping table must:

- use one primary row per kernel row
- be keyed by concrete kernel `row_id`
- never merge multiple kernel rows just because they share a kernel name, operator family, or coarse profiling label
- remain joinable back to the sliced kernel csv by `row_id`
- keep model-forward rows at layer/substage granularity when evidence permits, rather than stopping at `full forward`

Grouped summaries by operator family are allowed only as secondary overview tables.

### R9. Repeated-layer folding is strict

Only the middle repeated model-forward layers may be folded, and only when structural identity is strong enough.

Always:

- keep the first repetitive layer in full
- keep the last repetitive layer in full
- include the embedding-preface region in the first kept model-forward reference when it belongs to the same modeled entry path
- keep all non-repeated regions fully expanded
- keep all non-model-forward runtime and scheduler regions fully expanded

For user-highlighted hot windows, or when the task explicitly asks for compute-process breakdown, layer numbering, or last-layer judgment:

- do not fold model-forward rows in the primary mapping table
- keep layer-level ownership explicit row by row

### R10. Missing evidence narrows claims

If launch context, code, or evidence is missing:

- structure analysis may continue
- tentative hints may continue
- strict line-level localization must be downgraded

Use `insufficient_evidence` instead of guessing.

### R11. Keep performance commentary light

Allowed performance comments are limited to observations that directly help mapping, such as:

- obvious bubbles between code stages
- obvious serialization
- obvious missing fused paths
- obvious missing overlap that changes path interpretation

Do not expand into a broad diagnosis inventory.

## Execution workflow

Follow the stage order defined in [pipeline.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/pipeline.md).

High-level flow:

```text
INGEST
-> INPUT_CONTRACT_VALIDATION
-> RAW_PROFILING_DISCOVERY
-> WINDOW_VALIDATION
-> FIXED_PREPROCESSING
-> CONTEXT_INVENTORY
-> LAUNCH_CONTEXT_CHECK
-> MODEL_STRUCTURE_AND_WEIGHT_LAYOUT_CHECK
-> TOKEN_PATH_RECONSTRUCTION
-> RUNTIME_CONTROL_PATH_RECONSTRUCTION
-> RUNTIME_GROUNDING
-> TRACE_STRUCTURE_ANALYSIS
-> KERNEL_DEVICE_ANALYSIS
-> STRUCTURE_SYNTHESIS
-> GENERIC_CLUSTER_OWNER_COMPETITION
-> PROFILING_TO_CODE_ALIGNMENT
-> KERNEL_CODE_MAPPING_TABLE
-> LIGHT_PERFORMANCE_REVIEW
-> RENDER
```

## Output requirements

The final deliverable is one Markdown report:

- recommended filename: `kernel_code_mapping_report_<case_name>.md`
- report language: Chinese
- report format: Markdown
- all human-facing section titles, table titles, column headers, notes, summaries, and follow-up items must be written in Chinese
- kernel names, operator names, code symbols, and file paths may remain in their original language
- report center: kernel-to-code mapping table
- every required mapping table must be rendered as a Markdown table inside the report
- CSV may exist only as a preprocessing artifact or optional machine-readable attachment; it must never replace the human-facing Markdown tables in the final report

At minimum, the report must include:

- configuration context
- token path summary
- kernel-to-code mapping table
- last-layer compute row mapping table when a contrast or reference window is used to justify target-model compute ownership
- coverage summary for the selected kernel-row window
- runtime subphase coverage summary
- buffer-provenance notes for runtime-owned generic clusters
- repeated-layer folding notes when used
- unresolved rows when any kernel rows cannot be localized beyond `insufficient_evidence`
- layer / layer-range and substage information for model-forward rows
- last-layer candidate judgment when output-head anchors are observed
- light performance notes
- follow-up inputs that would materially improve mapping

Detailed schema and validity rules are defined in [output_contract.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/output_contract.md).

Rendering priority:

1. preserve the primary mapping table
2. preserve the coverage summary
3. preserve the runtime subphase summary
4. shorten narrative analysis only after the above are preserved

## Hard constraints

 - Never accept multiple profiling input modes; this skill supports only the single input contract defined above.
 - Never treat user-provided sliced files or derived summaries as the primary profiling input.
 - Never analyze the full raw profiling capture directly as final input; always slice it first by the user-provided ns window.
 - Always discover the primary raw profiling evidence under `profiling_root_path/ASCEND_PROFILER_OUTPUT/`.
 - Always locate and use both full raw files: `trace_view.json` and `kernel_details.csv`.
 - Always run all four preprocessing scripts in the fixed order before strict mapping.
- Never deliver the final report in English or mixed English-dominant prose when Chinese is required.
- Never deliver the final mapping tables as CSV instead of Markdown tables.
- Never start strict code localization without the user's actual launch parameters.
- Never align profiling to code by matching a single kernel or operator name alone.
- Always reconstruct one token path step by step before strict mapping.
- Always reconstruct runtime-control stages together with model-forward stages for speculative or overlap-heavy runs.
- Always explicitly check `runtime_future_resolve` when overlap, future-map, or speculative scheduling is present.
 - Always keep the generated sliced trace and sliced kernel csv as the lossless evidence layer.
- Never suppress a kernel row just because its mapping is uncertain; mark it `insufficient_evidence` when needed.
- Never output an aggregated primary mapping row that stands for multiple kernel `row_id`s.
- Never skip scheduler / overlap / future-map owners when assigning generic runtime clusters.
- Never collapse a lightweight generic cluster into `spec_prepare` just because a later neighboring kernel has a clearer name.
- Never assign generic kernels such as `GatherV2`, `Slice`, `Index`, `Cast`, `ViewCopy`, or `Arange` to model code before checking runtime-control owners.
- Never use model-layout evidence to override stronger runtime-control evidence.
- Never fold non-repeated regions.
- Never stop model-forward mapping at `full forward` or `decoder body` when layer/substage evidence is recoverable.
- Never guess one exact layer when the evidence only supports a layer range; use the narrowest honest range or `insufficient_evidence`.
- Never omit the primary mapping table. A report without the primary table is invalid and must be regenerated before delivery.
- Never silently drop kernel rows from the selected window. If a row cannot be localized beyond `insufficient_evidence`, keep it in the primary table or list it under `Unresolved Rows`.
- Never fold user-highlighted hot regions in the primary table.
- Never omit runtime-control, scheduler-overlap, speculative-preparation, or other non-compute kernel rows from the primary table; they must be covered row by row with the strongest honest code localization.
- Never summarize runtime-control work only as a phase-level paragraph when row-level mapping is required; overlap, future-map, copy, sync, metadata, and writeback rows must remain explicit.
- Never merge compute kernel rows in the primary mapping table, even if they share a kernel name, layer, substage, or owner; compute rows must remain one row per kernel `row_id`.
- Never fold compute kernel rows in the primary mapping table unless the user explicitly asks for reduced detail.
- Never ignore double-stream or multi-stream parallel context; if rows overlap in time or execute on different active streams, the report must state the per-row stream and parallel relationship when recoverable.
- The primary mapping table title is fixed as `Primary Kernel-to-Code Mapping Table`, or in Chinese `主 Kernel Row 到代码映射表`.
- Never use titles such as `Hot Kernel To Code Mapping`, `Focused Mapping`, or other hotspot-only wording for the primary mapping table.
- Never present a report as complete unless it explicitly states the selected-window row count, mapped primary-row count, `insufficient_evidence` row count, and any folded row ranges.
- Never compress the primary mapping table before compressing narrative analysis; if output pressure exists, preserve the primary table first, then runtime subphase summary, and only then shorten narrative sections.
- If no `Unresolved Rows` section is present, every kernel row in the selected window must be mapped to a concrete code line or the narrowest honest line range.
- Token Path Summary is phase-level summary only and must not replace row-level mapping.
- Runtime owner information is runtime-only row-level ownership evidence; it must cover all runtime-related rows in the selected window and may be rendered either as a dedicated `Runtime Owner Ledger` section or as explicit runtime-owner columns inside the primary mapping table.
- If a contrast or reference window is used to justify target-model compute behavior, double-stream evidence, or a last-layer candidate, the report must render a row-level compute table for the last-layer candidate neighborhood rather than keeping that compute evidence as narrative-only context.
- Keep performance commentary small and directly relevant to mapping.

## Trigger guidance

Trigger this skill when the user asks about:

- which code lines a profiling window corresponds to
- which code lines a kernel or kernel sequence corresponds to
- profiling-to-code alignment for SGLang on Ascend
- mapping a bubble boundary back to code transitions
- generating a per-kernel code mapping table
