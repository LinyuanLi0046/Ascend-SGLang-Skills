---
name: npu-api-adaptation-checker
description: Checks new/removed API parameters and their NPU adaptation status. Invoke when user asks about API parameter changes, NPU compatibility, Ascend NPU adaptation verification, verifying a specific parameter on NPU, or checking server argument differences.
---

# NPU API Adaptation Checker

This skill analyzes the SGLang codebase to identify newly added and removed API parameters (server arguments), checks their adaptation status on Ascend NPU, and performs verification using the specified Docker image.

## Key Source File

- **ServerArgs definition**: `python/sglang/srt/server_args.py`
- **NPU support features doc (old)**: `docs/platforms/ascend/ascend_npu_support_features.md`
- **NPU support features doc (new)**: `docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`
- **Server arguments doc (old)**: `docs/advanced_features/server_arguments.md`
- **Server arguments doc (new)**: `docs_new/docs/advanced_features/server_arguments.mdx`
- **NPU test cases**: `test/registered/ascend/`
- **NPU default settings**: `python/sglang/srt/hardware_backend/npu/utils.py`
- **NPU model weights constants**: `python/sglang/test/ascend/test_ascend_utils.py`
- **Parameter testing procedure**: `.agents/skills/npu-api-adaptation-checker/references/parameter-testing.md`

## Core Rule: Suggestions Only, No Auto-Modification

**The agent MUST NOT modify source code, documentation, or test files without the user's explicit confirmation.** This skill is an analysis and recommendation tool. When code/doc changes are identified:

- Present the suggested changes clearly in the report
- Prefix all code patches with "Suggested change (not applied):"
- Do NOT apply edits to any of these files without user approval:
  - `python/sglang/srt/server_args.py`
  - `python/sglang/srt/hardware_backend/npu/utils.py`
  - `docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`
  - Any other source or documentation file

**The only file the agent MAY create/modify without asking is the verification test file** (see Phase C below). Test files are generated artifacts of the analysis, not modifications to the existing codebase.

## Workflow

**IMPORTANT: You are NOT done with this skill until every step below is complete. Starting a step is not the same as finishing it. If NPU hardware is found in Step 1, Step 3 is NOT optional -- it is the core deliverable. Do NOT list Docker verification as an "action item" or "next step" at the end. Execute it.**

### Phase A: Setup (do first, before any analysis)

1. **Check hardware availability** -- Run `ls /dev/davinci*` and `ls /usr/local/Ascend/driver`. This determines everything that follows. See [references/docker-verification.md](references/docker-verification.md) Step 0.

   - **If NPU hardware IS found**: Read the "After the Check" section in docker-verification.md, note the GREEN path, and proceed to Phase B below. After Phase B's code analysis, Phase C Docker verification is MANDATORY.
   - **If NPU hardware is NOT found**: Note the RED path. Phase C will be skipped. Proceed to Phase B code analysis only.

2. **Prepare Docker container (if hardware found)** -- Before doing any code analysis, pull the image and launch the container in the background. This runs in parallel with your code analysis and saves time. Use Full Docker Verification Steps 1-2 (pull image, docker run -itd). See [references/docker-verification.md](references/docker-verification.md).

### Phase B: Code Analysis (read-only, suggestions only)

3. **Analyze API parameter changes** -- See [references/analysis-steps.md](references/analysis-steps.md) for Steps 1-7. This is a reading/code-search phase. **All code/doc changes identified in Steps 6-7 are suggestions only.** Do NOT edit source files or docs. Present recommendations in the report.

### Phase C: Runtime Verification via Generated Test File

4. **Generate the verification test file** -- Create a single Python test file under `test/registered/ascend/basic_function/parameter/` that tests ALL new NPU-applicable parameters (Category 1 and Category 2). The test file must:

   - Use `CustomTestCase` from `sglang.test.test_utils`
   - Use `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` for the model (smallest, fastest)
   - Test each new parameter by launching a server with it and verifying inference works
   - Use `popen_launch_server` for process management (it handles `ready to roll!` detection automatically)
   - Include `--attention-backend ascend` and `--disable-cuda-graph` in `other_args`
   - Register with `register_npu_ci` for CI integration

   Follow the template in [references/parameter-testing.md](references/parameter-testing.md). See [references/test-generation.md](references/test-generation.md) for file naming and location conventions.

   **This is the only file the agent may create without user approval.** It is a generated artifact, not a modification of existing code.

