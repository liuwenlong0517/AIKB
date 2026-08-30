# AIKB WebUI API 契约

本文档是阶段 1～4A 波次 1 的 Windows 本地 WebUI 接口契约，供后端、前端与验收共同使用。知识、运行状态和审计读取接口保持只读；阶段 3 只通过显式受控动作接口执行三项注册的本地只读检查；阶段 4A 当前只增加规则读取和候选预览。规则/知识应用写入、安装、索引重建和任意 Shell 仍不属于公共能力。

## 1. 传输与共同包络

- 基础路径为 /api/v1；服务只绑定 127.0.0.1。
- 知识、运行状态、审计和系统查询只接受 GET 和 OPTIONS。动作预览、任务创建/取消和规则候选预览是明确列出的受保护 POST；其他写方法仍返回结构化 405，不得触发未注册动作、规则/知识写入、Git 写操作或索引重建。
- 成功响应固定为 { "data": <T>, "meta": <Meta> }；失败响应固定为 { "error": <Error>, "meta": <Meta> }。
- meta.request_id 为服务生成或校验后的短 ASCII 请求标识，并通过 X-Request-ID 响应头返回；客户端提供的非法值会被忽略并重新生成。
- JSON 使用 UTF-8。时间使用带时区的 ISO-8601 字符串；分页排序必须稳定，未另行说明时按更新时间或开始时间倒序。

成功包络示例：

    {
      "data": {},
      "meta": {
        "request_id": "r-01J...",
        "api_version": "v1",
        "degraded": false,
        "warnings": []
      }
    }

degraded 和 warnings 仅在局部数据源不可用、记录损坏或字段被裁剪时出现。降级不是成功伪造：能返回的安全子集仍返回 200，整个资源无法形成可信读模型时返回 503 service_unavailable。

统一错误包络如下：

    {
      "error": {
        "code": "invalid_request",
        "message": "请求参数无效",
        "details": { "field": "page_size" }
      },
      "meta": { "request_id": "r-01J...", "api_version": "v1" }
    }

error.code 的公共枚举为：

| HTTP | code | 含义 |
|---:|---|---|
| 400 | invalid_request | 参数、逻辑标识或分页范围无效 |
| 404 | not_found | 路由或资源不存在；不区分不存在与未公开条目 |
| 405 | method_not_allowed | 非只读方法 |
| 422 | invalid_request | 框架层类型/必填校验失败时的兼容映射 |
| 503 | service_unavailable | 所需事实源或共享核心不可用 |
| 500 | internal_error | 未预期内部故障；详情只进服务端日志 |

错误 message 是稳定的中文用户提示，不得包含异常原文、绝对路径、SQL、命令、堆栈或诊断正文。details 只允许字段名、允许值、范围和安全计数等结构化信息，不放入用户输入回显。

## 2. 逻辑标识和公共字段

浏览器只接触逻辑标识：

- 稳定知识 ID：aikb: 加小写字母或数字开头、随后仅含小写字母、数字、:、- 的字符串，例如 aikb:projects:aikb-web:phase-1-read-only-mvp。
- 知识逻辑路径：以 content/ 开头的相对 POSIX 路径，例如 content/projects/aikb-web/README.md。路径段不能为空、. 或 ..。
- Working State 标识：work_id，匹配 [a-z0-9][a-z0-9-]*，最长 120 字符。
- 检查点标识：不透明的 checkpoint_id，最长 120 字符，只能作为查询值传回，客户端不得从中推导物理位置。
- 审计调用标识：不透明的 invocation_id，最长 120 字符；事件详情使用它聚合 invocation_started 与 invocation_finished。

所有标识查询都拒绝盘符、UNC/绝对路径、反斜杠路径、NUL、空路径段和目录穿越。服务不把非法输入原样放入错误响应。project_id 只能作为脱敏后的逻辑项目标识，不能接受或返回项目物理目录。

公共知识字段：id、title、type、status、path、tags、summary、last_verified、content_hash。Web 知识接口的 status 固定为枚举 verified，客户端不能提交 status 扩大读取范围。

## 3. 阶段 1 保持的接口

### GET /health

返回进程级状态，不保证所有事实源可读。

    {
      "status": "ok",
      "service": "aikb-web",
      "read_only": true
    }

status 枚举为 ok、degraded。该接口报告 Web 进程与共享核心初始化状态；知识索引、审计或运行状态等资源的具体可用性，由对应资源接口的 degraded、warnings 或结构化错误表达，健康检查不主动扫描全部事实源。

### GET /system/info

