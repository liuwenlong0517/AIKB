# AIKB WebUI 阶段 3 实施前置契约

本文档冻结“受控校验与任务中心”的任务分发前置条件。2026-08-29 首批三项只读动作、任务 API/SSE、Windows 执行器和任务中心已按本契约实现并通过真实 Windows 验收；未准入动作仍按本文阻塞条件管理。

## 1. 第一小版本边界

阶段 3 第一小版本只允许执行已注册的本机校验和派生报告动作。客户端提交语义化 `action_id` 与受 Schema 约束的参数，不能提交命令、脚本路径、工作目录、环境变量、stdin、文件或参数数组。

继续固定以下边界：

- 只监听 `127.0.0.1`，不增加登录、多用户、局域网或公网模式；
- 不修改正式知识、规则、Agent 配置或 Git；
- 不实现安装、卸载、修复、清理、索引重建和任意 Shell；
- macOS 只保留接口与目录位置，执行能力保持 `supported: false`；
- 任务运行数据只进入 `workspace/runtime/web/tasks/`，审计继续以 `workspace/audit/events/` JSONL 为事实源。

## 2. 动作准入矩阵

动作注册表必须由受版本控制的 Python 代码静态构造，不能从用户可修改的 YAML/JSON 加载可执行程序。进程动作只能使用受信任执行器键 `pwsh`、`python`、`git`，并以参数数组调用；禁止 `shell=True`、拼接命令行和 PowerShell `Invoke-Expression`。

| action_id | 真实入口 | 参数 | 副作用与风险 | 首批状态 |
|---|---|---|---|---|
| `validate.structure` | `pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File system/tests/validate-structure.ps1 -KnowledgePath <configured>` | 无 | 读取双仓；不写事实源 | 已实现，`read_only` |
| `repository.status.control` | `git` 固定参数读取控制仓 branch/revision/status | 无 | 只读控制仓 | 已实现，`read_only` |
| `repository.status.knowledge` | `git` 固定参数读取知识仓 branch/revision/status | 无 | 只读知识仓 | 已实现，`read_only` |
| `index.inspect` | 新增共享核心只读检查函数 | 无 | 只读 `workspace/db` 与知识指纹；不得重建 | 当前阻塞，完成专用入口后准入 |
| `config.doctor` | 新增 `doctor` 观察模式或共享诊断函数 | `agents`：`codex`、`claude-code` 的有限集合 | 现有 `doctor.ps1` 会执行真实 MCP/hook probe 并写审计，不能直接注册 | 当前阻塞，隔离 probe 后准入 |
| `audit.report.generate` | `python -m aikb audit report --date <ISO date>`；输出位置由服务固定 | `date`：合法 ISO 日期，默认本机当天 | 原子写入或覆盖派生 Excel 报告 | 条件准入，`derived_write`，必须确认 |

`rebuild`、`setup-aikb.ps1`、安装/卸载、`clear-workspace.ps1 -Apply`、规则写入和任意自定义路径全部排除。`validate-aikb-web.ps1` 会更新前端构建产物和缓存，暂不作为 Web 动作。

### 固定运行预算

| 动作组 | 超时 | 并发组上限 |
|---|---:|---:|
| 仓库状态 | 15 秒 | 2 |
| 结构校验 | 120 秒 | 1 |
| 索引检查 | 30 秒 | 1 |
| 配置诊断 | 90 秒 | 1 |
| 审计报告 | 120 秒 | 1 |

服务全局最多同时运行 2 个任务；同一动作和同一并发组的互斥由注册表声明。超时值、工作目录、程序位置与环境白名单不能由前端覆盖。

## 3. 注册表与参数契约

每个 `ActionSpec` 至少固定：

- `action_id`、标题、说明、支持平台和能力状态；
- `risk_level`：`read_only` 或 `derived_write`；
- `effects`：读取范围和唯一允许写入的逻辑范围；
- JSON 参数 Schema；未知字段使用 `additionalProperties: false` 拒绝；
- `executor_kind`、受信任程序键、固定工作目录和参数构造器；
- 超时、并发组、输出解析器和安全结果投影；
- `confirmation_required` 与不可绕过的服务端确认策略。

动作预览返回规范化参数、语义步骤、风险、副作用、超时和 `preview_digest`。执行请求必须携带服务端签发的短期确认令牌；令牌绑定 `action_id`、规范化参数、风险和预览摘要，有效期固定 5 分钟且只能成功消费一次，服务重启后失效。令牌密钥只存在于当前 Web 进程内存，不写入仓库、workspace 或前端。即使 `read_only` 动作不显示二次确认，也必须经过预览/校验，不能直接传入命令。

## 4. 任务事实源与状态机

任务目录固定为：

```text
workspace/runtime/web/tasks/<YYYY>/<MM>/<task_id>/
├─ events.jsonl       # 追加式任务事实源
└─ snapshot.json      # 可原子替换、可由 events 重建的当前投影
```

不把 SQLite、进程内对象或浏览器状态作为任务事实源。`task_id` 由服务生成，客户端不能指定。公共 API 只返回任务 ID、动作 ID、安全参数摘要、状态、时间、进度、有限输出、结果摘要和关联审计 ID，不返回物理目录、完整命令行、环境、PID、Job Handle 或原始异常。

状态机固定为：

```text
queued -> running -> succeeded | failed | timed_out
queued -> cancelled
running -> cancelling -> cancelled
queued | running | cancelling --服务异常恢复--> interrupted
```

