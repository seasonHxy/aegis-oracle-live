# 架构与恢复流程

## 边界

Aegis 的 Rust 路径是一进程一 feed、单签名者、固定 8 位报价精度的 EVM 预言机。Python HIP-3 路径保留为独立兼容实现。两者不共用发布状态，不保证双目标同步成功。

```text
Coinbase / Kraken / Bitstamp / JSON REST / static fixtures
          │ concurrent fetch, bounded timeout + one transient retry
          ▼
source validation (price, source timestamp, bid/ask)
          ▼
independence group collapse → weighted median → MAD → quorum → IQR
          ▼
compare against confirmed chain price (dry-run: isolated local reference)
          ▼
EIP-712 report → authorized signer
          ▼
sign transaction → durable journal → broadcast / rebroadcast same bytes
          ▼
receipt + confirmations → checked on-chain read
          ▼
AegisOracle.latestPrice → CollateralLens.quote
```

## 与 Python 基线的差异

保留分组加权中位数、组权重取成员最大值、MAD 异常组过滤、过滤前后 quorum、IQR 离散度、价格跳变熔断。

Rust 有意更严格：

- 价格统一为 8 位无符号定点整数，输入不接受超过 8 位有效小数，不使用浮点金融计算。
- 当前支持的价格上限是 10^18 个最小单位（100 亿美元），确保差值、bps 等中间运算有界。
- 权重限定为 1..1000 的整数。
- confidence 按 bps 向上取整，因此在阈值边界可能比 Python 更保守。
- 时间戳不允许来自未来，不以本机接收时间替代来源时间。
- Rust 路径面向 24/7 资产，不迁移股票市场开闭市语义。
- Rust 一进程一 feed；Python 仍保留 HIP-3 全 feed 批次行为。
- 每个独立组仍可配置不同权重；“独立分组”不代表等权共识。配置者必须防止单一供应商占据过高权重。

## 报告格式

EIP-712 domain：`name=AegisOracle`、`version=1`、实际 chainId、验证合约地址。

```text
Report(bytes32 feedId,uint128 price,uint64 observedAt,uint64 validUntil,uint64 sequence,uint32 confidenceBps)
```

`feedId = keccak256("ETH/USD")`。`observedAt` 取过滤后最旧源时间（秒）；`validUntil = observedAt + max_age_secs`。合约要求报告序号严格递增、来源时间不倒退、报价正数、有效期不超过配置。

签名者与交易提交者可以不同，合约依赖恢复出的签名地址授权。Rust MVP 默认由同一个钱包签报告和交易，使用一个串行循环避免本进程 nonce 竞争。不要将该钱包复用于其他实例或人工发交易。

## 发布与恢复

1. 启动校验 chainId、合约代码、授权 signer 和 feed 配置。
2. 启动/每轮先检查持久化的 pending journal。
3. 新报告序号从链上 `latestReport` 读取，不依赖本地计数。
4. 填充交易 nonce、gas、chainId 并签名，保存原始交易和哈希，fsync 后才广播。
5. RPC 超时或进程退出后，根据同一个交易哈希查询；节点遗忘时重播同一份字节。不会盲目创建新 nonce。
6. 本地链确认一次，公共测试网确认两次。未达到确认数时保留 journal，阻止新报告。
7. 失败回执会清除已结束交易的 journal，当前周期失败；后续周期可按新鲜数据重试。
8. 成功后调用 checked price 接口校验链上结果，更新状态。

无法自动解决的情形：同一 nonce 被外部钱包交易替换、长期低费交易、RPC 不一致、深度链重组。此时保留状态并阻止继续发布，需要核对交易与 nonce 后处理，不支持自动加价替换。两次确认不是最终性保证。

## 熔断恢复

价格跳变拒绝发布，持续使用上一笔已确认价作为基准。不能通过“重启”“等几次”或删除本地状态绕过 EVM 跳价检查。

MVP 不提供一键重新锚定。真实跳变经核实后，由运营者调整 `max_jump_bps` 配置再恢复，并记录变更；先恢复旧配置下的待处理交易，避免遗留 journal。若合约暂停或价格过期，checked consumer 拒绝估值。

## 状态与可观察性

- 默认状态命名包含模式、chainId、合约、feed 与完整配置哈希，避免模拟和正式状态混用。
- 文件锁只保护使用同一路径的进程，不是分布式锁。不能以不同状态文件启动同一 signer 的多个实例。
- 原子替换、文件 fsync、父目录 fsync，状态损坏时拒绝启动。
- `/api/status`：本轮源报价、错误、聚合、发布状态、已确认交易、最多 120 个成功周期。
- `/healthz`：本轮失败、未确认或过期返回 503。dry-run 的 200 仅表示模拟管线健康，不表示链上可用。
- 页面使用源时间计算年龄；连接中断或非成功周期禁用估值，避免用旧页面内容当作当前可用报价。
- 状态服务只监听 loopback，无修改权限接口。不提供管理员或私钥操作。
