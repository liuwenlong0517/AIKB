# AIKB WebUI

AIKB WebUI 是 AIKB 的本地管理终端。除正式知识、运行状态、审计、受控任务、规则治理和安装修复外，当前还提供独立的数据维护页：用户可按固定类别和保留期盘点审计数据、终态归档任务及终态 Web 任务，查看保护计数后通过短期预览和二次确认执行清理。服务不开放局域网监听。

## 开发边界

- Markdown/Git 是知识事实源，SQLite/FTS 是 `workspace/db/` 下的可重建派生层；
- Web 后端复用 `../aikb-mcp/aikb/`，不复制知识扫描和查询逻辑；
- 前端只调用 `/api/v1`；
- 所有知识接口固定过滤 `status=verified`；
- 运行状态分别查询活动 Working State（`planned`、`active`、`blocked`）与历史归档（`completed`、`abandoned`、`superseded`），两类接口保持独立且均为只读；审计只提供脱敏摘要、筛选、分页和调用详情；
- 阶段 3 当前只注册 `validate.structure`、`repository.status.control`、`repository.status.knowledge` 三项 Windows 本地只读动作；通过服务端预览、严格空参数 Schema、单次确认令牌、JSONL 任务事实源、SSE 和受控 Job Object 执行；
- 任务输出、结果和审计均为安全投影，任务事实源位于 `workspace/runtime/web/tasks/`，知识、规则和 Git 事实源不被动作修改；
- 规则中心使用四个固定逻辑 ID 读取规则，只有 `user` 可以生成候选预览；预览要求控制仓全仓洁净，只写 `workspace/runtime/web/rule-changes/` 下的短期候选和无正文事务摘要，不修改正式规则、Git、任务或审计；
- 规则应用只接收服务端 `change_id` 和进程内单次令牌；专用任务在跨进程全仓锁内重检 revision、哈希和候选，使用同目录临时文件原子替换，失败自动回滚，任务、事务和审计仅保存安全关联字段；
- 数据维护只接受 `audit`、`archived_work`、`web_tasks` 三个逻辑类别和 `1..36500` 天保留期；预览及应用均不接收或返回物理路径，应用在全局维护锁内重新扫描，候选变化、链接/重解析点、活动或不确定状态一律拒绝删除；
- 普通任务列表使用启动时建立并随写入更新的轻量投影，Working State Web 列表直接在 SQLite 中分页；全量 Markdown/任务事实遍历只保留给启动恢复、显式索引校验或数据维护扫描；
- macOS 只保留平台契约和目录位置，尚未实现或验证。

## 常用命令

从控制仓根目录运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/build-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/start-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/validate-aikb-web.ps1
```

开发前端时，先启动后端，再在 `frontend/` 运行 `npm run dev`。Vite 只把 `/api` 转发到 `http://127.0.0.1:8000`。

完整接口、读模型架构、页面空状态和安全边界见 `docs/api.md`、`docs/architecture.md`、`docs/ui-design.md` 和 `docs/security.md`。运行状态、审计、受控动作、规则写入、安装修复和数据维护是否可用，仍以启动时共享核心、恢复门禁、平台执行器和各资源接口响应为准。历史阶段契约继续见 `docs/phase-3-preconditions.md`、`docs/phase-4-preconditions.md` 与 `docs/phase-4b-install-repair-preconditions.md`；当前仍未开放其他规则/知识写入、索引重建、Git 写入或 macOS 实现。