终态不可逆。取消是幂等操作；终态任务再次取消返回当前状态，不重复写审计。服务启动时把非终态历史任务标为 `interrupted`，不根据旧 PID 猜测或重新附着进程。

每个输出事件单块最多 8 KiB，单任务持久化输出总预算 2 MiB，单行最多 4 KiB；超限时写一次 `output_truncated` 事件并继续保存最终结构化结果。输出统一 UTF-8 解码，出现 U+FFFD 视为验收失败。保存前执行密钥、认证头、物理路径和控制字符脱敏。

任务目录沿用 `RuntimeRetentionDays=30` 的清理策略；阶段 3 必须先扩展清理测试，保证运行中任务和最新投影不被删除。

## 5. Windows 执行与取消

Windows 正式执行器位于 `backend/aikb_web/platform/windows/`。每个外部任务必须创建 Windows Job Object，启用 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，并在子进程恢复运行前完成关联。无法创建或关联 Job Object 时拒绝启动任务，不能退化成只终止父进程。

执行器不能原样继承 Web 服务环境。子进程环境从空字典构造，只加入受注册表允许的 `AIKB_HOME`、`AIKB_KNOWLEDGE_HOME`、`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`、`NO_COLOR=1`，以及 Windows/Python/Git 正常启动所需的最小 `SystemRoot`、`TEMP`、`TMP`、`USERPROFILE`。程序均使用服务端解析后的绝对路径；不得继承 token、代理、Cookie、Agent 配置变量或捕获级别。动作如需新增环境项，必须更新注册表、文档和泄漏测试。

取消与超时统一执行：

1. 原子写入 `cancelling` 或 `timed_out` 原因；
2. 调用 `TerminateJobObject` 收敛完整进程树；
3. 在有限等待后关闭句柄并记录结束事实；
4. 根据取消、超时或执行失败生成不同终态，不能只看退出码。

Windows 测试必须包含父进程、子进程和孙进程，验证正常完成、用户取消、超时、并发取消和服务关闭后均无残留。macOS 执行器不在本阶段伪造实现。

## 6. REST 与 SSE 契约

阶段 3 已新增：

- `GET /api/v1/actions`：能力与 Schema；
- `POST /api/v1/actions/{action_id}/preview`：参数校验、风险和确认令牌；
- `POST /api/v1/tasks`：以确认令牌创建任务；
- `GET /api/v1/tasks`、`GET /api/v1/tasks/{task_id}`：列表与详情；
- `POST /api/v1/tasks/{task_id}/cancel`：幂等取消；
- `GET /api/v1/tasks/{task_id}/events`：SSE 事件流。

SSE 的 `id` 是任务内单调递增整数，类型只允许 `snapshot`、`status`、`progress`、`output`、`result`、`heartbeat`。客户端使用 `Last-Event-ID` 恢复；无法完整回放时先发带 `replay_reset=true` 的最新 `snapshot`。心跳间隔 15 秒，终态结果发出后关闭连接。

变更类 HTTP 必须同时满足：

- 仅接受 `application/json`；
- 校验同源 `Origin`/`Host`，拒绝 DNS rebinding 与跨站请求；
- 不开放通配 CORS，不允许浏览器提交自定义可执行字段；
- 使用 `X-AIKB-Request: 1` 和短期确认令牌；
- 请求体、错误响应和服务日志不回显秘密、物理路径或底层命令。

开发端口 `5173` 只在显式开发模式加入精确 Origin 白名单；生产只允许当前 `127.0.0.1:<port>` 或 `localhost:<port>` 同源请求。

## 7. 审计关联

每个任务创建后分配唯一 `invocation_id`，任务事件保存 `task_id`、`action_id` 和 invocation 关联。阶段 3 使用审计 schema v3：读端继续兼容 v1/v2，v3 将 `source` 扩展为 `web`，增加有限的 `task_id`、`action_id`、`target_task_id` 字段，并把 `cancelled`、`timed_out`、`interrupted` 加入审计状态枚举。Web 安全投影、筛选、汇总和测试必须同步扩展；未知新字段仍默认丢弃。

- 接受执行时写 `invocation_started`；
- 终态写对应完成记录，状态区分 succeeded、failed、blocked、cancelled、timed_out、interrupted；`outcome_code` 保留更细的机器原因；
- 取消请求使用独立审计 invocation，并以 `target_task_id` 关联；
- 审计只保存动作、风险、终态、耗时和安全摘要，不保存任务输出、完整参数、命令行、PID 或原始异常；
- fallback 审计仍可用，并由任务详情显示有限降级警告。

## 8. 任务分发门槛

只有以下设计条件全部写入测试和接口契约后，才能开始功能实现波次：

1. 静态动作注册表和 `additionalProperties: false` 参数模型；
2. 同源保护、确认令牌和只接受 JSON 的变更请求边界；
3. JSONL 任务事实源、原子 snapshot、状态转换表和崩溃恢复语义；
4. SSE 事件 ID、回放、心跳、终态关闭和输出预算；
5. Windows Job Object 强制关联与三代进程树回归方案；
6. 审计 v3 的 `web` 来源、任务关联字段、终态枚举和 v1/v2 兼容；
7. `index.inspect` 的无重建只读入口和 `config.doctor` 的无 probe 观察入口在各自准入前完成；
8. 所有测试使用临时 workspace，不执行真实安装、清理、Git 写入或正式知识修改。

本契约确认后，开发任务可按“契约与事实源 → Windows 执行器与任务服务 → API/SSE 与前端任务中心 → 真实进程边界验收”拆分。
