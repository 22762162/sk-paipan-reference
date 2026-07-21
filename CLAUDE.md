# Claude Code · AIFOS 协作入口

请先完整读取根目录 `AGENTS.md`，其中的 Git、并行开发、真实生成安全与测试
规则对 Claude Code 和 Codex 同时生效。

核心原则：两位 AI 维护的是同一套 AIFOS。所有新增能力通过独立提交和普通
merge 汇合；不得在脏工作区拉取，不得 reset/stash/覆盖另一位 AI 的修改，
不得在真实图片或视频生成期间切换代码或重启服务。
