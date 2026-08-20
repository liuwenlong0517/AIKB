---
id: aikb:projects:local-code-rag:p1-safe-context-watcher-retrieval
type: project-memory
status: verified
tags: [local-code-rag, mcp, fts5, watcher, retrieval-evaluation, docker, windows]
applicable_versions: local-code-rag 0.3.0 and later
last_verified: 2026-08-07
review_when: 修改安全过滤、FTS5 schema、MCP/API 工具、监听器、宿主机端口或默认检索限额时
supersedes: []
relations:
  - type: related_to
    target: aikb:projects:local-code-rag:retrieval-cache-context-mcp
---

# P1：安全上下文、容器监听与多项目检索基准

## 已验证事实

`read_code_context` / `POST /read-context` 只从 SQLite FTS5 中已通过敏感过滤的快照读取指定相对路径和行范围，不直接打开业务仓库文件。它拒绝绝对路径和 `..`，默认最多 240 行，因此 MCP 不能借上下文工具绕过受控索引边界。

索引任务状态新增 `progress_phase` 和 `progress_percent`。历史任务 SQLite 库在启动时会自动补列，不要求删除状态库或重建索引。只有 `queued` 任务允许取消；`running` 任务被拒绝强制终止，避免中断 SQLite/Qdrant 双层写入导致不一致。

`start_project_watcher` 在 API 容器内为已登记、只读挂载的项目按需启动监听。它以 0.5～30 秒防抖合并文件事件，只向现有单工作线程任务队列提交 `watch` 增量任务，不直接写入业务项目。监听状态不跨容器重启恢复，避免用户无感知地持续消耗 GPU；`stop_project_watcher` 只停止新事件采集，不中断已提交任务。

面向 Agent 的默认检索改为每文件一个定位片段（`max_per_file=1`），避免同一类的相邻切片占满候选；需要更多内容时先用安全上下文工具按行扩展。`ordertools` 新增 15 条经源码符号和目标路径核验的中文评测：4B embedding + FTS5 + RRF 下 Top-10 为 15/15、MRR 0.552、平均首命中排名 2.80。CRM 复测为 20/20、MRR 0.671、平均首命中排名 1.95。由于两个评测集均无 Top-10 漏召回，本阶段不引入额外 reranker。

Windows 在本次验证时系统保留 TCP `8769–8868`，导致 8787 无进程占用仍不能由 Docker 绑定。宿主机 API 已迁移为回环 `127.0.0.1:8870`，容器内部仍用 8787。`scripts/compose-local.ps1` 会自动合并存在的 Git 忽略挂载覆盖文件 `compose.projects.yaml` 与 `compose.registered-projects.yaml`，防止手工重启遗漏已登记项目。

## 验证证据

2026-08-07：完整单元测试 57/57 通过。Docker 镜像 `local-code-rag:0.3.0` 已重建，API 容器在 `127.0.0.1:8870` healthy；`/readyz` 为 `ready`，`crm-order-app` 与 `ordertools` 均为 available。真实在线调用完成 `ordertools` 的 `search → read-context`，上下文来源返回 `indexed_safe_snapshot`；监听器成功启动、列出并停止；dry-run 异步任务返回 `succeeded / completed / 100%`，且变更文件数为 0。

## 操作边界

- Agent 应先 `search_code`，再根据命中调用 `read_code_context`；命中仍需在业务仓库中最终核对。
- 要持续监听时显式启动；服务重启后需再次显式启动。
- 日常 Compose 操作使用 `E:\CodeSpace\local-code-rag\scripts\compose-local.ps1`，不要在已有额外挂载时裸用 `docker compose up`。
- 若未来考虑 reranker，先扩充真实项目评测并证明确有漏召回或关键目标的稳定靠后问题，再比较收益、显存和延迟。

## 关联文件

- `E:\CodeSpace\local-code-rag\src\local_code_rag\context.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\watch_manager.py`
- `E:\CodeSpace\local-code-rag\src\local_code_rag\index_tasks.py`
- `E:\CodeSpace\local-code-rag\eval\ordertools-retrieval.json`
- `E:\CodeSpace\local-code-rag\docs\local-code-rag-manual.md`
