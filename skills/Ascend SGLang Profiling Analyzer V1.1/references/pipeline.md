# Pipeline

This file defines only stage order, stage intent, required inputs, and stage outputs.
For rule meaning, read [design_principles.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/design_principles.md).
For output schema, read [output_contract.md](file:///d:/ai-0427/Ascend%20SGLang%20Profiling%20Analyzer%20V1.1/references/output_contract.md).

## Overview

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
-> RUNTIME_ROW_COMPLETENESS_AUDIT
-> PROFILING_TO_CODE_ALIGNMENT
-> KERNEL_CODE_MAPPING_TABLE
-> LIGHT_PERFORMANCE_REVIEW
-> RENDER
```

## Stage details

### 0. INPUT_CONTRACT_VALIDATION

Input:

- profiling root path
- `window_start_ns`
- `window_end_ns`
- model weight path
- local code repository path
- launch command
- optional benchmark command / result

Work:

- verify that all required contract fields are present
- verify that the profiling input is a profiling root path rather than a sliced file or derived-artifact path
- verify that the code repository path, model weight path, and launch command are all present
- record optional benchmark inputs when available

Output:

- validated input contract
- missing required inputs

### 1. RAW_PROFILING_DISCOVERY

Input:

- profiling root path

Work:

- locate `ASCEND_PROFILER_OUTPUT` under the provided profiling root path
- locate full raw `trace_view.json`
- locate full raw `kernel_details.csv`
- reject sliced-only or summary-only profiling inputs as the primary evidence source
- record the discovered raw profiling paths

Output:

- raw profiling paths
- profiler-output path
- discovery failures when applicable

### 2. WINDOW_VALIDATION

Input:

- `window_start_ns`
- `window_end_ns`
- raw profiling paths

Work:

- validate that the ns timestamps are present and ordered
- treat the window as one token-level inference window
- prefer decode-time windows
- for MTP / speculative cases, require a complete `verify` cycle with matching draft work
- stop and ask for corrected timestamps when the window is incomplete or ambiguous

Output:

- accepted token window
- window validation result

### 3. FIXED_PREPROCESSING

Input:

- raw `trace_view.json`
- raw `kernel_details.csv`
- accepted token window

Work:

- run `scripts/slice_profiling.py` on the full raw trace using the validated ns window
- run `scripts/process_profiling.py` on the sliced trace
- run `scripts/slice_kernel_csv.py` on the full raw kernel csv using the same ns window
- run `scripts/process_kernel.py` on the sliced kernel csv
- keep the generated sliced trace, sliced kernel csv, and derived summaries as the reusable evidence layer for later stages

Output:

- fixed preprocessing artifacts

### 4. CONTEXT_INVENTORY

Input:

- validated input contract
- fixed preprocessing artifacts

Work:

- inventory code repository path, launch command, benchmark inputs, and model-weight-path-derived files
- identify config files such as `config.json` and `quant_model_description.json`
- record evidence gaps immediately

Output:

- evidence inventory
- evidence gaps

### 5. LAUNCH_CONTEXT_CHECK

Input:

- launch script / launch command
- optional environment variables

Work:

- confirm which launch path produced the profiling run
- identify important runtime flags
- decide whether strict code localization is allowed

Output:

- launch context summary
- strict-localization gate

### 6. MODEL_STRUCTURE_AND_WEIGHT_LAYOUT_CHECK

Input:

- model weight path
- `config.json` when available under the model weight path
- `quant_model_description.json` when available under the model weight path

Work:

- extract architecture, layer count, hidden size, FFN size, attention method, and MoE scale
- extract representative submodule-to-format mappings
- build expected kernel-family hints for attention, dense MLP, MoE experts, and output-head regions
- record repeated-layer constraints for later folding

Reading strategy:

- read `config.json` in full when practical
- do not fully expand large `quant_model_description.json` files by default
- confirm schema first, then sample representative submodule families such as `self_attn`, `mlp.experts`, `lm_head`, `embed_tokens`, and `model.norm`

Output:

- model-structure constraints
- weight-layout hints

### 7. TOKEN_PATH_RECONSTRUCTION

Input:

- launch context
- code
- model constraints

Work:

- reconstruct one token path step by step
- keep intermediate stages
- when model-forward work is present, reconstruct the repeated layer template instead of stopping at `full forward`
- record expected per-layer substages when recoverable, such as attention preprocess, attention body, TP communication, dense MLP, MoE, residual merge, final norm, lm_head, and sampling tail
- record expected trace signatures
- record expected kernel signatures
- record candidate file / function / line owners
- record candidate layer ordinal or layer-range anchors when recoverable
- record candidate output-head anchors that may disambiguate the last repetitive layer
- record whether a contrast or reference window will be needed to render the last-layer compute neighborhood row by row

Output:

- token-path model

### 8. RUNTIME_CONTROL_PATH_RECONSTRUCTION

Input:

- launch context
- code

Work:

- reconstruct scheduler, manager, overlap, future-map, copy, sync, and submission stages
- identify runtime-control stages before and after model forward
- record semantic runtime subphases
- record candidate runtime owners for lightweight data-reorganization clusters
- record handoff points between runtime-control and model-forward regions

Mandatory semantic checks for speculative, overlap-heavy, or scheduler-driven runs:

- future-slot or future-index allocation
- future-buffer resolution into the current batch
- result storeback to future buffers
- delayed sampling or delayed materialization
- stream or host synchronization that guards the above

Output:

- runtime-control path model
- runtime subphase list

### 9. RUNTIME_GROUNDING

Input:

- token path
- runtime path
- launch context

Work:

- infer expected phases
- infer expected stream roles when recoverable
- infer expected decode or verify critical path
- infer likely host-side preparation or submission stages

Output:

- grounded phase expectations

### 10. TRACE_STRUCTURE_ANALYSIS

Input:

- fixed preprocessing trace artifacts

Work:

- derive phase windows, dominant tracks, root spans, child spans, suspicious serial regions, and useful markers

Output:

- trace-side structure view

### 11. KERNEL_DEVICE_ANALYSIS

Input:

- fixed preprocessing kernel artifacts

Work:

- derive per-stream busy and wall
- derive top kernels by duration and total cost
- derive bubble candidates, neighbors, and obvious wait-heavy rows

Output:

- kernel-side structure view

### 12. STRUCTURE_SYNTHESIS

Input:

- trace-side structure view
- kernel-side structure view
- grounded phase expectations

Work:

- map suspicious spans to kernel neighborhoods
- map bubble windows to phase transitions
- identify likely layer boundaries
- identify repeated per-layer kernel patterns
- infer exact layer ordinal or the narrowest honest layer range when repeated model-forward structure is present
- identify output-head anchors that can disambiguate the immediately preceding repetitive layer block
- mark whether a model-forward neighborhood is a last-layer candidate when evidence supports it
- identify whether wait-like trace regions coincide with real device idle
- identify whether neighborhoods are better explained by runtime-control or model-forward ownership
- if the selected primary window is runtime-heavy, identify whether a separate contrast or reference compute window is needed for the last-layer candidate neighborhood

Output:

- combined structural hypothesis

### 13. GENERIC_CLUSTER_OWNER_COMPETITION

Input:

- combined structural hypothesis
- runtime-control path model
- token-path model

Work:

- enumerate scheduler / overlap / future-map owners first
- compare candidates by phase match, neighboring-pattern match, buffer provenance, and code-path reachability
- keep `runtime_future_resolve` as an explicit candidate when overlap, future-map, or speculative scheduling is present
- split clusters that span multiple semantic regions
- downgrade to `adjacent_stage_only` or `insufficient_evidence` when no owner clearly dominates

This stage is mandatory for clusters dominated by `GatherV2`, `Slice`, `Index`, `Cast`, `ViewCopy`, `Arange`, `Transpose`, `Copy`, and similar data-reorganization work.

Output:

- owner-ranked generic clusters

### 13a. RUNTIME_ROW_COMPLETENESS_AUDIT

Input:

- owner-ranked generic clusters
- runtime-control path model
- kernel-side structure view

Work:

- verify that runtime-control, scheduler-overlap, speculative-preparation, copy, sync, metadata, and writeback rows remain visible row by row
- verify that each runtime row has completed owner competition before line-level localization is attempted
- verify that runtime rows are not silently replaced by a phase-only narrative
- verify that runtime rows participating in double-stream or multi-stream overlap preserve stream and concurrent-row context when recoverable
- prepare the runtime-owner information required by the output contract, either as a dedicated ledger or as explicit primary-table columns

Fail gate:

- if any runtime row in the selected window is missing from the required runtime-owner information, stop and regenerate before delivery
- if a runtime row has only coarse phase ownership but no honest code-localization ceiling, do not upgrade it to line-level localization

Output:

- runtime row completeness audit
- runtime-owner information

### 14. PROFILING_TO_CODE_ALIGNMENT

Input:

- token-path model
- runtime-control path model
- combined structural hypothesis
- owner-ranked generic clusters

Work:

- align by local execution pattern rather than by one kernel name
- use neighboring kernels, adjacent spans, phase context, tensor-shape / dtype transitions, and buffer provenance together
- check runtime-control owners before speculative-prep owners for generic clusters
- split mixed-owner neighborhoods
- preserve one ownership claim per kernel row instead of collapsing neighboring compute rows into one explanation
- keep stream and concurrent-row context for rows participating in double-stream or multi-stream execution when recoverable
- for model-forward rows, recover the strongest honest layer/substage ownership instead of stopping at coarse `full forward`
- if exact layer ordinal is not recoverable, output the narrowest honest layer range
- use output-head anchors to judge whether a repetitive model-forward block is a last-layer candidate when such anchors are observed
- if target-model compute ownership is justified by a contrast or reference window, recover a row-level last-layer candidate neighborhood rather than leaving that compute evidence as narrative-only context
- downgrade unsupported claims to `insufficient_evidence`
- recover exact line number when possible, otherwise use the narrowest honest line range

Fail gate:

- if a runtime row has not passed runtime-row completeness audit, do not emit line-level localization for that row
- if compute rows are merged by layer, operator family, or repeated-pattern summary, regenerate the mapping as one row per kernel `row_id`
- if concurrent execution exists but per-row stream or parallel context is omitted, regenerate the mapping with explicit parallel annotations
- if the report relies on a contrast or reference window for target-model compute interpretation but does not render the last-layer compute neighborhood row by row, regenerate the report with a dedicated compute table

Output:

- per-kernel ownership and code localization claims

### 15. KERNEL_CODE_MAPPING_TABLE

Input:

- per-kernel ownership and code localization claims

Work:

- render one primary row per kernel `row_id`
- include raw kernel identity, phase, stream, overlap context, concurrent rows or parallel group, owner class, semantic subphase, layer instance role, layer ordinal or layer range, substage, last-layer-candidate status, code region, localization ceiling, why this owner wins, why nearby owners do not win, mapping strength, evidence refs, and confidence
- keep repeated kernel names expanded rather than merged
- keep compute rows expanded rather than merged
- do not fold compute rows in the primary mapping table unless the user explicitly requests reduced detail
- never fold user-highlighted hot regions in the primary table
- keep required runtime subphases explicit for speculative or overlap-heavy windows
- produce coverage accounting for the selected kernel-row window, including mapped rows, `insufficient_evidence` rows, and folded row ranges
- prepare an `Unresolved Rows` list whenever any selected rows cannot be localized beyond `insufficient_evidence`
- ensure all runtime rows required by the runtime-owner information also appear in the primary mapping table
- render a `Last-Layer Compute Row Mapping Table` when a contrast or reference window is required for target-model compute ownership

Required runtime subphase coverage labels:

- `runtime_future_resolve`
- `runtime_metadata_prepare`
- `target_verify_body`
- `draft_cache_prepare`
- `verify_accept_or_sample`
- `draft_extend_v2_body`
- `tree_cache_update`
- `scheduler_postprocess_or_writeback`
- `final_norm_lm_head`

Output:

- runtime-owner information
- primary mapping table
- last-layer compute row mapping table when required
- coverage summary
- runtime subphase summary
- repeated-layer folding notes when used
- unresolved rows when required

### 16. LIGHT_PERFORMANCE_REVIEW

Input:

- mapping table
- structural evidence

Work:

- keep only small observations that directly help mapping
- allowed topics: obvious bubbles, obvious serialization, obvious missing fused paths, obvious missing overlap
- do not turn this stage into a bug inventory, host-bound finding set, or TPOT prioritization section

Output:

- short mapping-relevant performance notes

### 17. RENDER

Input:

- all validated report sections

Work:

- render one Markdown report in Chinese
- ensure the primary table can be joined back to sliced kernel csv by `row_id`
- ensure the report remains centered on the kernel-to-code table
- preserve sections in this priority order when output pressure exists: primary mapping table, coverage summary, runtime subphase summary, then narrative analysis
- treat a missing primary mapping table as a render failure rather than a valid shortened report
- treat missing runtime-owner information as a render failure for speculative, overlap-heavy, or scheduler-driven runs
- do not silently omit rows; unresolved rows must remain in the primary table or be declared in `Unresolved Rows`

Output:

- `kernel_code_mapping_report_<case_name>.md`
