---
name: "npu-api-adaptation-checker"
description: "Checks new/removed API parameters and their NPU adaptation status. Invoke when user asks about API parameter changes, NPU compatibility, Ascend NPU adaptation verification, verifying a specific parameter on NPU, or checking server argument differences."
---

# NPU API Adaptation Checker

This skill analyzes the SGLang codebase to identify newly added and removed API parameters (server arguments), checks their adaptation status on Ascend NPU, and performs verification using the specified Docker image.

## Key Source Files

- **ServerArgs definition (LLM)**: `python/sglang/srt/server_args.py`
- **ServerArgs definition (Multimodal)**: `python/sglang/multimodal_gen/runtime/server_args.py`
- **NPU support features doc (old)**: `docs/platforms/ascend/ascend_npu_support_features.md`
- **NPU support features doc (new)**: `docs_new/docs/hardware-platforms/ascend-npus/ascend_npu_support_features.mdx`
- **Server arguments doc (old)**: `docs/advanced_features/server_arguments.md`
- **Server arguments doc (new)**: `docs_new/docs/advanced_features/server_arguments.mdx`
- **NPU test cases**: `test/registered/ascend/`
- **NPU default settings**: `python/sglang/srt/hardware_backend/npu/utils.py`

## Workflow

1. **Analyze API parameter changes** — See [references/analysis-steps.md](references/analysis-steps.md) for Steps 1-7
2. **Verify using Docker** — See [references/docker-verification.md](references/docker-verification.md) for container setup and verification
3. **Report results** — See [references/output-format.md](references/output-format.md) for the output template
4. **Generate tests if needed** — See [references/test-generation.md](references/test-generation.md) for test file templates and conventions

## Important Notes

- The NPU support features doc (preferably the new `.mdx` version) should be updated whenever new parameters are verified on NPU.
- When adding new NPU-supported parameters to the doc, follow the existing table format with columns: Argument, Defaults, Options, Server supported.
- The `_handle_npu_backends()` method in each ServerArgs file is the primary entry point for NPU-specific argument handling. Check both `python/sglang/srt/server_args.py` and `python/sglang/multimodal_gen/runtime/server_args.py`.
- `set_default_server_args()` in `python/sglang/srt/hardware_backend/npu/utils.py` is where simple NPU default values are set. This is the preferred location for Category 1 auto-adaptation code.
- **Auto-adaptation (Step 6)**: New parameters are classified into 3 categories:
  - **Category 1 (Auto-Adaptable)**: Direct code generation — force-disable CUDA features, force-set NPU backends, set NPU defaults.
  - **Category 2 (Conditional)**: Generate template with `# TODO: verify on NPU` — depends on runtime conditions like NPU memory, model architecture, tp_size.
  - **Category 3 (Not Auto-Adaptable)**: Mark as "Special For GPU" — requires NPU kernel support that may not exist.
- NPU test cases in `test/registered/ascend/` follow the pattern: `test_npu_<feature>.py` using `CustomTestCase` from `sglang.test.test_utils`.
- The Docker image tag format for NPU is: `{version}-cann{cann_version}-{hardware}`, e.g., `cann8.5.0-a3-B131`.
- **When no existing test covers a new API parameter, you MUST auto-generate a test file** following the templates in [references/test-generation.md](references/test-generation.md) and place it in the correct subdirectory under `test/registered/ascend/`.
- Generated test files must follow the existing conventions: use `CustomTestCase`, `register_npu_ci`, `popen_launch_server`, and include the standard NPU server arguments (`--attention-backend ascend`, `--disable-cuda-graph`).
- Always verify generated tests by running them inside the Docker container with the specified image.
- **Before generating a test, select a model from the confirmed local availability mapping.** The Docker container mounts `/home:/home`, so all models under `/home/weights/` on the host are accessible at `/home/weights/` inside the container.
- **Models NOT available locally**: `MiniCPM-o-2_6` (replace with `Qwen3-VL-8B-Instruct` for multimodal, or `Qwen2.5-7B-Instruct` for warmup), `deepseek-coder-1.3b-base` (replace with `Qwen3-0.6B` or `Llama-3.2-1B-Instruct`), `AI-ModelScope/Llama-3.1-8B-Instruct` (use `/home/weights/LLM-Research/Meta-Llama-3.1-8B-Instruct` instead).
