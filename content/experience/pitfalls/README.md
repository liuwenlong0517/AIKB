# 工程陷阱

本目录存放已经验证的工程陷阱、触发条件和规避方式。不得记录仅凭猜测得出的风险结论。

## 条目索引

- [Windows Agent Hook 必须分别固定 Shell 解释器与 UTF-8 进程边界](windows-agent-hook-shell-encoding-boundaries.md)：区分 PowerShell 命令被 Git Bash 误解析与 Python/PowerShell 标准流编码冲突，并给出真实 handler 的诊断、修复和回归方式。