5. **Run the generated test in Docker** -- If NPU hardware is available:
   ```bash
   docker cp test/registered/ascend/basic_function/parameter/test_npu_<name>.py npu-api-adaptation-checker:/sglang/test/registered/ascend/basic_function/parameter/
   docker exec npu-api-adaptation-checker bash -c "export PYTHONPATH=/sglang/python:$PYTHONPATH && cd /sglang && python -m pytest test/registered/ascend/basic_function/parameter/test_npu_<name>.py -v"
   ```
   If hardware is NOT available, report "Skipped: no NPU hardware" and mark the test as "Pending NPU verification".

### Phase D: Deliverables

6. **Report results** -- See [references/output-format.md](references/output-format.md). Must include:
   - Docker Verification Status section
   - A "Suggested Changes" section listing all recommended code/doc edits (with diffs)
   - The generated test file path and results

### Completion Checklist (MUST be verified before ending)

Before you end your turn, check:

- [ ] Phase A complete: Hardware check ran, container launched (if hardware found)
- [ ] Phase B complete: All 7 analysis sub-steps executed. **No source/doc files modified** -- all changes are suggestions only.
- [ ] Phase C complete: Test file generated. If hardware found: test file copied to container and executed. If hardware not found: test file generated with "Pending NPU verification" note.
- [ ] Phase D complete: Report written with Docker Verification Status, Suggested Changes, and test file path.

If Phase C is unchecked and the hardware check found NPU devices, **do not end your turn**. Execute Phase C now.

## Important Notes

- The NPU support features doc (preferably the new `.mdx` version) should be updated whenever new parameters are verified on NPU. **The agent should suggest the updates but not apply them.**
- When adding new NPU-supported parameters to the doc, follow the existing table format with columns: Argument, Defaults, Options, Server supported.
- **`_handle_npu_backends()`** is the method in `python/sglang/srt/server_args.py` where NPU-specific backend logic is handled. All NPU adaptation code for server arguments goes here or in `set_default_server_args()`.
- `set_default_server_args()` in `python/sglang/srt/hardware_backend/npu/utils.py` is where simple NPU default values are set. This is the preferred location for Category 1 auto-adaptation code.
- **Auto-adaptation (Step 6)**: New parameters are classified into 3 categories. **The agent only recommends code changes -- it does not apply them.**
  - **Category 1 (Auto-Adaptable)**: Direct code generation -- force-disable CUDA features, force-set NPU backends, set NPU defaults.
  - **Category 2 (Conditional)**: Generate template with `# TODO: verify on NPU` -- depends on runtime conditions like NPU memory, model architecture, tp_size.
  - **Category 3 (Not Auto-Adaptable)**: Mark as "Special For GPU" -- requires NPU kernel support that may not exist.
- NPU test cases in `test/registered/ascend/` follow the pattern: `test_npu_<feature>.py` using `CustomTestCase` from `sglang.test.test_utils`.
- **Always use model weight path constants from `sglang.test.ascend.test_ascend_utils`** (e.g., `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH`) rather than hardcoded local paths. These constants resolve to the correct path depending on whether the test runs in CI (`/root/.cache/modelscope/hub/models/`) or locally (`/home/weights/`).
- The Docker image tag format for NPU is: `{version}-cann{cann_version}-{hardware}`, e.g., `cann8.5.0-a3-B131`.
- **The agent MUST generate a verification test file** that covers all new NPU-applicable parameters. This is the primary deliverable of Phase C. See [references/parameter-testing.md](references/parameter-testing.md) for the template.
- Generated test files must follow the existing conventions: use `CustomTestCase`, `register_npu_ci`, `popen_launch_server`, and include the standard NPU server arguments (`--attention-backend ascend`, `--disable-cuda-graph`).
- Always verify generated tests by running them inside the Docker container with the specified image -- **but only when NPU hardware is available**. If the host lacks NPU devices, generate the test file and mark it as "Pending NPU verification" in the report. Do NOT attempt to run NPU-dependent tests on a CPU-only host.
- **Before generating a test, select a model from the confirmed availability mapping in [references/test-generation.md](references/test-generation.md)**. Import the appropriate constant from `sglang.test.ascend.test_ascend_utils` rather than hardcoding paths.
- **Docker verification**: When the hardware check finds NPU devices AND driver/firmware, you MUST execute full Docker verification immediately -- it is NOT optional. Only skip when hardware is absent. If skipping, always explicitly report "Skipped: no NPU hardware available". Do NOT silently skip.
