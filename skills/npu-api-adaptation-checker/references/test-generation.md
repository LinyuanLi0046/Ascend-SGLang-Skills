# Test File Generation

When a new API parameter has **no existing test case** in `test/registered/ascend/`, you MUST generate a test file automatically. Follow the patterns and conventions below.

## Step 1: Determine Test File Location

Based on the parameter's category, place the test in the appropriate subdirectory:

| Parameter Category | Test Directory |
|---|---|
| Server parameter (e.g., `--log-level`, `--warmups`, `--chunked-prefill-size`) | `test/registered/ascend/basic_function/parameter/` |
| Backend parameter (e.g., `--attention-backend`, `--sampling-backend`) | `test/registered/ascend/basic_function/backends/` |
| Interface/API parameter (e.g., `--api-key`, `--enable-cache-report`) | `test/registered/ascend/interface/` |
| Quantization parameter (e.g., `--quantization`, `--kv-cache-dtype`) | `test/registered/ascend/basic_function/quant/` |
| Runtime/optimization parameter (e.g., `--tp-size`, `--cuda-graph-max-bs`) | `test/registered/ascend/basic_function/runtime_opts/` |
| Offloading parameter | `test/registered/ascend/basic_function/offloading/` |
| Speculative inference parameter | `test/registered/ascend/basic_function/speculative_inference/` |
| HiCache parameter | `test/registered/ascend/basic_function/HiCache/` |

## Step 2: File Naming Convention

Name the file as `test_npu_<parameter_name_with_underscores>.py`.

Examples:
- `--enable-dynamic-chunking` → `test_npu_dynamic_chunking.py`
- `--moe-runner-backend` → `test_npu_moe_runner_backend.py`
- `--prefill-delayer-max-delay-passes` → `test_npu_prefill_delayer_max_delay_passes.py`

## Step 3: Test File Templates

**Critical**: All generated tests MUST import model weight path constants from `sglang.test.ascend.test_ascend_utils` instead of hardcoding local paths like `/home/weights/...`. These constants resolve to the correct path depending on whether the test runs in CI or locally. See Step 4 for the mapping.

### Template A: Boolean Flag Parameter (e.g., `--enable-xxx`)

```python
import unittest

import requests

from sglang.srt.utils import kill_process_tree
from sglang.test.ascend.test_ascend_utils import LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
from sglang.test.ci.ci_register import register_npu_ci
from sglang.test.test_utils import (
    DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
    DEFAULT_URL_FOR_TEST,
    CustomTestCase,
    popen_launch_server,
)

register_npu_ci(
    est_time=200,  # Adjust based on model size; 200s for 1B models, 400s for 8B
    suite="nightly-1-npu-a3",
    nightly=True,
)


class TestNpu<ParameterName>(CustomTestCase):
    """Testcase: Verify that the --<parameter-name> parameter works correctly on NPU.

    [Test Category] Parameter
    [Test Target] --<parameter-name>
    """

    model = LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
    base_url = DEFAULT_URL_FOR_TEST

    @classmethod
    def setUpClass(cls):
        other_args = [
            "--<parameter-name>",
            "--attention-backend",
            "ascend",
            "--disable-cuda-graph",
            "--mem-fraction-static",
            "0.8",
        ]
        cls.process = popen_launch_server(
            cls.model,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=other_args,
        )

    @classmethod
    def tearDownClass(cls):
        kill_process_tree(cls.process.pid)

    def test_<parameter_name>_enabled(self):
        response = requests.get(f"{self.base_url}/server_info")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("<parameter_name>"))

        response = requests.post(
            f"{self.base_url}/generate",
            json={
                "text": "The capital of France is",
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": 32,
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Paris", response.text)


if __name__ == "__main__":
    unittest.main()
```

### Template B: String/Choice Parameter (e.g., `--xxx-backend=ascend`)

```python
import unittest

import requests

from sglang.srt.utils import kill_process_tree
from sglang.test.ascend.test_ascend_utils import LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
from sglang.test.ci.ci_register import register_npu_ci
from sglang.test.test_utils import (
    DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
    DEFAULT_URL_FOR_TEST,
    CustomTestCase,
    popen_launch_server,
)

register_npu_ci(
    est_time=200,
    suite="nightly-1-npu-a3",
    nightly=True,
)


class TestNpu<ParameterName>(CustomTestCase):
    """Testcase: Verify that the --<parameter-name> parameter works correctly with each supported option on NPU.

    [Test Category] Parameter
    [Test Target] --<parameter-name>
    """

    model = LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
    base_url = DEFAULT_URL_FOR_TEST

    @classmethod
    def setUpClass(cls):
        other_args = [
            "--<parameter-name>",
            "<option_value>",
            "--attention-backend",
            "ascend",
            "--disable-cuda-graph",
            "--mem-fraction-static",
            "0.8",
        ]
        cls.process = popen_launch_server(
            cls.model,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=other_args,
        )

    @classmethod
    def tearDownClass(cls):
        kill_process_tree(cls.process.pid)

    def test_<parameter_name>_with_<option>(self):
        response = requests.get(f"{self.base_url}/server_info")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("<parameter_name>"), "<option_value>")

        response = requests.post(
            f"{self.base_url}/generate",
            json={
                "text": "The capital of France is",
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": 32,
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Paris", response.text)


if __name__ == "__main__":
    unittest.main()
```

