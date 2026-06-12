# Docker Verification

Use the following Docker image for verification:
```
swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
```

> **Security note**: This image uses the `main` tag which may change over time. If reproducibility is critical, pin the image to a specific digest using `docker image inspect` to get the SHA256 digest and use `<image>@sha256:<digest>` instead.

## Prerequisites
- Your local SGLang repository (the one you are analyzing) must be mounted into the container. Navigate to the root of your local clone **before** starting the container.
- Ensure the host has sufficient shared memory (e.g., `--shm-size=64g` is used, check with `df -h /dev/shm`).
- Ensure Ascend NPU drivers and firmware are installed on the host at `/usr/local/Ascend/driver` and `/usr/local/Ascend/firmware`.

## Verification Steps

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
   - `--privileged=true`: Full device access for NPU operations (**security risk** — see warning above).
   - `--net=host`: Host network for distributed serving (**security risk** — see warning above).
   - `--entrypoint sleep infinity`: Keeps the container alive in background mode. **Required** — without this, the container may exit immediately.
   - `--device=/dev/davinci0..7`: Mount NPU devices. **Adjust the range** to match your host's actual NPU device count (use `ls /dev/davinci*` to check). Avoid bash brace expansion `{0..15}` — it only works in bash and may mount non-existent devices silently.
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

5. **Test specific new parameters**:
   ```bash
   # Example: test a new parameter on NPU
   python -m sglang.launch_server \
     --model-path <model_path> \
     --device npu \
     --attention-backend ascend \
     --sampling-backend ascend \
     <new_parameter_to_test> <value> \
     --port 8000
   ```

6. **Run existing NPU test suite**:
   ```bash
   # Run NPU interface tests
   cd /sglang && python -m pytest test/registered/ascend/interface/ -v

   # Run NPU parameter tests
   python -m pytest test/registered/ascend/basic_function/parameter/ -v

   # Run NPU backend tests
   python -m pytest test/registered/ascend/basic_function/backends/ -v
   ```

7. **Clean up: remove the container after verification**:
   ```bash
   docker stop npu-api-adaptation-checker && docker rm npu-api-adaptation-checker
   echo "Container npu-api-adaptation-checker removed"
   ```