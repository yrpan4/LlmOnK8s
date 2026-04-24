import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("tenant-gateway")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(os.getenv("STATIC_DIR", BASE_DIR / "static"))
TENANT_CONFIG_FILE = Path(os.getenv("TENANT_CONFIG_FILE", "/app/secrets/tenants.json"))
INFERENCE_TIMEOUT_SECONDS = float(os.getenv("INFERENCE_TIMEOUT_SECONDS", "180"))
DEFAULT_INFERENCE_MODE = os.getenv("DEFAULT_INFERENCE_MODE", "api").strip().lower()


class ChatTurn(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str = Field(..., min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=8000)
    history: list[ChatTurn] = Field(default_factory=list)


class BatchChatRequest(BaseModel):
    """批处理聊天请求"""

    questions: list[ChatRequest] = Field(..., min_items=1, max_items=32)
    history: list[ChatTurn] = Field(default_factory=list)


class TenantStore:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._tenants: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        if not self.config_path.exists():
            raise RuntimeError(f"找不到租户配置文件: {self.config_path}")

        tenant_list = json.loads(self.config_path.read_text(encoding="utf-8"))
        self._tenants = {
            tenant["tenant_id"]: tenant
            for tenant in tenant_list
            if tenant.get("tenant_id")
        }
        logger.info("已加载 %s 个租户配置", len(self._tenants))

    def public_items(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for tenant in self._tenants.values():
            result.append(
                {
                    "tenant_id": tenant["tenant_id"],
                    "display_name": tenant.get("display_name", tenant["tenant_id"]),
                    "description": tenant.get("description", ""),
                    "namespace": tenant.get("namespace", ""),
                    "inference_mode": _resolve_inference_mode(tenant),
                }
            )
        return result

    def authenticate(self, tenant_id: str, tenant_token: str) -> dict[str, Any]:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="租户不存在")
        expected_token = tenant.get("api_token")
        if not expected_token or tenant_token != expected_token:
            raise HTTPException(status_code=401, detail="租户令牌错误")
        return tenant


def _resolve_inference_mode(tenant: dict[str, Any]) -> str:
    mode = str(tenant.get("inference_mode", DEFAULT_INFERENCE_MODE)).strip().lower()
    if mode not in {"api", "rayservice"}:
        logger.warning("tenant=%s inference_mode=%s 非法，已回退为 api", tenant.get("tenant_id"), mode)
        return "api"
    return mode


def _build_messages(system_prompt: str, history: list[dict[str, str]], question: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": question})
    return messages


def _extract_answer_from_openai_payload(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""

    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        # 兼容 content 为多段结构化内容的情况
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()

    return ""


async def _request_openai_compatible(
    tenant: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    api_url = str(tenant.get("api_url", "")).strip()
    if not api_url:
        raise HTTPException(status_code=500, detail="API 模式缺少 api_url 配置")

    api_token = str(tenant.get("llm_api_token", "")).strip() or str(tenant.get("api_token", "")).strip()
    model_name = str(tenant.get("model_name") or os.getenv("MODEL_NAME", "Qwen/Qwen3.5-4B")).strip()
    system_prompt = str(tenant.get("system_prompt", "你是一个简洁、可靠的企业问答助手。"))
    temperature = float(tenant.get("temperature", 0.2))
    max_tokens = int(tenant.get("max_tokens", 512))

    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    payload = {
        "model": model_name,
        "messages": _build_messages(system_prompt, history, question),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    timeout_seconds = float(tenant.get("api_timeout_seconds", INFERENCE_TIMEOUT_SECONDS))
    start = time.time()

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            response = await client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.exception("API 模式调用返回错误")
            raise HTTPException(
                status_code=502,
                detail=f"API 模式推理服务错误: {exc.response.text}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("API 模式调用失败")
            raise HTTPException(status_code=504, detail="API 模式推理服务不可达") from exc

    data = response.json()
    answer = _extract_answer_from_openai_payload(data)
    duration = time.time() - start

    return {
        "tenant_id": tenant["tenant_id"],
        "answer": answer,
        "model_id": model_name,
        "ray_service": "vllm-api",
        "inference_mode": "api",
        "inference_time_s": duration,
        "raw": data,
    }


async def _request_rayservice(
    tenant: dict[str, Any],
    question: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    request_body = {
        "tenant_id": tenant["tenant_id"],
        "question": question,
        "history": history,
        "system_prompt": tenant.get("system_prompt", "你是一个简洁、可靠的企业问答助手。"),
        "temperature": float(tenant.get("temperature", 0.2)),
        "max_tokens": int(tenant.get("max_tokens", 512)),
    }

    inference_url = str(tenant.get("inference_url", "")).strip()
    if not inference_url:
        raise HTTPException(status_code=500, detail="RayService 模式缺少 inference_url 配置")

    logger.info("tenant=%s rayservice request -> %s", tenant["tenant_id"], inference_url)

    async with httpx.AsyncClient(timeout=INFERENCE_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(inference_url, json=request_body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.exception("推理服务返回错误")
            raise HTTPException(
                status_code=502,
                detail=f"推理服务错误: {exc.response.text}",
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("调用推理服务失败")
            raise HTTPException(status_code=504, detail="推理服务不可达") from exc

    data = response.json()
    return {
        "tenant_id": tenant["tenant_id"],
        "answer": data.get("answer", ""),
        "model_id": data.get("model_id", "unknown"),
        "ray_service": data.get("ray_service", "unknown"),
        "inference_mode": "rayservice",
        "inference_time_s": data.get("inference_time_s"),
    }


app = FastAPI(title="Qwen Multi-Tenant Gateway", version="1.1.0")
app.state.tenant_store = TenantStore(TENANT_CONFIG_FILE)


@app.on_event("startup")
async def startup_event() -> None:
    app.state.tenant_store.load()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "tenant_count": len(app.state.tenant_store.public_items()),
        "static_dir": str(STATIC_DIR),
        "default_inference_mode": DEFAULT_INFERENCE_MODE,
    }


@app.get("/api/tenants")
async def list_tenants() -> JSONResponse:
    return JSONResponse(app.state.tenant_store.public_items())


@app.post("/api/chat")
async def chat(
    payload: ChatRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_tenant_token: str = Header(..., alias="X-Tenant-Token"),
) -> JSONResponse:
    tenant = app.state.tenant_store.authenticate(x_tenant_id, x_tenant_token)
    history = [item.model_dump() for item in payload.history]

    mode = _resolve_inference_mode(tenant)
    logger.info("tenant=%s mode=%s", tenant["tenant_id"], mode)

    if mode == "api":
        result = await _request_openai_compatible(tenant, payload.question, history)
    else:
        result = await _request_rayservice(tenant, payload.question, history)

    return JSONResponse(
        {
            "tenant_id": tenant["tenant_id"],
            "display_name": tenant.get("display_name", tenant["tenant_id"]),
            "answer": result.get("answer", ""),
            "model_id": result.get("model_id", "unknown"),
            "ray_service": result.get("ray_service", "unknown"),
            "inference_mode": result.get("inference_mode", mode),
            "inference_time_s": result.get("inference_time_s"),
        }
    )


@app.post("/api/batch_chat")
async def batch_chat(
    payload: BatchChatRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_tenant_token: str = Header(..., alias="X-Tenant-Token"),
) -> JSONResponse:
    """批处理聊天请求 - 用于需要处理多个问题的场景"""
    tenant = app.state.tenant_store.authenticate(x_tenant_id, x_tenant_token)
    mode = _resolve_inference_mode(tenant)

    if mode == "rayservice":
        # 保持原 RayService 批处理兼容能力
        batch_requests = []
        for chat_req in payload.questions:
            request_body = {
                "tenant_id": tenant["tenant_id"],
                "question": chat_req.question,
                "history": [item.model_dump() for item in (payload.history or [])],
                "system_prompt": tenant.get("system_prompt", "你是一个简洁、可靠的企业问答助手。"),
                "temperature": float(tenant.get("temperature", 0.2)),
                "max_tokens": int(tenant.get("max_tokens", 512)),
            }
            batch_requests.append(request_body)

        batch_payload = {
            "tenant_id": tenant["tenant_id"],
            "requests": batch_requests,
        }

        inference_url = tenant.get("inference_batch_url") or str(tenant.get("inference_url", "")).replace(
            "/generate", "/batch_generate"
        )
        logger.info(
            "tenant=%s mode=rayservice batch_request (count=%d) -> %s",
            tenant["tenant_id"],
            len(batch_requests),
            inference_url,
        )

        async with httpx.AsyncClient(timeout=INFERENCE_TIMEOUT_SECONDS * 2) as client:
            try:
                response = await client.post(inference_url, json=batch_payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.exception("推理服务返回错误")
                raise HTTPException(
                    status_code=502,
                    detail=f"推理服务错误: {exc.response.text}",
                ) from exc
            except httpx.HTTPError as exc:
                logger.exception("调用推理服务失败")
                raise HTTPException(status_code=504, detail="推理服务不可达") from exc

        data = response.json()
        return JSONResponse(
            {
                "tenant_id": tenant["tenant_id"],
                "display_name": tenant.get("display_name", tenant["tenant_id"]),
                "inference_mode": "rayservice",
                "batch_id": data.get("batch_id", ""),
                "total_requests": data.get("total_requests", 0),
                "successful": data.get("successful", 0),
                "failed": data.get("failed", 0),
                "batch_duration_s": data.get("batch_duration_s", 0),
                "results": data.get("results", []),
            }
        )

    # API 模式下并发单请求模拟批处理
    semaphore = asyncio.Semaphore(int(tenant.get("api_concurrency", 5)))

    async def _run_one(index: int, question: str) -> dict[str, Any]:
        async with semaphore:
            start = time.time()
            try:
                res = await _request_openai_compatible(
                    tenant=tenant,
                    question=question,
                    history=[item.model_dump() for item in (payload.history or [])],
                )
                return {
                    "index": index,
                    "status": "success",
                    "answer": res.get("answer", ""),
                    "model_id": res.get("model_id", "unknown"),
                    "inference_time_s": res.get("inference_time_s", time.time() - start),
                }
            except Exception as exc:
                logger.exception("API 模式批处理中第 %d 个请求失败", index)
                return {
                    "index": index,
                    "status": "error",
                    "error": str(exc),
                    "inference_time_s": time.time() - start,
                }

    batch_start = time.time()
    tasks = [_run_one(idx, req.question) for idx, req in enumerate(payload.questions)]
    results = await asyncio.gather(*tasks)
    successful = sum(1 for item in results if item["status"] == "success")
    failed = len(results) - successful

    return JSONResponse(
        {
            "tenant_id": tenant["tenant_id"],
            "display_name": tenant.get("display_name", tenant["tenant_id"]),
            "inference_mode": "api",
            "batch_id": f"api-batch-{int(batch_start * 1000)}",
            "total_requests": len(results),
            "successful": successful,
            "failed": failed,
            "batch_duration_s": time.time() - batch_start,
            "results": results,
        }
    )


@app.get("/api/metrics")
async def get_metrics(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_tenant_token: str = Header(..., alias="X-Tenant-Token"),
) -> JSONResponse:
    """获取推理服务指标。RayService 模式默认支持；API 模式需显式配置 metrics_url。"""
    tenant = app.state.tenant_store.authenticate(x_tenant_id, x_tenant_token)
    mode = _resolve_inference_mode(tenant)

    metrics_url = str(tenant.get("metrics_url", "")).strip()
    if not metrics_url and mode == "rayservice":
        metrics_url = str(tenant.get("inference_url", "")).replace("/generate", "/metrics")

    if not metrics_url:
        raise HTTPException(status_code=400, detail="当前租户未配置 metrics_url")

    logger.info("tenant=%s mode=%s metrics -> %s", tenant["tenant_id"], mode, metrics_url)

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(metrics_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("获取指标失败")
            raise HTTPException(
                status_code=504,
                detail="无法获取推理服务指标",
            ) from exc

    from fastapi.responses import Response

    return Response(content=response.text, media_type="text/plain; charset=utf-8")


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