返回安全的运行摘要：

    {
      "platform": { "name": "windows", "architecture": "amd64" },
      "python": { "version": "3.x.y" },
      "repositories": {
        "control": { "available": true, "branch": "main", "short_commit": "24f4a85", "dirty": false },
        "knowledge": { "available": true, "branch": "main", "short_commit": "4bcd44a", "dirty": false }
      },
      "index": { "available": true, "tokenizer": "trigram", "rebuilt": false }
    }

仓库字段只允许 available、branch、short_commit、dirty；不可用字段使用 null 或省略。不得返回仓库路径、远端 URL、完整 Git 输出或变更文件名列表。

### GET /system/capabilities

knowledge_read_only 固定为 true；能力项使用 {id, supported, reason?}。除知识/运行状态/审计读取能力外，阶段 3 可声明 `controlled.actions` 和 `task.center`；它们只表示三项注册的 Windows 受控动作，不表示任意脚本或 Shell 能力。`knowledge.write`、`rules.write`、`shell.execute`、`git.write` 和网络访问必须为 supported: false。macOS 能力保持 `supported: false` 并给出平台未实现原因。

### GET /knowledge/overview、GET /knowledge/tree、GET /knowledge/tags

保留阶段 1 的 verified-only 语义。总览字段为 document_count、by_type、by_tag、recent_documents、directory_tree 和 index；目录接口返回以 root 包装的 directory/document 节点；标签接口返回 `{tags, status: "verified"}`。阶段 2 不改变这些既有响应结构。

### GET /knowledge/search

查询参数：q（或兼容的 query，1..200 字符）、type（最多 64）、tags（逗号分隔，总长度最多 500）、limit（默认 20，范围 1..20）、excerpt_chars（默认 700，范围 120..1600）。服务始终只搜索 verified；阶段 2 暂不改变阶段 1 的非分页搜索契约。

    {
      "query": "中文",
      "status": "verified",
      "count": 0,
      "results": []
    }

results 每项只能包含公开知识元数据、命中章节和限长 excerpt；不得返回正文全量、物理路径或索引 SQL。无命中是 200 加空数组，不是 404。索引不可用且无法可信搜索时返回 503，不要以空结果冒充“无命中”。

### GET /knowledge/document

查询参数：id_or_path（必填，逻辑 ID 或 content/...，最长 500）、可选 section（最长 200）、max_chars（默认 500000，范围 300..500000）。返回上述公共字段加 content、truncated、applicable_versions、relations。正文保持 Markdown 块和换行；不启用原始 HTML。不存在、非 verified 或非法标识统一为 404 not_found 或 400 invalid_request，不泄漏存在性差异。

## 4. 阶段 2 运行状态接口

运行状态是 workspace/active 与其可重建索引的只读投影，不是知识内容。默认只列活动状态：planned、active、blocked。已关闭的 completed、abandoned、superseded 不混入活动列表；如需历史必须由明确的 include_closed=true 契约另行实现，本阶段不开放。

### GET /runtime/working-states

查询参数：project_id（逻辑 ID，最长 120）、status（可重复或逗号分隔，枚举见上）、agent（最长 120）、page（1..100000，默认 1）、page_size（1..50，默认 20）。列表默认按 updated_at 最新时间倒序，排序相同的记录按 work_id 稳定打散。

返回 WorkingStateSummary 分页：

    {
      "items": [{
        "work_id": "webui-phase-2",
        "project_id": "aikb-...",
        "status": "active",
        "agent": "codex",
        "session_id": null,
        "role": "implement",
        "updated_at": "2026-08-29T10:00:00+08:00",
        "checkpoint_id": "...",
        "goal": "实现只读运行观察面",
        "current_state": "契约已固定",
        "next_steps": "实现读模型",
        "blockers": "",
        "branch": "main",
        "base_revision": "24f4a85",
        "workspace_dirty": false,
        "repositories": [{ "role": "control", "branch": "main", "revision": "24f4a85", "dirty": false }]
      }],
      "pagination": { "page": 1, "page_size": 20, "total": 1, "has_next": false }
    }

session_id 没有可靠来源时必须为 null，不得以 Agent、连接 ID 或时间戳冒充。repositories 每项最多 8 项，只允许 role、available、branch、revision、dirty；去除底层 path 和内部 signature。正文型章节在共享读模型中限长。第一小版本只查询活动 Working State，不提供归档或关闭任务搜索。

### GET /runtime/working-states/{work_id}

返回单个 WorkingStateDetail：列表字段加 `sections`、`detail_status`、`sensitivity`、`checkpoint_count`、`latest_checkpoint` 和最长 1500 字符的 `resume_capsule`。这些字段只使用共享核心白名单中的紧凑恢复章节，并继续执行脱敏和长度限制；不得包含聊天全文、隐藏推理、完整 diff、密钥、原始日志或物理路径。不存在或不是活动状态统一 404 not_found。

