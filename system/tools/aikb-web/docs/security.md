# AIKB WebUI 阶段 2 安全边界

## 运行边界

- 仅 Windows 本机支持，服务固定监听 127.0.0.1；启动器不接受任意监听地址、端口转发或局域网开关。
- 公共 API 只开放 GET、OPTIONS。不执行 Shell/PowerShell，不写 Markdown、Working State、审计事实源，不做 Git commit/push/reset/checkout。
- 生产模式由 FastAPI 同源提供前端静态资源；开发 CORS 只允许约定的本机 Vite 端口，不允许通配符来源。
- 未知 API 返回 JSON 404，不得回退到 HTML；页面深路由才允许 SPA 入口回退。

## 输入和逻辑标识

知识读取只接受 aikb: 稳定 ID 或 content/... 逻辑路径；Working State、检查点和审计调用只接受各自的不透明 ID。拒绝绝对路径、盘符、UNC 路径、反斜杠路径、NUL、空路径段、.、..、越界前缀以及超长参数。错误消息不回显原始值。

服务端先校验逻辑标识，再调用共享核心；绝不把浏览器字符串拼接为文件名、SQL、命令、工作目录或 Python import 路径。逻辑路径统一使用 /，但不对 macOS 预留实现假定大小写不敏感。

## 输出白名单

响应采用字段白名单而不是“读取对象后排除一个字段”。禁止任何层向浏览器传递：

- Windows 绝对路径、盘符、UNC、repo_root、knowledge_root、workspace 和 SQLite 物理路径；
- 完整 Git 状态/差异、远端 URL、环境变量和启动命令；
- SQL、底层异常原文、完整 traceback、模块导入路径和服务端日志；
- prompt、聊天全文、transcript、隐藏推理、完整 MCP payload/返回值和二进制附件；
- 审计诊断正文、原始诊断附件路径、知识正文（搜索和审计响应中）；
- token、cookie、password、private key 及其他未脱敏秘密。

Working State 列表和详情只返回紧凑恢复信息；session_id 没有可靠来源时必须为 null，session_label 同样没有可靠来源时必须为 null，不可用 Agent、connection ID、时间戳或人工标签伪造。审计优先显示真实 session_label；技术字段仍保持原值或 null，三者不得混称。

## 审计读取安全

workspace/audit/ 是独立本机操作面，不属于知识和 Working State。JSONL 是事实源，Excel/其他视图可重建。读取器要：

- 合并同一 invocation_id 的开始/结束事件，缺少结束事件标记 incomplete；
- 保留 fallback、损坏数量和局部降级标志，但不公开事件文件路径、行号和原始 JSON；
- 只返回 action/result 的安全中文摘要、限长状态和脱敏结构化摘要；
- 将 safe、diagnostic、full-local 作为捕获级别，不因 Web 查询自动提升捕获级别；
- 不提供原始诊断下载接口，也不在阶段 2 Web 模型中读取或返回诊断附件存在性。

既有审计规则继续有效：任何级别均不得记录聊天全文、隐藏推理、transcript、二进制、未脱敏密钥或完整 traceback；诊断输入输出只能在显式环境变量下脱敏、限长保存。

## Markdown 渲染

前端使用 react-markdown、GFM 和 rehype-sanitize，不启用 raw HTML。链接协议、图片来源、HTML 标签和脚本事件按严格白名单过滤。正文来自已验证逻辑文档，仍必须视为不可信输入；不能通过 Markdown 注入脚本、外部追踪或本地文件读取。

## 错误、限流和资源保护

- 所有失败使用 {error, meta}，只给稳定中文消息、错误码和安全字段提示；以 request_id 关联本机日志。
- 分页硬上限：Working State page_size<=50，审计 page_size<=100；阶段 1 搜索继续使用 limit<=20，文档正文 max_chars<=500000，检查点每个章节值 <=4000，搜索摘要 <=1600。
- 查询长度、标签数、标识长度和详情深度必须在 HTTP 层和读模型层双重限制。
- 索引不可用、审计全不可读或核心未初始化时，按资源可信度返回 503；不能用空数据掩盖故障。局部损坏使用 200 + degraded 并说明安全警告。
- 审计读取必须 fail-open 于 Agent/hook 写入语义：Web 查询失败不能阻断宿主 Agent，也不能为了展示而修改事实源。

## 发布前安全验收

至少验证：绝对路径/目录穿越/NUL 被拒且不回显；未知 API 是 JSON 404；非 GET 不执行副作用；未验证知识永不出现在总览/目录/搜索/正文；session 缺失显示 null；损坏审计记录局部降级；响应和错误中不存在盘符、物理路径、诊断正文和 traceback；服务仍只绑定 127.0.0.1。

若未来开放局域网，必须另行完成身份认证、CSRF、权限模型、TLS/可信反向代理、审计和部署评审，不能只改监听地址为 0.0.0.0。
