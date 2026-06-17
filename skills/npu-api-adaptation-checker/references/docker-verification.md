# Docker Verification

## Step 0: Hardware Availability Check

**Before attempting any Docker verification, check whether the host has Ascend NPU hardware.** This check determines which verification path to take:

```bash
# Check for NPU devices
ls /dev/davinci* 2>/dev/null && echo "NPU devices found" || echo "No NPU devices"

# Check for Ascend driver
ls /usr/local/Ascend/driver 2>/dev/null && echo "Ascend driver found" || echo "No Ascend driver"

# Check for Ascend firmware
ls /usr/local/Ascend/firmware 2>/dev/null && echo "Ascend firmware found" || echo "No Ascend firmware"
```

### Decision Matrix

| NPU Devices | Ascend Driver/Firmware | Verification Path |
|-------------|----------------------|-------------------|
| Found | Found | Full Docker verification (Steps 1-7 below) |
| Not found | Found | Light verification only (see Light Verification section below) |
| Not found | Not found | Skip Docker verification -- report "Skipped: no NPU hardware available" |

### After the Check: Immediate Next Action (CRITICAL)

**The hardware check is a GATE, not a GOAL. Do NOT stop after running `ls /dev/davinci*`. Based on the result:**

#### GREEN path -- NPU devices AND driver/firmware BOTH found

You MUST take these actions NOW (same tool call batch as the check):

1. Pull the Docker image (use timeout_ms=600000):
   ```
   docker pull swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```
2. Launch container in background:
   ```
   docker rm -f npu-api-adaptation-checker 2>/dev/null
   docker run -itd --shm-size=64g --privileged=true --name npu-api-adaptation-checker --net=host --entrypoint sleep infinity -v $(pwd):/sglang -v /mnt:/mnt -v /home:/home -v /data:/data -v /var/queue_schedule:/var/queue_schedule -v /etc/ascend_install.info:/etc/ascend_install.info -v /usr/local/sbin:/usr/local/sbin -v /usr/local/Ascend/driver:/usr/local/Ascend/driver -v /usr/local/Ascend/firmware:/usr/local/Ascend/firmware --device=/dev/davinci0 --device=/dev/davinci1 --device=/dev/davinci2 --device=/dev/davinci3 --device=/dev/davinci4 --device=/dev/davinci5 --device=/dev/davinci6 --device=/dev/davinci7 --device=/dev/davinci_manager --device=/dev/hisi_hdc swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```
   Adjust --device count to match actual NPU count from `ls /dev/davinci*`. Default: 8 devices (0-7) plus manager and hisi_hdc.
3. Then continue with Phase B code analysis. After analysis, come back for Phase C: generate the test file, copy it into the container, and run it with pytest.

**This is NOT optional. Do NOT defer to "next steps" or "action items". Do NOT ask the user. Execute NOW.**

#### YELLOW path -- driver/firmware found but NO NPU devices

Proceed to Light Verification section below. State "Light verification (no NPU devices)" in the report.

#### RED path -- neither NPU devices NOR driver/firmware found

Skip Docker verification entirely. Report "Skipped: no NPU hardware available" and fall back to static code analysis.

> **Important**: Do NOT silently skip Docker verification. Always explicitly report the skip reason in the output. When skipping, fall back to static code analysis (reading source files) for the NPU code path verification in Step 7 of `analysis-steps.md`.

---

## Full Docker Verification (NPU Hardware Available)

Use the following Docker image for verification:
```
swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
```

> **Security note**: This image uses the `main` tag which may change over time. If reproducibility is critical, pin the image to a specific digest using `docker image inspect` to get the SHA256 digest and use `<image>@sha256:<digest>` instead.

### Prerequisites
- Your local SGLang repository (the one you are analyzing) must be mounted into the container. Navigate to the root of your local clone **before** starting the container.
- Ensure the host has sufficient shared memory (e.g., `--shm-size=64g` is used, check with `df -h /dev/shm`).
- Ensure Ascend NPU drivers and firmware are installed on the host at `/usr/local/Ascend/driver` and `/usr/local/Ascend/firmware`.

