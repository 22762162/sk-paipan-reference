# sk-paipan-reference(L1 参考实现 · Python · 独立仓库)

> 本仓库另含 **AIFOS V3.2 · SK 五维 AI 精品漫剧工业化生产平台**(独立于排盘参考实现),
> 见 [`aifos/README.md`](aifos/README.md);入口:`python3 -m aifos`。
> V3.2 新增可版本化的**制作标准中心**、分层开拍门禁与逐集标准快照。
> 规则分层、冲突优先级、一次综合质检与无污染重制原则见
> [`docs/RULE_GOVERNANCE.md`](docs/RULE_GOVERNANCE.md)。

三鉴排盘引擎的独立参考实现,**仅用于与主仓库 Rust 实现差分对拍**,不对外提供服务。
本仓库是 Codex 参考实现者的专属工作区(主仓库 dev-plan V2.1 第 1、4 节)。

## 盲隔离声明(INV-09)

- 本仓库**不挂载主仓库、不持有主仓库凭据**;实现的唯一依据是本仓库携带的 `contracts/` 镜像。
- 正式流程中由 **Codex 独立会话盲写**(任务说明:`TASK.md`);触碰过主仓库 L1 代码的会话不得承接本仓库工作。
- `paipan_ref/` 已由独立 Codex 会话仅依据 `contracts/paipan-spec.md` v0.2 与 `contracts/schemas/paipan.schema.json` 盲写重构；现实现 `year_pillar`、`four_pillars` 全部 YP/LT/MP/DP/HP 条款及附录 A JSONL 协议，并以合成注入值覆盖各判界三连测。

## 约束

仅 Python 标准库(测试用 pytest);零第三方运行时依赖;确定性(无网络/时钟/无种子随机);历法事实仅来自注入参数或数据文件。

## 运行

```bash
python3 -m pytest -q tests          # 单元测试
echo '{"case_id":"x","op":"year_pillar","input":{"civil_year":1984,"t_unix":1,"lichun_unix":0}}' \
  | python3 -m paipan_ref.cli       # 对拍 CLI(contracts/paipan-spec.md 附录 A 协议)
```

## contracts 镜像同步

`contracts/` 为主仓库同名目录的只读镜像,以主仓库 main 的 commit 对齐;
同步由主仓库维护者(人)执行,Codex 不改契约——契约疑问写入 `SPEC-QUESTIONS.md`。
