# AIKB WebUI

AIKB WebUI 是 AIKB 的本地管理终端。第一阶段仅提供 Windows 上的正式知识总览、目录、搜索、Markdown 阅读和基础系统状态，不修改知识、不执行 Shell，也不开放局域网监听。

## 开发边界

- Markdown/Git 是知识事实源，SQLite/FTS 是 `workspace/db/` 下的可重建派生层；
- Web 后端复用 `../aikb-mcp/aikb/`，不复制知识扫描和查询逻辑；
- 前端只调用 `/api/v1`；
- 所有知识接口固定过滤 `status=verified`；
- macOS 只保留平台契约和目录位置，尚未实现或验证。

## 常用命令

从控制仓根目录运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/build-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/start-aikb-web.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File system/tools/aikb-web/scripts/validate-aikb-web.ps1
```

开发前端时，先启动后端，再在 `frontend/` 运行 `npm run dev`。Vite 只把 `/api` 转发到 `http://127.0.0.1:8000`。

完整接口和安全边界见 `docs/`。
