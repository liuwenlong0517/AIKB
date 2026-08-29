# macOS 平台扩展占位

第一阶段不实现 macOS 环境变量、Hook、进程或动作执行能力。本目录只保留未来适配位置；在真实 macOS 设备完成回归前，能力接口必须返回 `supported: false`。

未来实现必须遵循 `platform/base.py` 的公共契约，不得改变知识 API、任务模型或前端路由来迁就平台脚本。