### Verification Steps

1. **Pull the image**:
   ```bash
   docker pull swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```

2. **Launch container with NPU devices and mount local source code**:

   > **Caution**: `docker rm -f` below will forcibly remove any existing container named `npu-api-adaptation-checker`. If you have an important container with that name, rename it first or change the container name below.

   ```bash
   # Remove any existing container with the same name first
   docker rm -f npu-api-adaptation-checker 2>/dev/null

   # Adjust --device=/dev/davinci{0..N} to match the number of NPU devices on your host.
   # Use `ls /dev/davinci*` on the host to check available devices.
   # The example below mounts devices 0-7 (8 NPUs). Adjust as needed.
   docker run -itd \
     --shm-size=64g \
     --privileged=true \
     --name npu-api-adaptation-checker \
     --net=host \
     --entrypoint sleep infinity \
     -v $(pwd):/sglang \
     -v /mnt:/mnt \
     -v /home:/home \
     -v /data:/data \
     -v /var/queue_schedule:/var/queue_schedule \
     -v /etc/ascend_install.info:/etc/ascend_install.info \
     -v /usr/local/sbin:/usr/local/sbin \
     -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
     -v /usr/local/Ascend/firmware:/usr/local/Ascend/firmware \
     --device=/dev/davinci0:/dev/davinci0 \
     --device=/dev/davinci1:/dev/davinci1 \
     --device=/dev/davinci2:/dev/davinci2 \
     --device=/dev/davinci3:/dev/davinci3 \
     --device=/dev/davinci4:/dev/davinci4 \
     --device=/dev/davinci5:/dev/davinci5 \
     --device=/dev/davinci6:/dev/davinci6 \
     --device=/dev/davinci7:/dev/davinci7 \
     --device=/dev/davinci_manager:/dev/davinci_manager \
     --device=/dev/hisi_hdc:/dev/hisi_hdc \
     swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```

   > **Security warning**: `--privileged=true` grants the container full access to all host devices and kernel capabilities. `--net=host` removes network isolation between the container and host. These flags are required for NPU device access and distributed serving, but should only be used in trusted environments.

   Key flags explained:
   - `-v $(pwd):/sglang`: **Mounts the current local SGLang repo into the container**, so all code changes are immediately available.
   - `--shm-size=64g`: Shared memory for inter-process communication (verify host availability).
   - `--privileged=true`: Full device access for NPU operations (**security risk** -- see warning above).
   - `--net=host`: Host network for distributed serving (**security risk** -- see warning above).
   - `--entrypoint sleep infinity`: Keeps the container alive in background mode. **Required** -- without this, the container may exit immediately.
   - `--device=/dev/davinci0..7`: Mount NPU devices. **Adjust the range** to match your host's actual NPU device count (use `ls /dev/davinci*` to check). Avoid bash brace expansion `{0..15}` -- it only works in bash and may mount non-existent devices silently.
   - `-v /usr/local/Ascend/driver` and `-v /usr/local/Ascend/firmware`: Ascend driver and firmware.
   - `-v /etc/ascend_install.info`: Ascend installation info.
   - `-v /mnt:/mnt`, `-v /home:/home`, `-v /data:/data`: Model weights and data access.

3. **Set up environment inside the container**:
   ```bash
   # Enter the container
   docker exec -it npu-api-adaptation-checker bash

   # Use the mounted local code by adding its python/ to PYTHONPATH
   export PYTHONPATH=/sglang/python:$PYTHONPATH

   # Verify: should point to the mounted local code
   python -c "import sglang; print(sglang.__file__)"
   # Expected output contains .../sglang/python/sglang/...
   ```

4. **Verify server arguments**:
   ```bash
   # Inside the container
   python -m sglang.launch_server --help
   ```
   This will list all available server arguments. Compare against the NPU support features doc.

