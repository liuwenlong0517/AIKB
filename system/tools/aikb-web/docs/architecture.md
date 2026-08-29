# AIKB WebUI 阶段 2 架构与读模型

本文档定义 Windows 本地只读运行观察面。阶段 2 已按本契约实现运行状态、检查点和审计路由；macOS 仍只保留平台扩展位置，尚未实现或验证。

## 数据流与边界

    React 页面
      → /api/v1 HTTP JSON
      → FastAPI 只读路由与统一错误边界
      → Web 读模型适配器
      → aikb-mcp/aikb 共享核心
      → Markdown / workspace Working State / workspace/audit JSONL / SQLite 派生索引

浏览器永远不能直接访问文件系统、SQLite、Git、PowerShell 或 workspace。后端按事实源职责分流：

- 知识读模型读取知识仓 Markdown 及共享核心查询；workspace/db/ 只作可重建索引。
- Working State 读模型读取 workspace/active 的 work.md、检查点及其索引；不把工作状态提升为正式知识。
- 审计读模型读取 workspace/audit/events/、fallback JSON，并按 invocation_id 聚合；报告和索引只是派生物。
- 系统读模型只输出平台、Python、双仓 Git 短摘要和索引可用性；Git 命令必须固定、超时、白名单字段。

运行状态第一小版本只读活动项；归档项和关闭项不进入查询范围。列表默认按最新更新时间倒序，审计调用默认按最新开始时间倒序，并以逻辑 ID 做稳定排序兜底。

## 读模型分层

### 适配层

KnowledgeGateway、WorkingStateReader、AuditReader 负责适配共享核心/本机事实源，屏蔽物理路径和底层异常。适配层应在返回 Web 之前完成：状态过滤、字段白名单、长度限制、脱敏、损坏记录计数和逻辑标识转换。

### API 层

API 层只负责 HTTP 参数、分页、状态码和统一包络，不复制 SQLite SQL、Markdown 解析、审计配对或 Git 业务逻辑。任何适配器故障映射为稳定错误码；异常原文只留在服务端受控日志，并用 request_id 关联。

### 前端层

API Client 只接受 {data, meta} 或 {error, meta}。页面通过状态钩子分别表达 loading、空集、局部降级、完全不可用和成功；不能把 503 当成空列表，也不能根据缺失字段猜测状态。

## 路由与资源关系

    /health
    /system/info
    /system/capabilities
    /knowledge/*
    /runtime/working-states
      ├─ /{work_id}
      └─ /{work_id}/checkpoints
           └─ /{checkpoint_id}
    /audit/summary
    /audit/events
      └─ /{invocation_id}

所有运行与审计路由只读。阶段 2 不注册创建、更新、关闭、删除、重建、执行或下载原始附件路由；未知 /api/* 必须 JSON 404，不能被 SPA 回退吞掉。

## 可信度和降级

每个读模型必须区分：

1. available：事实源可读且字段完整；
2. degraded：部分记录损坏、索引缺失或 session 信息缺失，但安全子集仍可信；
3. unavailable：无法形成可信结果，返回 503 service_unavailable。

session_id 和 session_label 都是可缺失的事实字段；缺失时保留 null，不能通过 Agent、connection_id、时间戳或其他标签推断。

局部降级通过 meta.degraded=true 和固定 warnings 传达。空集只表示合法查询没有记录，不代表事实源不可用。审计记录的 fallback 和 damaged 是可见的状态计数，不是原始文件路径。

## 双仓和本机路径隔离

控制仓与知识仓独立显示为 repositories.control 和 repositories.knowledge。仅输出 available、分支、短提交、脏状态；不输出 repo_root、knowledge_root、远端、变更文件或命令输出。Working State 中的 repository snapshot 在 Web 读模型中去除 path 和内部 signature，只保留 role、available、branch、revision、dirty。

## 未来扩展点

动作执行、任务中心、SSE、取消、规则修改和知识写入必须新建显式能力契约，不能复用本阶段 GET 读模型暗中扩权。macOS 仍只保留 platform/base.py 与扩展目录；React/FastAPI 可移植不等于 macOS Agent hook、权限、路径大小写和 UTF-8 已验证。
