# Aegis Oracle Live

[![verify](https://github.com/seasonHxy/aegis-oracle-live/actions/workflows/ci.yml/badge.svg)](https://github.com/seasonHxy/aegis-oracle-live/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A Rust + Solidity oracle MVP that aggregates real USD trade prices from Coinbase, Kraken and Bitstamp, then verifies signed reports on an EVM chain.**

面向面试展示与工程学习的多源价格预言机：真实行情接入、源时间校验、异常过滤、EIP-712 签名、链上验证、交易恢复，以及可交互的中文监控页面。

**演示范围明确：真实行情来自公开 REST API；合约演示使用本地 Anvil；单签名者，不是去中心化共识网络，也不代表可直接承载主网资产。**

## 快速体验

需要 Rust（`rust-toolchain.toml` 固定版本）、Foundry（`forge` / `cast` / `anvil`）、Python 3.11+。首次编译需下载依赖。

```bash
git clone https://github.com/seasonHxy/aegis-oracle-live.git
cd aegis-oracle-live

# 真实 ETH/USD 行情 + 监控页面；不需要 API Key、钱包或资金
cargo run --locked -- --config config/live-eth-usd.json
```

若网络需要代理，设置 `AEGIS_HTTP_PROXY`，详见 [代理配置](docs/LIVE_MARKET_DATA.md#显式网络代理)。

打开 **http://127.0.0.1:8787**。页面显示每个交易所的报价、成交时间、请求耗时、有效来源及过滤结果。交易所不可达或报价过期时显示失败，不自动使用模拟价格。

```bash
# 单次真实行情校验
cargo run --locked -- --config config/live-eth-usd.json --once

# 真实行情 → 签名 → 本地链 → 抵押估值：一键完整演示
python3 scripts/demo.py --live-data

# 没有外网时，仍可使用明确标识的模拟数据演示
python3 scripts/demo.py
```

后两条命令会启动自己的 Anvil、部署 AegisOracle 和 CollateralLens；Ctrl+C 清理进程。`--port 8788` 可更换页面端口。演示使用公开 Anvil 测试密钥，不能用于任何公共网络资金。

## 行情语义

三家来源使用 **USD 计价的最新成交价**，没有把 USDT 或 USDC 当作 USD。成交时间来自交易所响应，收到响应的本机时间仅用于可观察性。

| 来源 | 接口 | 价格 / 源时间 |
|---|---|---|
| Coinbase Exchange | 产品 ticker | `price` / RFC3339 `time`，保留到毫秒 |
| Kraken | Recent Trades | 成交行价格 / 十进制 Unix 秒，向下转换为毫秒 |
| Bitstamp | 最近一分钟 transactions | 最新 `(date, tid)` 的 `price` / Unix 秒 |

- 内置 ETH/USD 和 BTC/USD，分别见 `config/live-eth-usd.json`、`config/live-btc-usd.json`。
- 默认每 10 秒请求一次，每份报价最长 60 秒；过滤后要求 **3 个来源和 3 个交易所组**。任一来源失效可能阻止发布，这是本演示选择的可用性与数据要求权衡。
- 不同接口但同一交易所不能冒充独立组，内置适配器会校验组名。
- 价格统一为 8 位定点整数，使用加权中位数、MAD 异常过滤、IQR 离散度与价格跳变熔断。
- 仅对网络故障 / 5xx 做一次短暂重试。429 不立即重试，持续运行时在该来源的后续周期退避；错误日志输出分类代码，不输出凭据或完整请求 URL。
- REST 演示不承诺逐笔实时性；网络、交易活跃度、交易所限流均可能使报价不可用。

详细接口依据与边界见 [真实行情设计](docs/LIVE_MARKET_DATA.md)。

## 架构

```text
Coinbase ETH-USD ─┐
Kraken ETHUSD ────┼→ Rust collector → source timestamp / quorum checks
Bitstamp ethusd ──┘                         ↓
                               group median → MAD → risk checks
                                           ↓
                           EIP-712 report + durable transaction journal
                                           ↓
                               Solidity AegisOracle
                                           ↓
                        checked latestPrice → CollateralLens
```

`CollateralLens.quote` 仅计算 18 位精度 ETH 数量的美元估值和示例 70% LTV 上限，不存款、不放贷、不清算。网页试算为展示，精确结果以合约调用为准。

## 测试与证据

```bash
# 确定性测试，不依赖交易所当时是否在线
make check

# 自愿联网检查：真实数据，默认连续采样 3 次，结果写入 state/
make live-smoke
```

CI 运行解析器、配置、HTTP 重试/退避、聚合、签名、合约、状态恢复和本地端到端测试。真实交易所检查独立于 CI，避免把外部服务故障误判为代码回归。

- [本版本验收](docs/LIVE_VALIDATION.md)：实际运行结果与未覆盖范围。
- [实网采样证据](docs/evidence/live-eth-usd.json)：带时间戳的点时观察，不是长期 SLA。
- [场景与测试](docs/SCENARIOS_AND_TESTS.md)：继承场景的详细矩阵；本版本增量以真实行情文档为准。
- [原版本验收](docs/VALIDATION.md)：保留的基线测试记录。

## 适合面试讨论的工程取舍

1. **为什么最新成交价配成交时间？** 刚收到 HTTP 响应并不能证明市场报价新鲜。
2. **为什么三个来源不等于去中心化？** 数据来源分散，报告签名者仍然只有一个。
3. **为什么不用浮点处理价格？** Rust 和 Solidity 必须共享固定精度语义。
4. **为什么先保存交易再广播？** 服务重启后可以追踪或重播同一笔交易，减少 nonce 不确定性。
5. **为什么拆分确定性测试和实网检查？** 分别验证软件逻辑与当前外部可用性。

两分钟讲解见 [面试说明](docs/PROJECT_DESCRIPTION.md)。

## 部署与范围

Rust 发布器仅允许 Anvil、Sepolia 和 Base Sepolia；公共测试网需要自己的可信 RPC、测试币和 `ORACLE_PRIVATE_KEY`，参见 [架构文档](docs/ARCHITECTURE.md)。当前已实现能力不意味着已部署公共网络。

- 源码不含真实私钥、API Key 或运行状态。`state/`、`.env*`、部署输出、编译产物不进入版本控制。
- 未实现多签名者共识、主备切换、自动加价替换、完整借贷协议或独立安全审计。
- 页面仅监听 loopback，不是在线托管服务；GitHub 仓库公开不等于网页已部署。
- BTC/USD 提供采集配置；默认部署脚本与 ETH 估值演示使用 ETH/USD。

基于 [seasonHxy/hip3-oracle](https://github.com/seasonHxy/hip3-oracle) 的聚合与风控设计扩展，保留 MIT 授权及 `legacy/hip3` Python 实现。HIP-3 兼容层独立运行，本项目不做 HyperCore/EVM 双目标原子发布。来源见 [NOTICE](NOTICE)，安全边界见 [SECURITY.md](SECURITY.md)。