5. **Test new parameters via generated pytest file** -- Generate a single Python test file that covers all new NPU-applicable parameters, following the template in [references/parameter-testing.md](references/parameter-testing.md). Then copy it into the container and run:
   ```bash
   docker cp test/registered/ascend/basic_function/parameter/test_npu_new_params_<date>.py npu-api-adaptation-checker:/sglang/test/registered/ascend/basic_function/parameter/
   docker exec npu-api-adaptation-checker bash -c "export PYTHONPATH=/sglang/python:$PYTHONPATH && cd /sglang && python -m pytest test/registered/ascend/basic_function/parameter/test_npu_new_params_<date>.py -v"
   ```
   This approach is preferred over ad-hoc shell commands because:
   - `popen_launch_server` handles `ready to roll!` detection automatically
   - Each parameter gets a proper test method with cleanup
   - Results are reported in standard pytest format
   - The test file serves as permanent regression coverage

6. **Run existing NPU test suite** (optional, only if time permits):
   ```bash
   # Run NPU interface tests
   cd /sglang && python -m pytest test/registered/ascend/interface/ -v
   ```

7. **Clean up: remove the container after verification**:
   ```bash
   docker stop npu-api-adaptation-checker && docker rm npu-api-adaptation-checker
   echo "Container npu-api-adaptation-checker removed"
   ```

---

## Light Verification (No NPU Devices Available)

When the host has Ascend driver/firmware installed but no `/dev/davinci*` devices (e.g., driver is present but hardware is offline), or when only Docker is available without NPU device passthrough, you can perform **light verification** -- checks that do not require actual NPU hardware:

1. **Pull the image** (same image, but no device passthrough):
   ```bash
   docker pull swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```

2. **Launch container without NPU devices**:
   ```bash
   docker rm -f npu-api-adaptation-checker 2>/dev/null

   docker run -itd \
     --shm-size=64g \
     --name npu-api-adaptation-checker \
     --entrypoint sleep infinity \
     -v $(pwd):/sglang \
     -v /home:/home \
     -v /data:/data \
     swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```

   > Note: `--privileged`, `--net=host`, and `--device` flags are omitted since NPU devices are not available.

3. **Set up environment and verify CLI help**:
   ```bash
   docker exec -it npu-api-adaptation-checker bash
   export PYTHONPATH=/sglang/python:$PYTHONPATH

   # Verify the module is importable (will NOT start a server -- no NPU)
   python -c "import sglang; print(sglang.__file__)"

   # Check that new parameters appear in --help output
   python -m sglang.launch_server --help 2>&1 | grep -E '<new_parameter_name>'
   ```

4. **Verify parameter definitions via Python introspection**:
   ```bash
   # Inside the container
   python -c "
   from sglang.srt.server_args import ServerArgs
   args = ServerArgs()
   print('trace_modules:', args.trace_modules)
   "
   ```

5. **Verify NPU adaptation code paths exist**:
   ```bash
   # Check that set_default_server_args() and _handle_npu_backends() exist
   python -c "
   from sglang.srt.hardware_backend.npu.utils import set_default_server_args
   print('set_default_server_args found:', set_default_server_args.__name__)
   "
   ```

6. **Clean up**:
   ```bash
   docker stop npu-api-adaptation-checker && docker rm npu-api-adaptation-checker
   ```

### Limitations of Light Verification

Light verification can confirm:
- New parameters are defined in `ServerArgs` with correct defaults
- New parameters appear in `--help` output
- NPU adaptation functions exist and are importable
- CANNOT confirm parameters actually work on NPU hardware
- CANNOT confirm server starts with `--device npu --attention-backend ascend`
- CANNOT run NPU test suite

**When reporting light verification results**, always prefix with "Light verification (no NPU hardware)" and clearly state which checks were performed and which could not be performed.

---

## No Docker Available (Pure Static Analysis)

When neither Docker nor NPU hardware is available, skip Docker verification entirely. Perform Step 7 (NPU code path verification) from `analysis-steps.md` using pure static code analysis -- reading source files and searching for NPU-specific handling patterns.

**You MUST explicitly report this in the output**: "Docker verification: Skipped -- no NPU hardware available on host. NPU code paths verified via static analysis only."
