# Parameter Testing on NPU (via Generated Test File)

This document defines the procedure for verifying new server parameters on Ascend NPU hardware using a **single generated Python test file** instead of ad-hoc shell commands.

## Critical Principle: Use pytest, Not Shell

Instead of manually launching servers and curling endpoints, the agent generates a proper pytest file that:
- Uses `popen_launch_server` (which handles `ready to roll!` detection automatically)
- Tests each new parameter in a separate test method
- Follows existing NPU test conventions
- Can be run with a single `pytest` command

## Test File Template

Generate the file at `test/registered/ascend/basic_function/parameter/test_npu_new_params_<date>.py` (e.g., `test_npu_new_params_20260601.py`).

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
    est_time=600,
    suite="nightly-1-npu-a3",
    nightly=True,
)


class TestNpuNewParams(CustomTestCase):
    """Testcase: Verify new Community API parameters work correctly on NPU.

    This test covers all new parameters added since the last NPU doc update
    that are classified as Category 1 (Auto-Adaptable) or Category 2 (Conditional).

    [Test Category] Parameter
    [Test Target] --trace-modules, --optimistic-prefill-retries,
                  --encoder-bootstrap-port, --encoder-register-urls
    """

    model = LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH
    base_url = DEFAULT_URL_FOR_TEST

    # ---- Helper ----

    @classmethod
    def _launch_with_extra_args(cls, extra_args):
        """Launch server with standard NPU args plus the given extra args."""
        other_args = [
            "--attention-backend", "ascend",
            "--disable-cuda-graph",
            "--mem-fraction-static", "0.8",
        ] + extra_args
        return popen_launch_server(
            cls.model,
            cls.base_url,
            timeout=DEFAULT_TIMEOUT_FOR_SERVER_LAUNCH,
            other_args=other_args,
        )

    @classmethod
    def _do_inference(cls):
        """Send a basic inference request and verify it works."""
        response = requests.post(
            f"{cls.base_url}/generate",
            json={
                "text": "The capital of France is",
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": 32,
                },
            },
        )
        assert response.status_code == 200, f"Inference failed: {response.text}"
        assert "Paris" in response.text, (
            f"Inference did not contain 'Paris': {response.text}"
        )

    # ---- Test methods (one per new parameter) ----

    def test_trace_modules(self):
        """Verify --trace-modules works on NPU."""
        self.process = self._launch_with_extra_args(["--trace-modules", "request"])
        try:
            self._do_inference()
        finally:
            kill_process_tree(self.process.pid)

    def test_optimistic_prefill_retries(self):
        """Verify --optimistic-prefill-retries works on NPU."""
        self.process = self._launch_with_extra_args(
            ["--optimistic-prefill-retries", "0"]
        )
        try:
            self._do_inference()
        finally:
            kill_process_tree(self.process.pid)

    def test_encoder_bootstrap_port(self):
        """Verify --encoder-bootstrap-port works on NPU."""
        self.process = self._launch_with_extra_args(
            ["--encoder-bootstrap-port", "8997"]
        )
        try:
            self._do_inference()
        finally:
            kill_process_tree(self.process.pid)

    def test_encoder_register_urls(self):
        """Verify --encoder-register-urls works on NPU with empty list."""
        self.process = self._launch_with_extra_args(
            ["--encoder-register-urls", "[]"]
        )
        try:
            self._do_inference()
        finally:
            kill_process_tree(self.process.pid)


if __name__ == "__main__":
    unittest.main()
```

## How to Use the Template

When generating the test file:

1. **Identify which parameters to test** — only Category 1 and Category 2 parameters (platform-agnostic or NPU-applicable). Skip Category 3 (Special For GPU) parameters.

2. **Add one `test_<param_name>` method per parameter** — follow the pattern above. Each method:
   - Calls `_launch_with_extra_args(["--param-name", "default_value"])`
   - Calls `_do_inference()` to verify the server works
   - Uses try/finally to always clean up the process

3. **Use the correct default value** — pass the parameter with its default value (not a random test value). The goal is to verify the parameter is recognized and doesn't break anything.

4. **For Category 1 params that need no action** — still include a test. The test proves the parameter works with its default value on NPU.

5. **For Category 2 params with conditions** — include the test with the default (safe) value. Add a TODO comment about what additional values need verification.

## Key Design Decisions

- **One file, many tests**: Rather than one file per parameter, use a single file with one test method per parameter. This is faster (one server per test, no need to restart for every parameter) and easier to maintain.
- **`popen_launch_server`** handles the `ready to roll!` wait automatically. Do NOT implement manual polling — the framework already does this.
- **`LLAMA_3_2_1B_INSTRUCT_WEIGHTS_PATH`** is the default model. It's the smallest and fastest. Only use larger models if a parameter requires a specific model architecture.
- **Standard NPU args** (`--attention-backend ascend`, `--disable-cuda-graph`, `--mem-fraction-static 0.8`) are included in every launch.

## Running the Test

Inside the Docker container:
```bash
docker exec npu-api-adaptation-checker bash -c "
export PYTHONPATH=/sglang/python:\$PYTHONPATH
cd /sglang
python -m pytest test/registered/ascend/basic_function/parameter/test_npu_new_params_<date>.py -v
"
```

If a test fails:
- Check if the parameter requires special NPU-handling code -> add to suggestions
- Check if the model is compatible with the parameter -> try a different model constant
- Check if the default value needs NPU override -> add to Category 1 suggestions