### GET /runtime/working-states/{work_id}/checkpoints

查询参数：page（1..100000，默认 1）、page_size（1..50，默认 20）。返回检查点摘要 `{checkpoint_id, based_on, status, agent, session_id, role, updated_at, workspace_dirty, repositories, truncated, detail_status}` 及分页信息。session_id 缺失仍为 null，repositories 继续使用安全公共投影。

### GET /runtime/working-states/{work_id}/checkpoints/{checkpoint_id}

返回有限检查点详情：摘要字段加白名单 `sections`，并把 goal、current_state、next_steps、blockers、verification、changed_files 作为同值扁平字段方便页面使用；每个标量或列表项最多 4000 字符，列表最多 50 项，物理路径统一替换为安全占位符。不返回磁盘位置、完整 Markdown、完整 diff、聊天记录或恢复时的原始输入。

## 5. 阶段 2 审计接口

审计是 workspace/audit/events/**/*.jsonl 及 fallback 记录的可重建只读投影，独立于知识和 Working State。一次 MCP/hook 调用按 invocation_id 聚合开始/结束事件；缺少结束事件的逻辑调用状态为 incomplete。

### GET /audit/summary

查询参数：since（<正整数>h 或 <正整数>d）、date（YYYY-MM-DD，不能与 since 同时使用）、agent（最长 120）、source（mcp 或 hook）。返回：

    {
      "count": 12,
      "statuses": { "succeeded": 8, "failed": 1, "noop": 2, "blocked": 1, "incomplete": 0 },
      "agents": { "codex": 12 },
      "sources": { "mcp": 10, "hook": 2 },
      "operations": { "search_knowledge": 4 },
      "average_duration_ms": 128.5,
      "fallback_records": 0,
      "damaged_count": 0,
      "last_activity": "2026-08-29T10:00:00+08:00"
    }

status 枚举为 started、succeeded、failed、noop、blocked、incomplete；summary 允许计数为 0。损坏记录只公开数量，不公开文件名、行号或物理路径。无任何记录是 200 加全零摘要。

### GET /audit/events

查询参数：since、date、agent、source 同上，另有 status（可重复，枚举同上）、operation（最长 120）、page（1..10000，默认 1）、page_size（1..100，默认 50）。返回 items 和分页，默认按 started_at 最新时间倒序，排序相同的记录按 invocation_id 稳定打散。列表项字段为：

invocation_id、event_id、started_at、finished_at、source、agent、session_label、session_id、project_id、operation、action_text、status、outcome_code、result_text、capture_level、duration_ms、error_type、fallback。

session_label 是优先展示的人类标签；session_label、session_id 缺失时均为 null，不能用其他技术字段推断。capture_level 枚举为 safe、diagnostic、full-local。列表不返回 client、connection_id、action、result_summary 或任何诊断附件内容。

### GET /audit/events/{invocation_id}

返回单次调用的有限详情，字段仍严格使用上述 Web 安全投影；详情接口不会扩大到 client、action、result_summary、诊断附件存在性或原始事件内容。可使用 event_id、结束事件 ID 或 invocation_id 定位同一聚合调用，不存在时返回 404。

审计读取失败时，若仍能得到完整安全摘要，返回 200 并设置 meta.degraded=true、警告 audit_partial；JSONL 全部不可读时返回 503 service_unavailable。单条损坏记录不应使其他记录消失。

## 6. 阶段 3 受控动作与任务接口

阶段 3 当前只注册以下三个 Windows 本地只读动作：`validate.structure`（结构校验）、`repository.status.control`（控制仓状态）和 `repository.status.knowledge`（知识仓状态）。动作注册表由版本控制下的服务端代码静态构造，前端只能提交动作 ID 和严格校验的参数对象；当前三个动作参数必须为空对象。动作列表的 `supported` 只在当前平台、执行器和前置文件均可用时为 true，macOS 始终为 false。

所有变更类请求还必须满足 `Content-Type: application/json`、`X-AIKB-Request: 1`、本机同源 Host/Origin 校验和服务端签发的确认令牌。预览令牌绑定动作、规范化参数、风险和 `preview_digest`，有效期 5 分钟、只能消费一次，重启后失效。

### GET /actions

返回动作能力、安全参数 Schema、风险等级、读取影响范围、超时和并发组。不返回命令数组、脚本路径、工作目录、环境变量或进程信息。

### POST /actions/{action_id}/preview

请求体为 `{ "parameters": {} }`。服务端只校验和规范化参数，不执行动作；返回语义步骤、风险、副作用、超时、`preview_digest`、确认要求、令牌和令牌剩余有效秒数。未知动作、非空未允许参数或平台/前置条件不可用时返回稳定错误。

