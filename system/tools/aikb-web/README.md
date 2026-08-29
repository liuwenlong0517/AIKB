# AIKB WebUI

AIKB WebUI 是 AIKB 的本地管理终端。第一阶段提供 Windows 上的正式知识总览、目录、搜索、Markdown 阅读和基础系统状态；第二阶段新增只读运行状态、检查点与脱敏审计观察面。全程不修改知识、不执行 Shell，也不开放局域网监听。

## 开发边界

- Markdown/Git 是知识事实源，SQLite/FTS 是 `workspace/db/` 下的可重建派生层；
- Web 后端复用 `../aikb-mcp/aikb/`，不复制知识扫描和查询逻辑；
- 前端只调用 `/api/v1`；
- 所有知识接口固定过滤 `status=verified`；
- 阶段 2 第一小版本的运行状态只查询活动 Working State（`planned`、`active`、`blocked`），不支持归档搜索；审计只提供脱敏摘要、筛选、分页和调用详情；
- macOS 只保留平台契约和目录位置，尚未实现或验证。

## 常用命令

从控制仓根目录运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/build-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/start-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/validate-aikb-web.ps1
```

开发前端时，先启动后端，再在 `frontend/` 运行 `npm run dev`。Vite 只把 `/api` 转发到 `http://127.0.0.1:8000`。

完整接口、读模型架构、页面空状态和安全边界见 `docs/api.md`、`docs/architecture.md`、`docs/ui-design.md` 和 `docs/security.md`。运行状态与审计路由已实现，是否可用仍以启动时共享核心初始化结果和各资源接口响应为准。