### Template C: Integer/Float Parameter (e.g., `--xxx-size=4`)

```python
import unittest

import requests

from sglang.srt.utils import kill_process_tree
from sglang.test.ascend.test_ascend_utils import LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
from sglang.test.ci.ci_register import register_npu_ci
from sglang.test.test_utils import (
    DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
    DEFAULT_URL_FOR_TEST,
    CustomTestCase,
    popen_launch_server,
)

register_npu_ci(
    est_time=200,
    suite="nightly-1-npu-a3",
    nightly=True,
)


class TestNpu<ParameterName>(CustomTestCase):
    """Testcase: Verify that the --<parameter-name> parameter works correctly on NPU.

    [Test Category] Parameter
    [Test Target] --<parameter-name>
    """

    model = LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
    base_url = DEFAULT_URL_FOR_TEST

    @classmethod
    def setUpClass(cls):
        other_args = [
            "--<parameter-name>",
            "<value>",
            "--attention-backend",
            "ascend",
            "--disable-cuda-graph",
            "--mem-fraction-static",
            "0.8",
        ]
        cls.process = popen_launch_server(
            cls.model,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=other_args,
        )

    @classmethod
    def tearDownClass(cls):
        kill_process_tree(cls.process.pid)

    def test_<parameter_name>_value(self):
        response = requests.get(f"{self.base_url}/server_info")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("<parameter_name>"), <expected_value>)

        response = requests.post(
            f"{self.base_url}/generate",
            json={
                "text": "The capital of France is",
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": 32,
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Paris", response.text)


if __name__ == "__main__":
    unittest.main()
```

## Step 4: Model Path Constants and Availability

### CRITICAL: Use Constants, Not Hardcoded Paths

**Always import model weight path constants from `sglang.test.ascend.test_ascend_utils`**. These constants automatically resolve to the correct path:
- In CI: `/root/.cache/modelscope/hub/models/<model_name>`
- Locally: `/home/weights/<model_name>`

Never hardcode paths like `"/home/weights/LLM-Research/Llama-3.2-1B-Instruct"` in generated tests. Always use the constant:

```python
from sglang.test.ascend.test_ascend_utils import LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
# ...
model = LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
```

### Model Weight Constants Reference

Below is a reference of commonly used model weight constants from `sglang.test.ascend.test_ascend_utils`. Always prefer constants from this module over hardcoded paths.

| Constant Name | CI Path (under `MODEL_WEIGHTS_DIR`) | Local Path (`/home/weights/`) | Notes |
|---|---|---|---|
| `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` | `LLM-Research/Llama-3.2-1B-Instruct` | `/home/weights/LLM-Research/Llama-3.2-1B-Instruct` | ✅ Best for 1-NPU basic tests (smallest) |
| `META_LLAMA_3_1_8B_INSTRUCT` | `LLM-Research/Meta-Llama-3.1-8B-Instruct` | `/home/weights/LLM-Research/Meta-Llama-3.1-8B-Instruct` | ✅ Best for 1-NPU accuracy tests |
| `QWEN2_5_7B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen2.5-7B-Instruct` | `/home/weights/Qwen/Qwen2.5-7B-Instruct` | ✅ Available |
| `QWEN3_8B_WEIGHTS_PATH` | `Qwen/Qwen3-8B` | `/home/weights/Qwen/Qwen3-8B` | ✅ Available |
| `QWEN3_0_6B_WEIGHTS_PATH` | `Qwen/Qwen3-0.6B` | `/home/weights/Qwen3-0.6B` | ✅ Best for FIM/completion tests |
| `QWEN3_VL_8B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen3-VL-8B-Instruct` | `/home/weights/Qwen/Qwen3-VL-8B-Instruct` | ✅ Best for multimodal tests |
| `QWEN2_5_VL_7B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen2.5-VL-7B-Instruct` | `/home/weights/Qwen2.5-VL-7B-Instruct` | ✅ Available |
| `GLM_4_5V_WEIGHTS_PATH` | `ZhipuAI/GLM-4.5V` | `/home/weights/GLM-4.5V` | ✅ Available |
| `BAICHUAN2_13B_CHAT_WEIGHTS_PATH` | `baichuan-inc/Baichuan2-13B-Chat` | `/home/weights/Baichuan2-13B` | ✅ Available |
| `MINICPM_O_2_6_WEIGHTS_PATH` | `openbmb/MiniCPM-o-2_6` | — | ❌ Not available locally; use `QWEN3_VL_8B_INSTRUCT_WEIGHTS_PATH` for multimodal, `QWEN2_5_7B_INSTRUCT_WEIGHTS_PATH` for warmup |
| `DEEPSEEK_CODER_1_3_B_BASE_PATH` | `deepseek-ai/deepseek-coder-1.3b-base` | — | ❌ Not available locally; use `QWEN3_0_6B_WEIGHTS_PATH` for FIM, `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` for basic |