### POST /tasks

请求体为 `action_id`、规范化 `parameters`、`preview_digest` 和 `confirmation_token`。成功返回服务端生成的 `task_id` 和任务安全投影，并异步进入 `queued`。客户端不能指定任务 ID、命令、路径、环境或进程参数。

### GET /tasks、GET /tasks/{task_id}

返回任务列表或单项详情。公共字段包括任务 ID、动作 ID、空参数摘要、风险、影响范围、状态、时间、进度、限长 UTF-8 输出、输出裁剪标记、安全结构化结果和关联审计 ID。任务事实源为 `workspace/runtime/web/tasks/<YYYY>/<MM>/<task_id>/events.jsonl`，`snapshot.json` 是可重建投影；响应不公开物理目录、命令行、PID、句柄、环境、令牌或原始异常。

状态为 `queued`、`running`、`cancelling`、`succeeded`、`failed`、`cancelled`、`timed_out`、`interrupted`；终态不可逆，服务重启时遗留非终态任务收敛为 `interrupted`。

### POST /tasks/{task_id}/cancel

幂等请求取消。排队任务直接进入 `cancelled`，运行中任务先进入 `cancelling`，Windows Job Object 收敛后再进入终态；终态重复取消只返回当前安全投影，不重复制造目标任务的结束审计。

### GET /tasks/{task_id}/events

返回 `text/event-stream`。允许事件类型为 `snapshot`、`status`、`progress`、`output`、`result`、`heartbeat`；事件 ID 在任务内单调递增。客户端通过 `Last-Event-ID` 回放，游标失效时收到带 `replay_reset=true` 的最新安全快照；心跳最长 15 秒，终态结果发出后关闭。输出受单块 8 KiB、单行 4 KiB、单任务 2 MiB 预算约束，超限只公开 `truncated` 标志并保留最终安全结果。

## 7. 阶段 4A 规则读取与候选预览

规则注册表固定为 `entry`、`user`、`agent`、`contributing`；四项均可读取，只有 `user` 的 `writable=true`。浏览器只使用逻辑 ID，不接收或提交物理路径。

### GET /rules、GET /rules/{rule_id}

目录返回标题、说明、读写能力、风险、字符预算、内容哈希和 Git revision；详情另返回当前正文。公共投影不包含 `relative_path`、控制仓根目录或文件异常。未知或路径型 ID 返回安全 404。

### POST /rules/{rule_id}/preview

只允许 `user`。请求体严格为 `base_content_hash` 和 `candidate_content`，并要求 JSON、`X-AIKB-Request: 1` 与本机同源 Host/Origin。服务在预览前后检查普通分支、无 merge/rebase 等操作、全仓洁净、revision 和当前正文哈希；候选走共享规则验证器。

成功返回完整 unified diff、校验摘要、`change_id`、前后/diff 哈希、`preview_digest`、五分钟进程内确认令牌和过期时间。候选最多 2,000 行，diff 最多 4,000 行或 256 KiB，超限必须拒绝而不是截断。草稿只在 `workspace/runtime/web/rule-changes/` 保存候选和 `prepared` 事务摘要，不创建备份，不写任务或审计。当前没有 `/rules/{rule_id}/apply` 路由，令牌没有 HTTP 消费入口。

## 8. 分页、空集和降级统一规则

- page 从 1 开始；阶段 2 新增的运行状态 page_size 上限为 50，审计上限为 100。超限返回 400 invalid_request，不得静默扩大或无限制读取；阶段 1 搜索继续使用 `limit<=20`。
- total 是当前筛选下可信计数；无法计算时返回 null，并在 meta.degraded=true，不得用当前页长度伪造总数。
- 合法查询但无结果统一返回 200、空 items/results、has_next=false。空 Working State、无检查点、无审计记录、无搜索命中都必须提供可解释的空集。
- 可重建索引不可用时，能从 Markdown/JSONL 事实源安全读取的接口可以局部降级；无法保证结果完整性的搜索、分页或详情接口必须 503。
- 所有降级都要给机器可读的 warnings 枚举（如 index_unavailable、audit_partial、damaged_records、session_id_unavailable），不把底层异常文本放入响应。

## 9. 明确禁止公开的内容

任何接口、错误包络、日志转发或浏览器源代码都不得公开：Windows 绝对路径、盘符、UNC 路径、workspace/数据库物理路径、完整 Git 输出、SQL、环境变量值、密钥/token/cookie、完整请求 payload、完整 MCP 返回值、知识正文（搜索/审计场景）、诊断正文、聊天全文、transcript、隐藏推理、二进制附件和完整 traceback。需要定位问题时只返回 request_id，由本机服务日志关联。
