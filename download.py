import os

# 国内镜像（必须加，否则必超时）
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# 超大超时 + 超多重试
os.environ["HUGGINGFACE_HUB_DOWNLOAD_TIMEOUT"] = "9999"
os.environ["HUGGINGFACE_HUB_MAX_RETRY"] = "50"

from huggingface_hub import snapshot_download

# 公开模型，无需登录、无需权限
model_name = "shenzhi-wang/Llama3-8B-Chinese-Chat"

# 保存路径
save_path = "D:/Download_APP/llama3-8b"

print("开始下载（单线程，最稳定）...")

snapshot_download(
    repo_id=model_name,
    local_dir=save_path,
    max_workers=1,  # 单线程 = 最稳
)

print("下载完成！模型在：", save_path)