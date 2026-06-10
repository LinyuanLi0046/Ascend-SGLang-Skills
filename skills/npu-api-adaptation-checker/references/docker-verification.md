# Docker Verification

Use the following Docker image for verification:
```
swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:cann8.5.0-a3-B131
```

## Prerequisites
- Your local SGLang repository (the one you are analyzing) must be mounted into the container. Navigate to the root of your local clone **before** starting the container.
- Ensure the host has sufficient shared memory (e.g., `--shm-size=64g` is used, check with `df -h /dev/shm`).

## Verification Steps

1. **Pull the image**:
   ```bash
   docker pull swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3
   ```

2. **Launch container with NPU devices and mount local source code**:
   ```bash
   # Remove any existing container with the same name first
   docker rm -f npu-api-adaptation-checker 2>/dev/null

   # Replace $(pwd) with absolute path to your local sglang repo if not already inside it
   docker run -itd \
     --shm-size=64g \
     --privileged=true \
     --name npu-api-adaptation-checker \
     --net=host \
     -v $(pwd):/sglang \
     -v /mnt:/mnt \
     -v /home:/home \
     -v /data:/data \
     -v /var/queue_schedule:/var/queue_schedule \
     -v /etc/ascend_install.info:/etc/ascend_install.info \
     -v /usr/local/sbin:/usr/local/sbin \
     -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
     -v /usr/local/Ascend/firmware:/usr/local/Ascend/firmware \
     --device=/dev/davinci{0..15}:/dev/davinci{0..15} \
     --device=/dev/davinci_manager:/dev/davinci_manager \
     --device=/dev/hisi_hdc:/dev/hisi_hdc \
     swr.cn-southwest-2.myhuaweicloud.com/base_image/dockerhub/lmsysorg/sglang:main-cann9.0.0-a3 
   ```

   Key flags explained:
   - `-v $(pwd):/sglang`: **Mounts the current local SGLang repo into the container**, so all code changes are immediately available.
   - `--shm-size=64g`: Shared memory for inter-process communication (verify host availability).
   - `--privileged=true`: Full device access for NPU operations.
   - `--net=host`: Host network for distributed serving.
   - `--device=/dev/davinci{0..15}`: Mount all 16 NPU devices.
   - `-v /usr/local/Ascend/driver` and `-v /usr/local/Ascend/firmware`: Ascend driver and firmware.
   - `-v /etc/ascend_install.info`: Ascend installation info.
   - `-v /mnt:/mnt`, `-v /home:/home`, `-v /data:/data`: Model weights and data access.
   - `--entrypoint sleep infinity`: Keeps the container alive in background mode.

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
