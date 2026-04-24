import requests
import time
import concurrent.futures
import statistics

# ====================== INT8 配置 ======================
API_URL = ""
TOKEN = ""
MODEL_NAME = "/mnt/data/Qwen2.5-7B-Instruct-GPTQ-Int8"

CONCURRENCY = 5
REQUESTS = 30
MAX_TOKENS = 256
# ======================================================

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def single_request(idx):
    start = time.time()
    try:
        resp = requests.post(API_URL, headers=HEADERS, json={
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": "请简单介绍人工智能。"}],
            "temperature": 0.7,
            "max_tokens": MAX_TOKENS,
            "stream": False
        }, timeout=60)
        cost = time.time() - start
        return cost, resp.status_code == 200
    except Exception as e:
        return -1, False

def pressure_test():
    print("=" * 50)
    print("🔥 INT8 性能压测（最终实验）")
    print(f"并发：{CONCURRENCY} | 请求数：{REQUESTS}")
    print("=" * 50)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(single_request, range(REQUESTS)))
    total_time = time.time() - t0

    success_results = [r for r in results if r[1]]
    success_count = len(success_results)
    latencies = [r[0] for r in success_results]

    if latencies:
        avg_latency = statistics.mean(latencies)
        p95_latency = statistics.quantiles(latencies, n=100)[94]
        throughput = success_count / total_time * 60
    else:
        avg_latency = p95_latency = throughput = 0

    print("\n📊 INT8 压测结果：")
    print(f"成功请求数：{success_count}/{REQUESTS}")
    print(f"总耗时：{total_time:.2f}s")
    print(f"平均延迟：{avg_latency:.2f}s")
    print(f"P95 延迟：{p95_latency:.2f}s")
    print(f"吞吐量：{throughput:.2f} req/min")
    print(f"成功率：{success_count/REQUESTS*100:.1f}%")
    print("=" * 50)

if __name__ == "__main__":
    pressure_test()