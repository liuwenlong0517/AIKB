---
status: verified
tags: [local-code-rag, indexing, async-task, sqlite, mcp, docker]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 修改索引并发模型、SQLite 状态库位置、HTTP/MCP 索引接口或引入外部任务队列时
supersedes: []
---

# 异步索引任务队列与重启状态边界

## 背景

`E:\CodeSpace\local-code-rag` 的首次全量索引可能持续数十分钟。同步 `POST /index` 将 HTTP 连接生命周期与 Qdrant、SQLite manifest、FTS5 的长时间写入绑定；客户端断开时无法可靠观察最终结果。

## 问题

Agent 和 MCP 需要在不阻塞 stdio 或 HTTP 调用的前提下启动索引，并能查询任务是否排队、运行、成功或失败。运行状态不能只保存在内存，否则容器重启后会把未完成任务误报为成功或直接丢失。

## 解决方案

从提交 `5257f73` 开始，`POST /index` 仅创建任务并返回 HTTP 202 与随机 `task_id`；`GET /index-tasks/{task_id}` 查询单个状态，`GET /index-tasks` 查询最近任务。stdio MCP 对应提供 `index_project`、`get_index_task` 与 `list_index_tasks`。

任务状态保存到与 manifest 分离的 `*-tasks.db`，只记录项目标识、请求范围、时间、状态、无源码的索引统计报告和安全错误摘要；不保存绝对路径、代码正文或密钥。服务使用单个工作线程，主动与现有全局索引锁保持一致，避免多个项目同时写同一个 manifest/FTS5 状态库。

服务启动时会将上一次容器实例遗留的 `queued` 或 `running` 任务标记为 `failed`，错误类型为 `ServiceRestarted`。这确保任务不会被静默误认为完成。

## 验证

2026-08-07 运行 42 个单元测试，覆盖任务报告持久化、Windows SQLite 连接关闭、重启中断状态、API 返回 202、失败信息脱敏和 MCP 转发。重建 Docker 服务后，对已登记的 `ordertools` 实际提交了 dry-run 与无变更真实增量任务：均返回 202、状态转为 `succeeded`，真实任务报告为 0 个变化文件、0 个新增片段与 0 个删除向量点。

## 适用范围

适用于 Windows + Docker Desktop 上该项目的单个 `local-code-rag` API 容器。任务队列不是分布式调度器，不提供取消、细粒度百分比进度或多工作线程并行；任务数据库保存在受 Git 忽略的 `data/`。

容器在真实写入中异常退出时，原有 `.lock` 文件可能仍需人工确认后处理；Qdrant、manifest 与 FTS5 的自动一致性修复属于后续 P0 工作，不能因任务状态已标记失败而假设数据已自动回滚。

## 关联信息

- `E:\CodeSpace\local-code-rag\src\local_code_rag\index_tasks.py`
- `E:\CodeSpace\local-code-rag\docs\mcp-stdio.md`
- `E:\CodeSpace\local-code-rag\docs\local-code-rag-manual.md`
