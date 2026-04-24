#!/usr/bin/env bash
set -euo pipefail

# 阿里云 ACK 部署脚本
# 目标：部署前端网关 + 多租户 RayService，并检查外网可访问性

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_DIR="${ROOT_DIR}/rendered"
K8S_DIR="${ROOT_DIR}/k8s"
CONFIG_DIR="${ROOT_DIR}/config"
PYTHON_BIN=""

mkdir -p "${RENDER_DIR}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "缺少命令: $cmd" >&2
    exit 1
  fi
}

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "缺少环境变量: ${name}" >&2
    exit 1
  fi
}

detect_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
    return
  fi

  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
    return
  fi

  echo "缺少命令: python3 或 python" >&2
  exit 1
}

normalize_registry_host() {
  local registry="$1"
  # 兼容错误写法：crpi-xxx.cn-hangzhou.aliyuncs.com -> crpi-xxx.cn-hangzhou.personal.cr.aliyuncs.com
  if [[ "$registry" =~ ^crpi-[^.]+\.cn-[^.]+\.aliyuncs\.com$ ]]; then
    echo "${registry/.aliyuncs.com/.personal.cr.aliyuncs.com}"
    return
  fi
  echo "$registry"
}

normalize_repo() {
  local repo="$1"
  repo="${repo#/}"
  if [[ -n "${ALIYUN_NAMESPACE:-}" ]]; then
    local ns="${ALIYUN_NAMESPACE#/}"
    ns="${ns%/}"
    if [[ "$repo" == "${ns}" || "$repo" == "${ns}/"* ]]; then
      echo "$repo"
      return
    fi
    if [[ "$repo" == */* ]]; then
      echo "$repo"
      return
    fi
    echo "${ns}/${repo}"
    return
  fi
  echo "$repo"
}

render_env_file() {
  local input_file="$1"
  local output_file="$2"

  if command -v envsubst >/dev/null 2>&1; then
    envsubst < "$input_file" > "$output_file"
    return
  fi

  "${PYTHON_BIN}" - "$input_file" "$output_file" <<'PY'
import os
import re
import sys

inp, outp = sys.argv[1], sys.argv[2]
text = open(inp, "r", encoding="utf-8").read()
pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

def repl(match):
    return os.environ.get(match.group(1), "")

rendered = pattern.sub(repl, text)
open(outp, "w", encoding="utf-8", newline="\n").write(rendered)
PY
}

wait_webapp_ready() {
  local timeout_sec="${1:-300}"
  local interval=5
  local elapsed=0

  while (( elapsed < timeout_sec )); do
    local available
    available="$(kubectl get deploy qwen-webapp -n platform -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)"
    if [[ "$available" =~ ^[1-9][0-9]*$ ]]; then
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  return 1
}

wait_webapp_external_ip() {
  local timeout_sec="${1:-600}"
  local interval=10
  local elapsed=0

  while (( elapsed < timeout_sec )); do
    local lb_ip
    lb_ip="$(kubectl get svc qwen-webapp -n platform -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
    if [[ -n "$lb_ip" ]]; then
      echo "$lb_ip"
      return 0
    fi

    local lb_host
    lb_host="$(kubectl get svc qwen-webapp -n platform -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
    if [[ -n "$lb_host" ]]; then
      echo "$lb_host"
      return 0
    fi

    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  return 1
}

ensure_cluster_dns_ready() {
  local timeout_sec="${1:-300}"

  if [[ ! -f "${K8S_DIR}/platform/coredns-patch.json" ]]; then
    log "未找到 CoreDNS 补丁文件，直接检查 CoreDNS 就绪状态"
  else
    log "尝试应用 CoreDNS 调度补丁..."
    kubectl -n kube-system patch deployment coredns \
      --type merge \
      --patch-file "${K8S_DIR}/platform/coredns-patch.json" >/dev/null 2>&1 || \
      log "警告：CoreDNS 补丁应用失败，将继续等待 CoreDNS 就绪"
  fi

  log "等待 CoreDNS Deployment 就绪..."
  if ! kubectl -n kube-system rollout status deployment/coredns --timeout="${timeout_sec}s"; then
    echo "CoreDNS 未在预期时间内就绪" >&2
    kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide || true
    kubectl -n kube-system describe deployment coredns || true
    exit 1
  fi
}

wait_inference_service_endpoint() {
  local tenant_ns="$1"
  local timeout_sec="${2:-1800}"
  local interval=10
  local elapsed=0

  while (( elapsed < timeout_sec )); do
    local endpoint_ip
    endpoint_ip="$(kubectl get endpoints qwen-rayservice-serve-svc -n "${tenant_ns}" -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null || true)"

    if [[ -n "${endpoint_ip}" ]]; then
      log "租户 ${tenant_ns} 推理 endpoint 已就绪: ${endpoint_ip}:8000"
      return 0
    fi

    sleep "${interval}"
    elapsed=$((elapsed + interval))
  done

  echo "租户 ${tenant_ns} 推理 endpoint 未在预期时间内就绪" >&2
  kubectl get rayservice -n "${tenant_ns}" -o wide || true
  kubectl get pods -n "${tenant_ns}" -o wide || true
  kubectl describe svc qwen-rayservice-serve-svc -n "${tenant_ns}" || true
  return 1
}

main() {
  require_cmd kubectl
  detect_python

  require_var ALIYUN_REGISTRY
  require_var ACR_WEBAPP_REPO
  require_var ACR_INFERENCE_REPO
  require_var IMAGE_TAG
  require_var TENANT_A_TOKEN
  require_var TENANT_B_TOKEN

  ALIYUN_REGISTRY="$(normalize_registry_host "${ALIYUN_REGISTRY}")"
  export ALIYUN_REGISTRY
  export TENANT_A_TOKEN TENANT_B_TOKEN

  local normalized_web_repo normalized_infer_repo
  normalized_web_repo="$(normalize_repo "${ACR_WEBAPP_REPO}")"
  normalized_infer_repo="$(normalize_repo "${ACR_INFERENCE_REPO}")"

  export WEBAPP_IMAGE="${ALIYUN_REGISTRY}/${normalized_web_repo}:${IMAGE_TAG}"
  export INFERENCE_IMAGE="${ALIYUN_REGISTRY}/${normalized_infer_repo}:${IMAGE_TAG}"

  export MODEL_SOURCE="${MODEL_SOURCE:-hf}"
  export MODEL_OSS_URI="${MODEL_OSS_URI:-}"
  export VLLM_MODEL_REF="${VLLM_MODEL_REF:-Qwen/Qwen3.5-4B}"
  export MODEL_DISPLAY_NAME="${MODEL_DISPLAY_NAME:-Qwen3.5-4B}"
  export MODEL_LOCAL_PATH="${MODEL_LOCAL_PATH:-/models/Qwen3.5-4B}"
  export OSS_ENDPOINT="${OSS_ENDPOINT:-oss-${ALIYUN_REGION:-cn-hangzhou}.aliyuncs.com}"
  export MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
  export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.9}"
  export VLLM_DTYPE="${VLLM_DTYPE:-float16}"
  export PIPELINE_PARALLEL_SIZE="${PIPELINE_PARALLEL_SIZE:-1}"
  export TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
  export SERVE_ACTOR_NUM_GPUS="${SERVE_ACTOR_NUM_GPUS:-$((PIPELINE_PARALLEL_SIZE * TENSOR_PARALLEL_SIZE))}"
  export SERVE_REPLICAS="${SERVE_REPLICAS:-1}"
  export WORKER_REPLICAS="${WORKER_REPLICAS:-1}"

  log "WebApp 镜像: ${WEBAPP_IMAGE}"
  log "Inference 镜像: ${INFERENCE_IMAGE}"

  log "应用命名空间..."
  kubectl apply -f "${K8S_DIR}/namespaces.yaml"

  ensure_cluster_dns_ready

  log "应用稳定推理服务名..."
  kubectl apply -f "${K8S_DIR}/platform/inference-services.yaml"

  log "渲染租户配置..."
  render_env_file "${CONFIG_DIR}/tenants.template.json" "${RENDER_DIR}/tenants.json"

  log "创建/更新 tenant-routing-config Secret..."
  kubectl -n platform create secret generic tenant-routing-config \
    --from-file=tenants.json="${RENDER_DIR}/tenants.json" \
    --dry-run=client -o yaml | kubectl apply -f -

  if [[ -n "${ALIYUN_REGISTRY_USERNAME:-}" && -n "${ALIYUN_REGISTRY_PASSWORD:-}" ]]; then
    log "创建/更新镜像拉取 Secret..."
    for ns in platform tenant-a tenant-b; do
      kubectl -n "$ns" create secret docker-registry aliyun-registry-secret \
        --docker-server="${ALIYUN_REGISTRY}" \
        --docker-username="${ALIYUN_REGISTRY_USERNAME}" \
        --docker-password="${ALIYUN_REGISTRY_PASSWORD}" \
        --dry-run=client -o yaml | kubectl apply -f -
    done
  else
    log "未提供 ACR 用户名密码，跳过 imagePullSecret 创建"
  fi

  if [[ -n "${OSS_ACCESS_KEY_ID:-}" && -n "${OSS_ACCESS_KEY_SECRET:-}" ]]; then
    log "创建/更新 OSS 凭证 Secret..."
    for ns in tenant-a tenant-b; do
      kubectl -n "$ns" create secret generic oss-credentials \
        --from-literal=ACCESS_KEY_ID="${OSS_ACCESS_KEY_ID}" \
        --from-literal=ACCESS_KEY_SECRET="${OSS_ACCESS_KEY_SECRET}" \
        --dry-run=client -o yaml | kubectl apply -f -
    done
  fi

  log "渲染并部署前端清单..."
  render_env_file "${K8S_DIR}/platform/webapp.yaml" "${RENDER_DIR}/webapp.yaml"
  kubectl apply -f "${RENDER_DIR}/webapp.yaml"

  log "渲染并部署租户 A RayService..."
  export TENANT_NAMESPACE="tenant-a"
  render_env_file "${K8S_DIR}/templates/rayservice.tmpl.yaml" "${RENDER_DIR}/tenant-a-rayservice.yaml"
  kubectl apply -f "${RENDER_DIR}/tenant-a-rayservice.yaml"

  log "渲染并部署租户 B RayService..."
  export TENANT_NAMESPACE="tenant-b"
  render_env_file "${K8S_DIR}/templates/rayservice.tmpl.yaml" "${RENDER_DIR}/tenant-b-rayservice.yaml"
  kubectl apply -f "${RENDER_DIR}/tenant-b-rayservice.yaml"

  log "等待租户推理 endpoint 就绪..."
  wait_inference_service_endpoint tenant-a 1800
  wait_inference_service_endpoint tenant-b 1800

  log "等待前端 Deployment 就绪..."
  if ! wait_webapp_ready 360; then
    echo "前端 Deployment 未在预期时间内就绪" >&2
    kubectl get pods -n platform -o wide || true
    kubectl describe deploy qwen-webapp -n platform || true
    exit 1
  fi

  log "等待 LoadBalancer 外网地址..."
  local external_addr
  if ! external_addr="$(wait_webapp_external_ip 900)"; then
    echo "未获取到 qwen-webapp 的外网地址" >&2
    kubectl get svc -n platform qwen-webapp -o wide || true
    exit 1
  fi

  log "外网地址: http://${external_addr}"

  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 15 "http://${external_addr}/api/health" >/dev/null; then
      log "外网健康检查通过: http://${external_addr}/api/health"
    else
      log "警告：外网健康检查失败，请检查 SLB 安全组、监听和后端节点放通"
    fi
  fi

  log "部署完成"
  kubectl get deploy,svc -n platform -o wide
}

main "$@"
