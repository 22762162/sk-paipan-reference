# 当前任务 · ref-implementer(盲写参考实现)

> 在 Codex **独立会话**中打开本仓库执行;先读 `AGENTS.md` 的硬性约束。

## 任务

依据 `contracts/paipan-spec.md`(以其版本状态行为准)重写 `paipan_ref/`:
当前占位实现由非独立会话产出,不满足异源独立性,须**整体盲写替换**
(可保留包结构与 CLI 入口约定:`python3 -m paipan_ref.cli`)。

规格范围:spec 当前已评审通过的全部条款(v0.1 = 年柱 YP-1/YP-2/YP-3 + 附录 A 协议);
spec 升级到 v0.2(四柱)后,本任务随之扩展,以 spec 条款编号为准。

## 交付

- `paipan_ref/` 实现 + `tests/` 单元测试(边界三连测覆盖每个判界条款);
- `SPEC-QUESTIONS.md`:实现中发现的 spec 模糊点(交主仓库规格流程消歧);
- 全部测试通过:`python3 -m pytest -q tests`。
