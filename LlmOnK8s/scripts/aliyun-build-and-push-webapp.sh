#!/usr/bin/env bash
set -euo pipefail

# 阿里云仅 WebApp 镜像构建和推送脚本
# 不构建、不推送 inference 镜像

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "缺少环境变量: ${name}" >&2
    exit 1
  fi
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

    # 已包含 namespace 或者已经是多级路径时，不再重复拼接 namespace
    if [[ "$repo" == "${ns}" || "$repo" == "${ns}/"* || "$repo" == */* ]]; then
      echo "$repo"
      return
    fi

    echo "${ns}/${repo}"
    return
  fi

  echo "$repo"
}

require_var ALIYUN_REGION
require_var ALIYUN_REGISTRY
require_var ACR_WEBAPP_REPO

ALIYUN_REGISTRY="$(normalize_registry_host "${ALIYUN_REGISTRY}")"
export ALIYUN_REGISTRY

normalized_web_repo="$(normalize_repo "${ACR_WEBAPP_REPO}")"

export IMAGE_TAG="${IMAGE_TAG:-latest}"
export WEBAPP_IMAGE="${ALIYUN_REGISTRY}/${normalized_web_repo}:${IMAGE_TAG}"

echo "======================================"
echo "阿里云仅 WebApp 镜像构建和推送配置"
echo "======================================"
echo "阿里云地域: ${ALIYUN_REGION}"
echo "阿里云镜像仓库: ${ALIYUN_REGISTRY}"
echo "Web应用镜像: ${WEBAPP_IMAGE}"
echo "镜像标签: ${IMAGE_TAG}"
echo "======================================"

if [[ -n "${ALIYUN_REGISTRY_USERNAME:-}" && -n "${ALIYUN_REGISTRY_PASSWORD:-}" ]]; then
  log "使用用户名密码登录 ACR..."
  echo "${ALIYUN_REGISTRY_PASSWORD}" | docker login \
    --username "${ALIYUN_REGISTRY_USERNAME}" \
    --password-stdin \
    "${ALIYUN_REGISTRY}"
elif command -v aliyun >/dev/null 2>&1; then
  log "使用阿里云 CLI 凭证登录 ACR..."
  aliyun cr GetAuthorizationToken --region-id "${ALIYUN_REGION}" --output json 2>/dev/null | \
    jq -r '.data.authorizationToken' | \
    base64 -d | \
    docker login --username "${ALIYUN_REGISTRY}" --password-stdin "${ALIYUN_REGISTRY}"
else
  echo "错误：未找到登录凭证" >&2
  echo "请设置 ALIYUN_REGISTRY_USERNAME 和 ALIYUN_REGISTRY_PASSWORD" >&2
  echo "或安装阿里云 CLI 并配置凭证" >&2
  exit 1
fi

log "开始构建 WebApp 镜像..."
docker build -f webapp/Dockerfile -t "${WEBAPP_IMAGE}" .

log "推送 WebApp 镜像到阿里云..."
docker push "${WEBAPP_IMAGE}"

log "仅 WebApp 镜像构建和推送完成"
echo "已推送镜像: ${WEBAPP_IMAGE}"
