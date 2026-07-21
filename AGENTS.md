# AIFOS 双 AI 协作规则

本分支用于 AIFOS（AI 精品漫剧工业化生产平台）。Claude Code 与 Codex
共同开发同一产品，任何一方的新增功能都必须通过 Git 合并进入共同分支，
禁止用整目录或整文件覆盖另一方的工作。

## 共同基线

- 正式仓库：`/Users/sk/AIFOS`
- 共同分支：`claude/sk-manga-drama-platform-b8nbe3`
- 正式测试服务：`http://127.0.0.1:8619/`
- 每次开始开发前先执行 `git fetch origin`、`git status --short --branch`。
- 工作区不干净时禁止 `pull`、`rebase`、`stash`、`reset` 或切换分支。
- 禁止强推；远端前进时只允许再次普通 merge。

## 并行开发

- Claude Code 与 Codex 应使用各自的 branch/worktree；不要同时修改同一工作树。
- 每项功能独立提交，提交信息说明功能与测试结果。
- 合并冲突必须逐段做功能级整合，保留双方能力；禁止用 `ours`、`theirs`
  或复制整文件快速覆盖。
- 发现他人未提交改动时停止写入并等待，不得代为整理或提交。

## 生产安全

- 修改、合并或重启前检查 `/api/jobs`，以及 `aifos.adapters`、`codex exec`、
  `dreamina`、`seedance`、测试和 Git 写入进程。
- 真实生成期间不得改代码、数据库、素材或服务进程。
- 8619 服务只能在确认 PID 的 cwd 为 `/Users/sk/AIFOS`、命令包含
  `python -m aifos serve` 后优雅 TERM；禁止强杀未知进程。
- 不删除或覆盖已有生成资产；重做使用新版本并保留历史记录。

## 交付门槛

- 至少运行 `node --check aifos/web/static/app.js`、
  `python3 -m compileall -q aifos tests`、`git diff --check` 和完整测试。
- 漫剧产线遵守 `sk-manju-storyboard-skill`：先五维分镜，再关键帧，再视频；
  人物、服装、文字、段间状态及无字幕规则必须通过硬门禁。
- 新 UI/API 必须有测试；失败原因必须在用户界面可见，不能只写后台日志。
