"""WebUI 正式启动路径的共享核心导入回归。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class StartupImportTests(unittest.TestCase):
    """验证启动脚本不依赖开发测试额外设置的 ``PYTHONPATH``。"""

    def test_main_imports_with_only_backend_on_python_path(self) -> None:
        """模拟 ``uvicorn --app-dir backend``，确认早期审计导入能够找到共享核心。"""
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", "import aikb_web.main; import aikb.audit"],
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
