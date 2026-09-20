# 真实行情接入设计

## 官方接口依据

- Coinbase：[Get product ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)，最新成交、bid/ask、成交时间。
- Kraken：[Get Recent Trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades)，trade 行含价格与 Unix 秒时间。
- Bitstamp：[API / Transactions](https://www.bitstamp.net/api/)，`/api/v2/transactions/{market}/?time=minute`，每笔成交含 `price`、`date` 和 `tid`。

公开接口无需账户密钥；接口可用性、访问地区、限流与服务条款由各服务方决定。此项目不宣称获得生产行情授权或商业转售权。

## 固定交易对与来源身份

内置适配器只接受 `ETH/USD` 和 `BTC/USD`；URL 由代码映射，不能配置为另一种计价资产。Kraken 同时检查返回市场键 `XETHZUSD` / `XXBTZUSD`，并拒绝非空错误数组。Coinbase/Bitstamp 的响应不附完整交易对，语义依赖固定 HTTPS 请求路径。

内置组名分别固定为 `coinbase`、`kraken`、`bitstamp`。这是交易场所分组，不是对供应商关联关系、市场操纵抵抗能力或经济独立性的证明。使用其他接口时，应独立核实底层来源。

## 时间与价格

所有内置适配器输出最新成交价，避免混合成交价与 ticker 的统计均价。Coinbase 另外使用 bid/ask 校验价差，但不会把中间价替换为成交价。

- Coinbase RFC3339 时间允许亚秒与时区，转换为 Unix 毫秒；无时区拒绝。
- Kraken 用十进制解析秒数，再向下取整为毫秒；`last` 是游标，不作为价格时间。
- Bitstamp 取最大 `(date,tid)`，不依赖响应数组排序；无成交返回空集时拒绝报价。
- 不使用 HTTP Date、响应接收时间或服务器 time 接口“刷新”陈旧成交。
- `received_at_ms` 与 `latency_ms` 只用于诊断。freshness 使用 `observed_at_ms`。

各交易所最后成交可能不同步。60 秒阈值和三个来源 quorum 是演示参数；低活跃度或网络故障时应拒绝，而不是无限延长有效期。

## HTTP 行为

复用 reqwest 客户端与连接池，禁用自动系统代理与重定向。请求限制 4 秒，外层单次 fetch 5 秒，最多 1 MB 响应。网络错误及 5xx 最多重试一次，中间等待 200ms；一个来源最多约 10.2 秒。正常轮询下限 10 秒。

429 返回时不重试，读取 Retry-After 的秒数或 HTTP 日期，默认 30 秒，当前退避限制在 1..300 秒。持续进程将退避保存在内存中，重启不保留；这是单实例演示策略，不是跨实例共享限流器。

错误输出为 `http_403`、`http_429_backoff_*`、`rate_limited_backoff`、`timeout`、`transport_error`、`invalid_payload` 或 `response_too_large`。不把服务商错误正文、URL 查询参数或凭据写入日志。

## 测试设计

解析器使用固定样本覆盖时间单位、市场键、缺字段、空成交列表与排序，HTTP 用本地服务器覆盖 500 后成功、429 下个周期退避、403 不重试。CI 不需要真实行情服务在线。

`scripts/live_smoke.py` 才会请求真实市场。保留每个来源报价、成交时间、接收时间、耗时、聚合/拒绝原因与退出码；失败也会输出证据，不替换价格、不降低 quorum。每个 sample 是独立 dry-run 进程，因此该脚本不验证跨周期限流；它验证当前网络可达性、解析和数据质量。

## 剩余改进

WebSocket 增量行情、生产 SLA、持续运行基准、跨重启限流、RPC 加价替换、多节点共识、公共网络最终性和独立安全审计均不在此次真实行情升级交付中。

## 显式网络代理

节点默认直连，不自动读取操作系统代理。网络需要代理时可设置 `AEGIS_HTTP_PROXY=http://127.0.0.1:7890`（端口仅为示例）。支持 HTTP/HTTPS 代理，TLS 校验保持开启，localhost、127.0.0.1、::1 绕过代理；不要把代理凭据提交到仓库。

```sh
AEGIS_HTTP_PROXY=http://127.0.0.1:7890 python3 scripts/live_smoke.py
```
