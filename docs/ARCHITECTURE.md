# Humpback 架构

完整的设计说明（含图）见 Claude 文档《Humpback 总体架构与分阶段路线》。本文件只记录代码层面需要知道的内容。

## 一条订单的路径

```
OrderIntent
  → 能力声明检查（不支持的类型在本地报 UNSUPPORTED_CAPABILITY）
  → 精度检查（各场所自己的规则）
  → 账本记录意图（按 business_key 幂等：同一决策只记一次，内容不同直接报错）
  → 风控闸门（成交后最坏情况的敞口、模式、额度、频率）→ 签发 HMAC 批准凭证
  → 签名服务（核对凭证、白名单、防重放、分配 nonce）
  → 账本写入 SUBMIT_STARTED（先落盘，再发送）
  → 场所：ACCEPTED / REJECTED / 结果未知 → UNKNOWN_IN_FLIGHT
  → 成交按场所成交 ID 只入账一次
```

提交从不自动重发。结果未知的订单由 `recon.resolver` 按客户端订单号向场所查询；只有场所给出明确状态，或连续多次查不到且已超过宽限时间，才会解除。

## 运行模式

`risk.supervisor` 取所有未解除原因中最严格的模式：

| 模式 | 触发原因 | 允许 |
| --- | --- | --- |
| `NORMAL` | 无 | 全部 |
| `REDUCE_ONLY` | 有未知订单、当日亏损达到上限 | 只允许真正减仓的单和撤单 |
| `HALT_NEW_ORDERS` | 对账不一致、启动锁定、紧急停止 | 撤单和只减仓的平仓单 |
| `HALTED` | 人工全停 | 只允许撤单 |

每个原因只能由提出它的组件凭证据解除，不能靠计时器自动解除。

## 密钥分级

| 级别 | 是什么 | 放在哪里 |
| --- | --- | --- |
| L0 | 硬件钱包 / Safe 多签 | 离线，只做出入金和授权代理 |
| L1 | 各场所代理密钥（不能提现；Lighter 的 Key 例外，只能提到账户自己的 L1 地址） | 只在签名服务进程里 |
| L2 | 公开地址 | 配置文件，只读查询 |

签名服务的白名单永远不能包含 `WITHDRAW`、`TRANSFER`、`APPROVE_AGENT`。这条由构造函数强制。

## 已核实的场所事实（2026-11-05）

| 场所 | 事实 | 代码位置 |
| --- | --- | --- |
| Hyperliquid | L1 动作：EIP-712 域 `Exchange`、chainId 1337；订单字段顺序为 `a,b,p,s,r,t,c`；每个签名者保留最近 100 个 nonce；cloid 为 16 字节；没有原生市价单 | `venues/hyperliquid.py`、`signer/backends.py` |
| Aster V3 | EIP-712 `AsterSignTransaction` v1，chainId 1666；nonce 为微秒；代理权限 `canWithdraw` 必须设为 false；HTTP 503 表示结果未知；V1 已于 2026-11-02 从文档移除 | `venues/aster.py` |
| Lighter | 主网 chainId 304，测试网 300；API Key 槽位 4–254；价格和数量以整数上传；`sendTx` 返回 200 不代表已执行 | `venues/lighter.py` |

待核实：Aster 代理模式下签名消息是否需要包含 `user`；Hyperliquid 的最小订单金额；Lighter 市价单的价格上限参数。

## 代码规范

Python 3.12、`ruff`（lint 与格式化）、`mypy --strict`、`pytest`。数据模型使用 `dataclass(frozen=True, slots=True)`；时间统一为 UTC 毫秒。代码和注释用英文，文档用中文。
