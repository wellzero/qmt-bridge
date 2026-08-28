from qmt_bridge import QMTClient

client = QMTClient(host="localhost", port=8083)

# 历史 K 线
df = client.get_history("000001.SZ", period="1d", count=60)

# 增强版 K 线，前复权
dfs = client.get_history_ex(
    ["000001.SZ", "600519.SH"],
    dividend_type="front",
    count=60,
)

# 大盘行情一览
indices = client.get_major_indices()

print("indices:", indices)
