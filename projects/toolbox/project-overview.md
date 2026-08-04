---
status: verified
tags: [toolbox, static-web, javascript, developer-tools]
applicable_versions: "ToolBox commit 0253cc7"
last_verified: 2026-08-04
review_when: "核心框架、工具脚本、构建方式、外部 API 或测试体系发生变化时"
supersedes: []
---

# ToolBox 项目架构与功能全景

## 背景

ToolBox 是位于 `E:\CodeSpace\codex\ToolBox` 的开发者日常工具集合。当前仓库只有一个初始化提交 `0253cc7`，需要保留一份可复用的项目入口、功能边界与验证基线。同一基线还有一个并行副本 `E:\CodeSpace\claude\ToolBox`（内容与 codex 副本除 `.claude`/`.git` 外完全一致），供 Claude Code 会话使用。

## 问题

后续开发需要快速判断项目如何运行、如何扩展工具、哪些能力完全在浏览器本地完成、哪些能力会访问外部服务，以及页面描述与实现之间有哪些边界。

## 解决方案

- 技术形态：零构建、零后端、无包管理器的原生 HTML/CSS/JavaScript 单页应用。入口为 `index.html`，样式集中在 `styles.css`，直接通过 `<script>` 顺序加载 `app/core.js` 和 20 个工具脚本；使用本地静态 HTTP 服务即可运行。
- 框架机制：`app/core.js` 定义工具元数据、11 个分类、筛选排序状态和列表/工具视图切换；每个工具文件调用 `registerTool(id, { render, init })` 注册实现。新增工具至少需要增加工具元数据、创建注册脚本并在 `index.html` 中按顺序加载。
- 主界面能力：分类计数、名称/描述/标签搜索、中文名称正序或倒序、网格/列表视图切换；视图偏好通过 `localStorage` 保存。
- 20 个工具：JSON 格式化/压缩/校验、Base64 编解码、URL 编解码、MD5、SHA、时间戳转换、正则匹配/替换、UUID 生成、密码生成、颜色转换、Markdown 预览与 HTML 下载、文本去重/排序/计数/反转、JS/CSS/HTML 压缩、二维码生成、IP 查询、Unix 权限计算、字符串统计、HTML 实体转义。
- 外部访问：二维码将输入发送给 `api.qrserver.com` 生成图片；IP 查询调用 `ipapi.co`。其余主要处理在浏览器本地完成，但剪贴板、Web Crypto 和下载能力依赖浏览器 API 与安全上下文。
- 已知实现边界：工具描述中提到的 JSON 树形查看、MD5 文件校验、正则常用参考、JS 混淆、完整 GFM 支持并未在当前实现中出现；UUID v1 是自定义近似实现且 UUID 使用 `Math.random()`；JS/CSS/HTML 压缩器和 Markdown 解析器均为正则驱动的简化实现，不适合作为语义完备的生产级处理器；Markdown 预览直接把转换结果写入 `innerHTML`，不能安全预览不可信输入；IP API 返回值也直接拼入 `innerHTML`。
- 工程现状：仓库没有 README、构建配置、自动化测试、lint 或 CI 配置。

## 验证

- 2026-08-04 逐一阅读 `index.html`、`styles.css`、`app/core.js` 和 `app/tools/tool-01` 至 `tool-20` 的实现。
- 对 `app/core.js` 与 20 个工具脚本运行 `node --check`，共 21 个文件语法检查通过。
- 通过本地 HTTP 服务进行浏览器冒烟验证：首页显示 20 个工具；搜索“哈希”得到 2 个工具且分类计数同步；JSON 工具可打开并将 `{"b":2,"a":1}` 成功格式化；浏览器控制台未记录错误。
- 验证时业务仓库 `git status` 为干净的 `main` 分支。

## 适用范围

仅适用于 ToolBox 提交 `0253cc7` 的当前静态前端基线。若引入构建工具、框架、后端、第三方库，或修改任何工具实现，应按 `review_when` 重新核验。对第三方 API 的可用性、额度、隐私策略和跨域规则未做长期保证。

## 关联信息

- 项目入口：`E:\CodeSpace\codex\ToolBox\index.html`（并行副本：`E:\CodeSpace\claude\ToolBox\index.html`）
- 核心框架：`E:\CodeSpace\codex\ToolBox\app\core.js`
- 工具实现：`E:\CodeSpace\codex\ToolBox\app\tools\`
