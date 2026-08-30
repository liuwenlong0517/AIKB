"""AIKB 本地管理 WebUI 后端。

本包只提供 HTTP 适配、公开 DTO 和安全边界；知识事实仍由共享核心负责。
正式启动脚本使用 ``uvicorn --app-dir backend``，因此 Python 默认只能看到
Web 后端目录。包初始化阶段只补充版本库内固定的 sibling ``aikb-mcp`` 路径，
确保所有直接共享核心导入具有同一启动契约，而不是依赖某个网关碰巧先加载。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_shared_core() -> None:
    """将受版本控制的共享核心加入导入路径。

    路径由当前包位置静态推导，不读取请求、配置文件或可由浏览器覆盖的值。
    如果运行的是不含 sibling 核心的独立安装包，这里保持不变，让后续导入以
    明确的 ``ModuleNotFoundError`` 失败，不能回退到磁盘扫描或未知目录。
    """
    core_root = Path(__file__).resolve().parents[3] / "aikb-mcp"
    package_marker = core_root / "aikb" / "__init__.py"
    if package_marker.is_file() and str(core_root) not in sys.path:
        sys.path.insert(0, str(core_root))


_bootstrap_shared_core()

__version__ = "0.1.0"
