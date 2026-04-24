from modelscope import snapshot_download
import os

# ========== 1. 选一个你要下的版本 ==========
# 方案A: AWQ INT4（推荐vLLM）
model_id = "qwen/Qwen2.5-7B-Instruct-AWQ"
save_dir = "./Qwen2.5-7B-Instruct-AWQ"

# 方案B: GGUF Q4（轻量）
# model_id = "qwen/Qwen2.5-7B-Instruct-GGUF"
# save_dir = "./Qwen2.5-7B-Instruct-GGUF"

# ========== 2. 开始下载 ==========
print(f"开始从阿里云 ModelScope 下载: {model_id}")
model_dir = snapshot_download(
    model_id,
    local_dir=save_dir,
    revision="master"
)

print(f"\n✅ 下载完成！路径: {os.path.abspath(model_dir)}")
print("接下来可以把整个文件夹上传到阿里云 OSS")