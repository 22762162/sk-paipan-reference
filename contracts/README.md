# sk-contracts —— 三鉴契约仓(唯一事实源)

主实现(sk/engine-paipan,Rust)、参考实现(sk-paipan-reference,Python)与运行态输出协议
之间**唯一**的共享物。以**不可变 tag** 发版;两侧以钉版 vendored 副本 + `contracts.lock`
引用,CI 校验副本与本仓 tag 内容一致(DESIGN V3.0 §4)。

| 文件 | 内容 |
| --- | --- |
| `paipan-spec.md` | 排盘算法数学规格(条款编号 + 判界 + 舍入;条款-测试映射见附录 C) |
| `schemas/paipan.schema.json` | 对拍 I/O 协议(JSONL) |
| `schemas/claim.schema.json` | 运行态论断结构(claim_type 分型、证据逐条绑定、结构化 confidence) |
| `schemas/run-manifest.schema.json` | 每次模型调用落盘清单 |
| `schemas/consultation-manifest.schema.json` | 每场会诊落盘清单(实验臂、拉丁方分配、judge 盲评) |
| `public-fixtures/` | 协议格式示例(占位数值,不代表历法事实) |

**边界纪律**:canonicalization 只做 JSON 字段排序、数值格式、序列化、枚举归一化;
**禁止**放入节气、日期、时区、四柱等任何领域计算。
变更闸门:先 RFC(主仓 docs/specs/)经本人批准 → 本仓合入 → 发新 tag;历史 tag 永不改写。
