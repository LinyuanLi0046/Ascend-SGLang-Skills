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

### Template A: Boolean Flag Parameter (e.g., `--enable-xxx`)

```python
import unittest

import requests

from sglang.srt.utils import kill_process_tree
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

    model = "/home/weights/LLM-Research/Llama-3.2-1B-Instruct"
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

    model = "/home/weights/LLM-Research/Llama-3.2-1B-Instruct"
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

    model = "/home/weights/LLM-Research/Llama-3.2-1B-Instruct"
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

## Step 4: Model Path Constants and Local Availability

### Local Model Paths

Local model weights are stored at `/home/weights/` on the host, and are accessible inside the Docker container at the same path `/home/weights/` (via the `-v /home:/home` mount).

**Important**: Model availability verification is the **user's responsibility** before running tests. Use the mapping table below to select a model that is known to be locally available. Always prefer models with confirmed local paths.

### Local Model Availability Mapping

| Test Constant | CI Path (under `/root/.cache/modelscope/hub/models/`) | Local Path (`/home/weights/`) | Available Locally? | Replacement If Missing |
|---|---|---|---|---|
| `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` | `LLM-Research/Llama-3.2-1B-Instruct` | `/home/weights/LLM-Research/Llama-3.2-1B-Instruct` | ✅ Available | — |
| `LLAMA_3_1_8B_INSTRUCT_WEIGHTS_PATH` | `AI-ModelScope/Llama-3.1-8B-Instruct` | `/home/weights/LLM-Research/Meta-Llama-3.1-8B-Instruct` | ⚠️ Different path | Use `/home/weights/LLM-Research/Meta-Llama-3.1-8B-Instruct` |
| `QWEN2_5_7B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen2.5-7B-Instruct` | `/home/weights/Qwen/Qwen2.5-7B-Instruct` | ✅ Available | — |
| `QWEN3_8B_WEIGHTS_PATH` | `Qwen/Qwen3-8B` | `/home/weights/Qwen/Qwen3-8B` | ✅ Available | — |
| `QWEN3_0_6B_WEIGHTS_PATH` | `Qwen/Qwen3-0.6B` | `/home/weights/Qwen3-0.6B` | ✅ Available | — |
| `QWEN3_VL_8B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen3-VL-8B-Instruct` | `/home/weights/Qwen/Qwen3-VL-8B-Instruct` | ✅ Available | — |
| `QWEN2_5_VL_7B_INSTRUCT_WEIGHTS_PATH` | `Qwen/Qwen2.5-VL-7B-Instruct` | `/home/weights/Qwen2.5-VL-7B-Instruct` | ✅ Available | — |
| `GLM_4_5V_WEIGHTS_PATH` | `ZhipuAI/GLM-4.5V` | `/home/weights/GLM-4.5V` | ✅ Available | — |
| `BAICHUAN2_13B_CHAT_WEIGHTS_PATH` | `baichuan-inc/Baichuan2-13B-Chat` | `/home/weights/Baichuan2-13B` | ✅ Available | — |
| `LLAMA_2_7B_WEIGHTS_PATH` | `LLM-Research/Llama-2-7B` | `/home/weights/Llama-2-7b-chat-hf` | ⚠️ Different model | Use `/home/weights/Llama-2-7b-chat-hf` (chat version) |
| `DBRX_INSTRUCT_WEIGHTS_PATH` | `AI-ModelScope/dbrx-instruct` | `/home/weights/AI-ModelScope/dbrx-instruct` | ✅ Available | — |
| `QWEN3_32B_WEIGHTS_PATH` | `Qwen/Qwen3-32B` | `/home/weights/Qwen/Qwen3-32B` | ✅ Available | — |
| `MINICPM_O_2_6_WEIGHTS_PATH` | `openbmb/MiniCPM-o-2_6` | — | ❌ Not available | Use `QWEN3_VL_8B_INSTRUCT_WEIGHTS_PATH` for multimodal tests, or `QWEN2_5_7B_INSTRUCT_WEIGHTS_PATH` for warmup tests |
| `DEEPSEEK_CODER_1_3_B_BASE_PATH` | `deepseek-ai/deepseek-coder-1.3b-base` | — | ❌ Not available | Use `QWEN3_0_6B_WEIGHTS_PATH` (`/home/weights/Qwen3-0.6B`) for small model FIM tests, or `LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH` for basic parameter tests |

### How to Use Local Model Paths in Generated Tests

Generated test files use **direct local paths** for model weights. The Docker container mounts `/home:/home`, so `/home/weights/` on the host is accessible at `/home/weights/` inside the container.

Example:
```python
model = "/home/weights/LLM-Research/Llama-3.2-1B-Instruct"
```

**Pre-generation model selection**: Before writing any test file, choose a model from the "Available Locally" column in the mapping table above. **Do not rely on executing `ls` from the generation context**; instead, instruct the user to verify availability before running tests, using a command like:
```bash
docker exec npu-api-adaptation-checker ls /home/weights/LLM-Research/Llama-3.2-1B-Instruct/config.json
```

Model selection guidelines:
- For 1-NPU basic tests: `/home/weights/LLM-Research/Llama-3.2-1B-Instruct` (smallest, fastest)
- For 1-NPU accuracy tests: `/home/weights/LLM-Research/Meta-Llama-3.1-8B-Instruct`
- For multimodal tests: `/home/weights/Qwen/Qwen3-VL-8B-Instruct`
- For FIM/completion tests: `/home/weights/Qwen3-0.6B`

## Step 5: CI Registration Convention

```python
register_npu_ci(
    est_time=200,            # Estimated test time in seconds; 200s for small models, 400s for 7-8B
    suite="nightly-1-npu-a3",  # Suite name: nightly-<num_npu>-npu-<hardware>
    nightly=True,           # Whether to run in nightly CI
    disabled="run failed",  # Optional: disable if known to fail
)
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

1. **Server starts successfully**: The server with the new parameter should start without errors
2. **Parameter takes effect**: Check `/server_info` endpoint to confirm the parameter value is applied
3. **Inference works**: A basic generate request should return a valid response
4. **No NPU-specific errors**: Check server logs for any NPU-related warnings or errors
5. **Test passes**: Run `python3 -m pytest <test_file> -v` inside the Docker container

## Step 8: Running Generated Tests in Docker

After generating the test file, run it inside the Docker container:

```bash
# Copy test file into container
docker cp test/registered/ascend/basic_function/parameter/test_npu_<name>.py npu-api-adaptation-checker:/sglang/test/registered/ascend/basic_function/parameter/

# Execute inside container (with PYTHONPATH set to use local code)
docker exec npu-api-adaptation-checker bash -c "export PYTHONPATH=/sglang/python:\$PYTHONPATH && cd /sglang && python3 -m pytest test/registered/ascend/basic_function/parameter/test_npu_<name>.py -v"
```

If the test fails, analyze the error output and:
- If the parameter is not supported on NPU, mark it as "Planned" or "Special For GPU" in the NPU support features doc
- If the parameter needs NPU-specific handling, add the necessary code in `python/sglang/srt/hardware_backend/npu/utils.py` `set_default_server_args()` or `server_args.py` `_handle_npu_backends()`
- If the test logic is wrong, fix the test file and re-run