> **Note**: `LLAMA_3_1_8B_INSTRUCT_WEIGHTS_PATH` points to `AI-ModelScope/Llama-3.1-8B-Instruct` (CI path), but locally use `META_LLAMA_3_1_8B_INSTRUCT` which resolves to `LLM-Research/Meta-Llama-3.1-8B-Instruct` (the confirmed local path).

### Pre-generation Model Selection Guidelines

- For 1-NPU basic tests: `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` (smallest, fastest)
- For 1-NPU accuracy tests: `META_LLAMA_3_1_8B_INSTRUCT`
- For multimodal tests: `QWEN3_VL_8B_INSTRUCT_WEIGHTS_PATH`
- For FIM/completion tests: `QWEN3_0_6B_WEIGHTS_PATH`
- For warmup tests: `MINICPM_O_2_6_WEIGHTS_PATH` (CI only) or `QWEN2_5_7B_INSTRUCT_WEIGHTS_PATH` (local)

## Step 5: CI Registration Convention

```python
register_npu_ci(
    est_time=200,            # Estimated test time in seconds; 200s for small models, 400s for 7-8B
    suite="nightly-1-npu-a3",  # Suite name: nightly-<num_npu>-npu-<hardware>
    nightly=True,           # Whether to run in nightly CI
)
```

The full signature of `register_npu_ci` is:
```python
def register_npu_ci(
    est_time: float,
    suite: Optional[str] = None,
    nightly: bool = False,
    disabled: Optional[str] = None,
    *,
    stage: Optional[str] = None,
    runner_config: Optional[str] = None,
):
```

Suite naming convention:
- `nightly-1-npu-a3` — Single NPU test on Atlas A3
- `nightly-4-npu-a3` — 4 NPU test on Atlas A3
- `stage-b-test-1-npu-a2` — Stage B test on Atlas A2

## Step 6: Required NPU Server Arguments

All NPU test cases MUST include these arguments in `other_args`:

```python
other_args = [
    "--attention-backend", "ascend",   # Required: NPU attention backend
    "--disable-cuda-graph",             # Required: Disable CUDA graph on NPU
    "--mem-fraction-static", "0.8",     # Recommended: Memory fraction for NPU
]
```

## Step 7: Verification Checklist for Generated Test

After generating the test file, verify:

1. **Server reaches ready state**: Launch server in background, poll for `ready to roll!`. Do NOT kill on ERROR log lines -- NPU environments emit harmless ERRORs from missing optional modules. Only `ready to roll!` + successful inference matters.
2. **Parameter takes effect**: Check `/server_info` endpoint to confirm the parameter value is applied
3. **Inference works**: Send a generate request; response should be valid
4. **Test passes**: Run `python3 -m pytest <test_file> -v` inside the Docker container

## Step 8: Running Generated Tests in Docker

After generating the test file, run it inside the Docker container:

```bash
# Copy test file into container
docker cp test/registered/ascend/basic_function/parameter/test_npu_<name>.py npu-api-adaptation-checker:/sglang/test/registered/ascend/basic_function/parameter/

# Execute inside container (with PYTHONPATH set to use local code)
docker exec npu-api-adaptation-checker bash -c "export PYTHONPATH=/sglang/python:$PYTHONPATH && cd /sglang && python3 -m pytest test/registered/ascend/basic_function/parameter/test_npu_<name>.py -v"
```

If the test fails, analyze the error output and:
- If the parameter is not supported on NPU, mark it as "Planned" or "Special For GPU" in the NPU support features doc
- If the parameter needs NPU-specific handling, add the necessary code in `python/sglang/srt/hardware_backend/npu/utils.py` `set_default_server_args()` or `server_args.py` `_handle_npu_backends()`
- If the test logic is wrong, fix the test file and re-run