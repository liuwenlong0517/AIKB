---
id: aikb:projects:local-code-rag:index-integrity-observability-registration
type: project-memory
status: verified
tags: [local-code-rag, qdrant, fts5, sqlite, recovery, observability, docker, registry]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 修改 Qdrant collection 结构、manifest/FTS5 schema、索引事务顺序、任务队列或项目挂载方式时
supersedes: []
relations:
  - type: related_to
    target: aikb:projects:local-code-rag:p1-safe-context-watcher-retrieval
---

# 索引一致性恢复、任务可观测性与自动项目注册

## 背景

本地代码索引由 Qdrant 向量、SQLite manifest 和 SQLite FTS5 三层组成。首次大型索引若在向量提交和词法提交之间中断，三层可能脱节；多项目登记此前也需要手工同步 TOML 注册表、Compose 只读挂载和容器 recreate。

## 问题

仅检查容器存活不足以证明索引完整，且 Agent 无法仅凭任务最终状态判断是否发生恢复。手写多项目挂载容易遗漏 `read_only`、容器路径或 recreate 步骤。Windows 上使用 POSIX 风格 `os.kill(pid, 0)` 探测锁拥有者也不安全。

## 解决方案

提交 `412e176` 增加按 `repo_id` 的三层计数检查：Qdrant points、manifest chunks/文件和 FTS5 chunks/文件。真实异步索引任务开始前自动检查；仅词法层不一致时只重建 FTS5，向量不一致时先删除该项目的 Qdrant points，再按现有扫描、AST 切片和敏感过滤规则重建向量与词法层。

任务 SQLite 库新增阶段事件与指标。API 提供 `/index-tasks/{task_id}/events`、`/index-integrity` 和 `/metrics`；stdio MCP 提供对应的 `get_index_task_events`、`check_index_integrity`、`get_code_rag_metrics`。事件只记录阶段、枚举和统计，不保存代码、绝对路径或密钥。

`.lock` 异常恢复只在确认拥有者已死亡时执行：Linux 容器同时比较 PID 与 `/proc` 启动 tick；Windows 使用 `OpenProcess/GetExitCodeProcess`，不使用有副作用的 `os.kill(pid, 0)`。

宿主机 `register-project` CLI 默认只预览；显式 `--apply` 后原子更新 Git 忽略的 `data/repositories.toml` 和 `compose.registered-projects.yaml`，生成 `/repositories/<repo_id>` 的只读 bind mount，并只 recreate `local-code-rag`。它不构建镜像、不重启 Ollama/Qdrant，容器 API/MCP 仍不暴露宿主机绝对路径。

## 验证

2026-08-07 完整单元测试为 49/49 通过，覆盖死锁拥有者回收、Windows 锁探测分支、三层计数、Qdrant repo filter、最小恢复选择、任务事件、指标和注册幂等性。重建 Docker API 后，`ordertools` 的 manifest、FTS5、Qdrant 计数均为 4,031；真实无变更异步任务依次记录 `queued`、`running`、`integrity_check`、`integrity_result`、`succeeded`。

## 适用范围

适用于 Windows + Docker Desktop 上的单个 `local-code-rag` API 容器和受 Git 忽略的本地 `data/`。恢复依赖计数检测，不替代备份；队列仍是单工作线程，不提供取消、百分比进度或分布式执行。自动注册只新增项目，不自动删除旧 collection、旧注册条目或业务目录。

## 关联信息

- `E:\CodeSpace\local-code-rag\src\local_code_rag\integrity.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\registration.py`
- `E:\CodeSpace\local-code-rag\docs\local-code-rag-manual.md`
- [异步索引任务队列与重启状态边界](async-index-task-queue.md)
