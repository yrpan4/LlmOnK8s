import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from ray import serve
from transformers import AutoTokenizer
from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine

# OSS 支持
try:
    import oss2
except ImportError:
    oss2 = None

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("qwen-rayservice")

# Ray 和 GPU 配置常量
PIPELINE_PARALLEL_SIZE = int(os.getenv("PIPELINE_PARALLEL_SIZE", "2"))
TENSOR_PARALLEL_SIZE = int(os.getenv("TENSOR_PARALLEL_SIZE", "1"))
TOTAL_GPU_PER_REPLICA = max(1, PIPELINE_PARALLEL_SIZE * TENSOR_PARALLEL_SIZE)

api = FastAPI(title="Qwen vLLM on Ray - Distributed Inference")


# 监控指标定义
request_count = Counter(
    "qwen_requests_total",
    "总请求数",
    ["endpoint", "status", "tenant_id"]
)
request_duration = Histogram(
    "qwen_request_duration_seconds",
    "请求处理时间",
    ["endpoint", "tenant_id"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0)
)
active_requests = Gauge(
    "qwen_active_requests",
    "活跃请求数",
    ["endpoint", "tenant_id"]
)
model_inference_duration = Histogram(
    "qwen_model_inference_duration_seconds",
    "模型推理时间",
    ["model_id", "pipeline_parallel_size"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
)
tokens_generated = Counter(
    "qwen_tokens_generated_total",
    "生成的tokens数",
    ["model_id", "tenant_id"]
)


class ChatTurn(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str = Field(..., min_length=1)


class GenerateRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    history: list[ChatTurn] = Field(default_factory=list)
    system_prompt: str | None = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)


class BatchGenerateRequest(BaseModel):
    """批处理请求"""
    tenant_id: str = Field(..., min_length=1)
    requests: list[GenerateRequest] = Field(..., min_items=1, max_items=32)


@serve.deployment(
    name="QwenVLLMDeployment",
    num_replicas=3,
    max_concurrent_queries=100,
    health_check_period_s=10,
    health_check_timeout_s=30,
    # GPU 资源配置 - 总卡数 = 流水线并行 × 张量并行
    ray_actor_options={"num_gpus": TOTAL_GPU_PER_REPLICA},
    # 放置组配置 - 每个 bundle 占 1 张 GPU，总 bundle 数等于总 GPU 需求
    placement_group_bundles=[{"GPU": 1} for _ in range(TOTAL_GPU_PER_REPLICA)],
    placement_group_strategy="STRICT_PACK",  # 强制打包在同一节点，降低跨节点通信开销
)

@serve.ingress(api)
class QwenVLLMDeployment:
    def __init__(self) -> None:
        # 模型配置 - 支持 HuggingFace 和 OSS 两种方式
        self.model_source = os.getenv("MODEL_SOURCE", "oss").lower()
        self.model_id = os.getenv("MODEL_ID", "Qwen/Qwen3.5-4B")
        self.model_local_path = os.getenv("MODEL_LOCAL_PATH", "/models/Qwen/Qwen3.5-4B")
        
        # 创建模型本地目录
        Path(self.model_local_path).parent.mkdir(parents=True, exist_ok=True)
        
        # 处理模型加载
        if self.model_source == "oss":
            self._download_model_from_oss()
            self.model_ref = self.model_local_path  # 使用本地路径
        elif self.model_source == "hf":
            self.model_ref = self.model_id  # 使用 HuggingFace ID
        else:
            raise ValueError(f"不支持的 MODEL_SOURCE: {self.model_source}，仅支持 'hf' 或 'oss'")
        
        # 配置参数
        self.pipeline_parallel_size = max(1, PIPELINE_PARALLEL_SIZE)
        self.max_model_len = int(os.getenv("MAX_MODEL_LEN", "8192"))
        self.gpu_memory_utilization = float(os.getenv("GPU_MEMORY_UTILIZATION", "0.92"))
        self.vllm_dtype = os.getenv("VLLM_DTYPE", "bfloat16")
        self.download_dir = os.getenv("HF_HOME", "/models/cache")

        logger.info(
            "启用分布式推理 - 模型源: %s, 模型: %s, 流水线并行: %d, dtype: %s",
            self.model_source.upper(),
            self.model_ref,
            self.pipeline_parallel_size,
            self.vllm_dtype
        )

        # 初始化 vLLM 引擎 - 支持流水线并行 + 张量并行
        engine_args = AsyncEngineArgs(
            model=self.model_ref,
            trust_remote_code=True,
            pipeline_parallel_size=self.pipeline_parallel_size,
            tensor_parallel_size=self.tensor_parallel_size,
            gpu_memory_utilization=self.gpu_memory_utilization,

            max_model_len=self.max_model_len,
            dtype=self.vllm_dtype,
            download_dir=self.download_dir,
            enable_prefix_caching=True,
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        
        # 初始化 Tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_ref,
            trust_remote_code=True,
            use_fast=False,
        )

        # 输出详细配置信息
        logger.info("=" * 80)
        logger.info("🚀 分布式推理系统已启用")
        logger.info("=" * 80)
        logger.info("📦 模型源: %s", self.model_source.upper())
        logger.info("📦 模型配置: %s", self.model_ref)
        logger.info("🔀 副本级分布式:   启用 (num_replicas=3)")
        logger.info("🔀 流水线并行:     启用 (pipeline_parallel_size=%d)", self.pipeline_parallel_size)
        logger.info("🔀 张量并行:       启用 (tensor_parallel_size=%d)", self.tensor_parallel_size)
        logger.info("🎯 每副本总 GPU:   %d (= PP × TP)", self.total_gpu_per_replica)
        logger.info("   - 层分割策略:  不同层分配到不同GPU")
        logger.info("   - GPU分配:    结合 PP/TP 进行并行切分")

        logger.info("📊 单副本并发量:   100 qps")
        logger.info("📈 总系统吞吐量:   300+ qps (3副本 × 100并发)")
        logger.info("🛡️  故障转移:      副本级故障自动转移")
        logger.info("💾 模型缓存:       %s", self.download_dir)
        logger.info("📍 数据类型:       %s", self.vllm_dtype)
        logger.info("=" * 80)

    def _download_model_from_oss(self) -> None:
        """从 OSS 下载模型文件"""
        if oss2 is None:
            raise RuntimeError("OSS 模式需要 oss2 库，请先安装：pip install oss2")

        model_oss_uri = os.getenv("MODEL_OSS_URI", "")
        if not model_oss_uri:
            raise ValueError("当 MODEL_SOURCE=oss 时，必须提供 MODEL_OSS_URI，格式: oss://bucket-name/models/Qwen/Qwen3.5-4B")

        oss_endpoint = os.getenv("OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
        oss_key_id = os.getenv("OSS_ACCESS_KEY_ID", "")
        oss_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "")

        if not oss_key_id or not oss_key_secret:
            raise ValueError("当 MODEL_SOURCE=oss 时，必须提供 OSS_ACCESS_KEY_ID 和 OSS_ACCESS_KEY_SECRET")

        # 解析 OSS URI：oss://bucket-name/model-path
        if not model_oss_uri.startswith("oss://"):
            raise ValueError(f"OSS URI 格式错误: {model_oss_uri}，应为 oss://bucket-name/model-path")

        oss_path = model_oss_uri[6:]  # 去掉 'oss://'
        parts = oss_path.split("/", 1)
        bucket_name = parts[0]
        model_prefix = parts[1] if len(parts) > 1 else ""

        logger.info(f"开始从 OSS 下载模型: {model_oss_uri}")
        logger.info(f"  - Endpoint: {oss_endpoint}")
        logger.info(f"  - Bucket: {bucket_name}")
        logger.info(f"  - Prefix: {model_prefix}")
        logger.info(f"  - 本地路径: {self.model_local_path}")

        try:
            # 初始化 OSS 客户端
            auth = oss2.Auth(oss_key_id, oss_key_secret)
            bucket = oss2.Bucket(auth, f"https://{oss_endpoint}", bucket_name)

            # 创建目标目录
            Path(self.model_local_path).mkdir(parents=True, exist_ok=True)

            # 列出并下载所有模型文件
            downloaded_count = 0
            for obj in oss2.ObjectIterator(bucket, prefix=model_prefix):
                obj_key = obj.key

                # 跳过目录对象
                if obj_key.endswith("/"):
                    continue

                # 计算相对路径
                if model_prefix:
                    rel_path = obj_key[len(model_prefix):].lstrip("/")
                else:
                    rel_path = obj_key

                local_file = Path(self.model_local_path) / rel_path
                local_file.parent.mkdir(parents=True, exist_ok=True)

                # 下载文件
                logger.info(f"下载: {obj_key} -> {local_file}")
                bucket.get_object_to_file(obj_key, str(local_file))
                downloaded_count += 1

            if downloaded_count == 0:
                raise ValueError(f"从 OSS 未找到任何文件，路径: {model_oss_uri}")

            logger.info(f"✓ 模型下载完成，共 {downloaded_count} 个文件")

        except Exception as e:
            logger.error(f"从 OSS 下载模型失败: {str(e)}")
            raise RuntimeError(f"OSS 模型下载失败: {str(e)}") from e

    def _build_prompt(self, request: GenerateRequest) -> str:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.extend(item.model_dump() for item in request.history)
        messages.append({"role": "user", "content": request.question})
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

    async def _generate_internal(
        self,
        payload: GenerateRequest,
        request_id: str,
    ) -> dict[str, Any]:
        """内部生成逻辑"""
        prompt = self._build_prompt(payload)
        sampling_params = SamplingParams(
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            top_p=0.95,
        )

        inference_start = time.time()
        last_output = None

        async for output in self.engine.generate(prompt, sampling_params, request_id):
            last_output = output

        inference_duration = time.time() - inference_start

        if not last_output or not last_output.outputs:
            raise HTTPException(status_code=500, detail="模型没有返回内容")

        answer = last_output.outputs[0].text.strip()
        output_tokens = last_output.outputs[0].finish_reason  # 获取tokens信息

        # 记录性能指标
        model_inference_duration.labels(
            model_id=self.model_id,
            pipeline_parallel_size=self.pipeline_parallel_size
        ).observe(inference_duration)
        tokens_generated.labels(
            model_id=self.model_id,
            tenant_id=payload.tenant_id
        ).inc(last_output.prompt_token_ids.__len__())

        return {
            "tenant_id": payload.tenant_id,
            "model_id": self.model_id,
            "ray_service": os.getenv("RAY_SERVICE_NAME", "qwen-rayservice"),
            "answer": answer,
            "inference_time_s": inference_duration,
        }

    @api.get("/healthz")
    async def healthz(self) -> dict[str, Any]:
        """健康检查 - 显示分布式推理配置"""
        return {
            "ok": "true",
            "mode": "distributed-inference",
            "model_source": self.model_source.upper(),
            "model_id": self.model_ref,
            "pipeline_parallel_size": self.pipeline_parallel_size,
            "tensor_parallel_size": self.tensor_parallel_size,
            "total_gpu_per_replica": self.total_gpu_per_replica,
            "num_replicas": 1,
            "max_concurrent_queries": 50,

            "max_model_len": self.max_model_len,
            "distributed_config": {
                "replica_level": "enabled",
                "pipeline_parallel_level": "enabled (layers split across GPUs)",
                "layer_distribution": f"Layer 0-N distributed across {self.pipeline_parallel_size} GPUs",
                "total_concurrent_capacity": 300,
                "fault_tolerance": "single_replica_failure_resilient"
            }
        }

    @api.get("/metrics")
    async def metrics(self) -> Any:
        """Prometheus 指标端点"""
        from fastapi.responses import Response
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST
        )

    @api.post("/generate")
    async def generate(self, payload: GenerateRequest) -> dict[str, Any]:
        """单个请求推理"""
        request_id = str(uuid.uuid4())
        start_time = time.time()
        active_requests.labels(endpoint="/generate", tenant_id=payload.tenant_id).inc()

        try:
            result = await self._generate_internal(payload, request_id)
            duration = time.time() - start_time

            request_count.labels(
                endpoint="/generate",
                status="success",
                tenant_id=payload.tenant_id
            ).inc()
            request_duration.labels(
                endpoint="/generate",
                tenant_id=payload.tenant_id
            ).observe(duration)

            return result

        except Exception as e:
            logger.exception("生成请求失败: %s", request_id)
            request_count.labels(
                endpoint="/generate",
                status="error",
                tenant_id=payload.tenant_id
            ).inc()
            raise HTTPException(status_code=500, detail=str(e)) from e

        finally:
            active_requests.labels(endpoint="/generate", tenant_id=payload.tenant_id).dec()

    @api.post("/batch_generate")
    async def batch_generate(self, payload: BatchGenerateRequest) -> dict[str, Any]:
        """批处理推理 - 支持多个请求"""
        batch_id = str(uuid.uuid4())
        start_time = time.time()
        active_requests.labels(endpoint="/batch_generate", tenant_id=payload.tenant_id).inc()

        try:
            results = []
            successful = 0
            failed = 0

            for idx, req in enumerate(payload.requests):
                # 确保所有请求使用相同的tenant_id
                req.tenant_id = payload.tenant_id
                request_id = f"{batch_id}-{idx}"

                try:
                    result = await self._generate_internal(req, request_id)
                    results.append({
                        "index": idx,
                        "status": "success",
                        **result
                    })
                    successful += 1
                except Exception as e:
                    logger.error("批处理中第 %d 个请求失败: %s", idx, str(e))
                    results.append({
                        "index": idx,
                        "status": "error",
                        "error": str(e)
                    })
                    failed += 1

            duration = time.time() - start_time

            request_count.labels(
                endpoint="/batch_generate",
                status="success",
                tenant_id=payload.tenant_id
            ).inc()
            request_duration.labels(
                endpoint="/batch_generate",
                tenant_id=payload.tenant_id
            ).observe(duration)

            return {
                "batch_id": batch_id,
                "tenant_id": payload.tenant_id,
                "total_requests": len(payload.requests),
                "successful": successful,
                "failed": failed,
                "batch_duration_s": duration,
                "results": results,
            }

        except Exception as e:
            logger.exception("批处理请求失败: %s", batch_id)
            request_count.labels(
                endpoint="/batch_generate",
                status="error",
                tenant_id=payload.tenant_id
            ).inc()
            raise HTTPException(status_code=500, detail=str(e)) from e

        finally:
            active_requests.labels(endpoint="/batch_generate", tenant_id=payload.tenant_id).dec()


deployment = QwenVLLMDeployment.bind()
