FROM vllm/vllm-openai:v0.8.5

WORKDIR /app

COPY inference/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --upgrade-strategy=only-if-needed -r /tmp/requirements.txt

COPY inference /app

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models/cache \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    TOKENIZERS_PARALLELISM=false \
    VLLM_USE_MODELSCOPE=false

CMD ["python", "-c", "print('This image is intended to be started by KubeRay/RayService.')"]
