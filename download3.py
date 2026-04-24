from modelscope import snapshot_download

# ======================================
# 千问官方 INT8 模型（你要的 8bit 版本）
# ======================================
model_id = "qwen/Qwen2.5-7B-Instruct-GPTQ-Int8"
save_dir = "./Qwen2.5-7B-Instruct-GPTQ-Int8"

print("=" * 50)
print("开始下载 通义千问 7B INT8 模型")
print("模型：Qwen2.5-7B-Instruct-GPTQ-Int8")
print("量化：INT8 (GPTQ)")
print("保存路径：" + save_dir)
print("=" * 50)

# 开始下载
model_path = snapshot_download(
    model_id,
    local_dir=save_dir,
    revision="master"
)

print("\n✅ INT8 模型下载完成！")
print("本地路径：", model_path)