# Output Format

When reporting results, use the following **de-duplicated** format to avoid overlapping entries:

## Summary
- Total API parameters in ServerArgs (`python/sglang/srt/server_args.py`): X
- Parameters documented in NPU support features: Y
- New parameters (not in NPU doc): Z (Local: L, Community: M, Previously Missing: P)
- Removed parameters (in NPU doc but not in ServerArgs): W
- Parameters with changed default values (neither new nor removed): C

## New API Parameters (Not in NPU Doc)
*This table includes all new parameters regardless of source. The "Change Source" column indicates Local, Community, or Previously Missing. The "Adapt Category" column indicates the auto-adaptation classification from Step 6.*

| Argument | Source File | Category | Default | NPU Adaptation Status | Adapt Category | Change Source | Notes |
|----------|-------------|----------|---------|----------------------|----------------|---------------|-------|

## Auto-Adaptation Recommendations (Suggested, Not Applied)

*The agent does NOT modify source files. All changes below are recommendations. The user must apply them manually.*

### Code Changes

```python
# Suggested: add to python/sglang/srt/hardware_backend/npu/utils.py, function set_default_server_args()

# Category 1: Auto-adaptable
args.<param_name> = <value>  # <reason>

# Category 2: Conditional (needs verification)
# TODO: verify on NPU - <reason>
if args.<param_name> is None:
    args.<param_name> = <suggested_value>
```

### Documentation Changes

Suggested additions to `docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`:

- `--<param-name>` -> category section, status: A2, A3 / Special For GPU
- ...

## Removed API Parameters
| Argument | Previous NPU Status | Change Source | Notes |
|----------|-------------------|---------------|-------|

## Default-Value Changes (Existing Parameters)
*Only list parameters that remain in both the codebase and NPU doc but whose default values or types have changed.*

| Argument | Old Default | New Default | NPU Status | Change Source | Notes |
|----------|-------------|-------------|------------|---------------|-------|

## NPU Adaptation Status Summary
| Status | Count |
|--------|-------|
| A2, A3 | X |
| Planned | X |
| Special For GPU | X |
| Experimental | X |
| New (needs triage) | X |
| Not in NPU documentation | X |

## Docker Verification Status

**This section MUST always be included in the report, even when verification was skipped.** Choose one of the following:

- Full Docker verification completed -- NPU hardware was available, test file was generated and executed in the Docker container.
- Light verification completed (no NPU hardware) -- Docker container was used, but only CLI/import checks were performed. NPU-dependent tests were skipped.
- Skipped: no NPU hardware available on host -- Neither Docker container nor NPU hardware was available. NPU code paths were verified via static code analysis only.
- Skipped: no Docker available -- Docker could not be used, but NPU hardware may be present.
- Pending NPU verification -- Test files were generated but not yet run on NPU hardware. List the generated test file paths.

Example:
```
Docker verification: Skipped - no NPU hardware available on host.
NPU code paths verified via static analysis only.
- /dev/davinci* devices: not found
- /usr/local/Ascend/driver: not found
- /usr/local/Ascend/firmware: not found
```

## Generated Test File

| File | Parameters Covered | Status |
|------|-------------------|--------|
| `test/registered/ascend/basic_function/parameter/test_npu_new_params_<date>.py` | param1, param2, ... | Passed / Failed / Pending |
