# 平台扩展说明

第一阶段只实现并验证 Windows。公共平台模型位于 `backend/aikb_web/platform/`，当前能识别 macOS，但必须返回 `supported: false`。

macOS 设备就位后再实现：

1. 确认 Intel 或 Apple Silicon 架构；
2. 验证 Python、Node、Git 和目标 Agent；
3. 在 `platform/macos/` 添加路径、环境、进程和动作实现；
4. 增加 `.sh` 或 Python 启动包装，但不要求 macOS 安装 PowerShell；
5. 回归中文和空格路径、UTF-8、大小写敏感卷、符号链接和可执行权限；
6. 在真实设备验证生成后的 Agent Handler 和长任务终止；
7. 验证通过后才把能力状态改为 `supported: true`。

macOS 实现不得改变知识 API、前端路由、动作语义 ID 或审计结构。
