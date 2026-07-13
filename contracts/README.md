# contracts —— 双实现与两平面共享的唯一接口

本目录是主实现(Rust)与参考实现(独立仓库,Python)之间**唯一**的共享物,
也是运行态输出协议(claim / run manifest)的事实源。变更闸门见 INV-05:先 RFC,本人批准。

| 文件 | 内容 |
| --- | --- |
| `paipan-spec.md` | 排盘算法数学规格(条款编号 + 判界 + 舍入 + tolerance) |
| `paipan.schema.json` | 对拍 I/O 协议(JSONL 逐行)的 JSON Schema |
| `claim.schema.json` | 运行态论断结构(含 claim_type 分型,dev-plan 3.4) |
| `run-manifest.schema.json` | 每次模型调用的落盘清单(dev-plan 3.3) |

参考仓库以镜像方式携带本目录副本;两侧以主仓库 main 上的 contracts 版本(commit)对齐。
