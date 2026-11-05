# Humpback 座头鲸

自用的中低频链上量化交易系统，交易 Hyperliquid、Lighter、Aster 上的加密永续和美股永续。

不做高频，不做纯速度套利。护城河是三件事：不出错的底座、结构上防住前视偏差的研究流水线、对新市场规则的深入理解。

> 当前阶段：**第一阶段（安全底座）**。只在本地模拟和测试网运行，不做任何主网下单或转账。

## 已经有什么

| 模块 | 作用 |
| --- | --- |
| `core` | Decimal 金额类型（拒绝 float）、时钟、ID、错误码 |
| `domain` | 标的、订单意图、成交、账户快照 |
| `ledger` | 只追加的 SQLite 事件库；订单状态机（含 `UNKNOWN_IN_FLIGHT`）；订单、成交、持仓、保证金四账 |
| `venues` | 统一接口 + 能力声明；Hyperliquid、Aster V3、Lighter 的编码器和精度规则；带故障注入的模拟场所 |
| `risk` | 风控闸门（签发 HMAC 批准凭证）、运行模式监督、紧急停止三阶段 |
| `signer` | 独立签名服务：动作白名单、批准凭证校验、防重放、按签名者分配 nonce |
| `recon` | 未知订单解析、定时对账、启动恢复 |
| `capital` | 跨场所资金调度状态机，只凭到账证据解锁 |
| `audit` | 只追加审计日志，全链路 `trace_id` |
| `research` | 第三阶段的接口契约（只有定义，没有策略） |

## 运行检查

```bash
uv venv && uv pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy
pytest -q
```

## 规矩

- 钱只用 `Decimal`。CI 会扫描源码里的 float。
- 运行时没有 LLM。策略只能产出交易意图，必须经过风控闸门。
- 结果不确定就当作不安全：超时一律记为 `UNKNOWN_IN_FLIGHT`，只能凭交易所的证据解除。
- 密钥不进仓库、不进日志。签名服务从加密 keystore 读取密钥。
- 提交信息使用 Conventional Commits 格式；`main` 分支只通过 PR 合并。

文档：[架构](docs/ARCHITECTURE.md) · [路线](docs/ROADMAP.md) · [决策记录](docs/DECISIONS.md)
