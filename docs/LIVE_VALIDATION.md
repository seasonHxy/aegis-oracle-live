# Aegis Oracle Live 0.2 验收记录

验收日期：2026-09-20。项目采用 Rust + Solidity，真实行情来自 Coinbase Exchange、Kraken 和 Bitstamp 的公开 USD 成交接口。

## 本地完整检查

`make check` 已通过：Rust 35 项、Solidity 17 项（其中一个 fuzz 测试运行 256 次）、Python 兼容层 25 项；另有 Python/Rust 迁移对照 5 组、本地端到端检查 15 项。编译、rustfmt、Clippy、Forge 格式及 ABI 一致性检查通过。测试数量不等同于覆盖率。

本机因默认 Cargo 缓存权限问题，执行时使用 `CARGO_HOME=/private/tmp/aegis-cargo CARGO_NET_OFFLINE=true make check`。常规环境使用 README 中的命令即可。

## 真实行情验证

- ETH/USD 连续 3 次采样全部 `dry_run`，每次有 3 个来源 / 3 个独立组，`simulated=false`；[原始采样结果](evidence/live-eth-usd.json)。
- 初次直连因本机网络环境发生超时和传输错误，节点正确阻止聚合；[失败记录](evidence/live-direct-network-failure.json)。显式配置本机 HTTP 代理后采样通过，未降低 quorum 或放宽报价时效。
- `scripts/demo.py --live-data --no-build --port 8788` 已完成真实行情采集、EIP-712 签名、本地 Anvil 发布与链上读取验证，状态 `published`、链 ID `31337`；[状态快照](evidence/live-anvil.json)。交易哈希属于一次性本地链，不能在公共区块浏览器查询。
- 浏览器检查确认页面显示三个交易所的报价、成交时间、请求耗时、3 / 3 独立来源以及“价格已确认”，ETH 估值及 70% 示例额度正常显示。

记录是一次点时验证，不代表长期可用性、吞吐量或延迟 SLA。BTC/USD 有配置与解析器测试，本轮未执行 BTC 实网采样。未部署公共测试网或主网，未实施独立安全审计。

## 可复现步骤

```bash
make check
python3 scripts/live_smoke.py --output state/live-smoke.json
python3 scripts/demo.py --live-data
```

代理按需通过 `AEGIS_HTTP_PROXY` 提供。实时网络检查可能因交易所不可达、最近无新成交、限流或风险阈值而失败；不会自动改用模拟报价。确定性 CI 不依赖外部行情在线。
