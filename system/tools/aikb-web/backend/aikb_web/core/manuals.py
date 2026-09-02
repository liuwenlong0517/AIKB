"""控制仓人类维护手册的只读投影。

该模块只暴露两个固定逻辑标识，避免 WebUI 演变成可浏览控制仓任意文件的
通用文件接口。正文仍从控制仓事实源即时读取，并附带当前 Git revision 和
正文哈希，供页面刷新与问题定位使用。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .gateway import GatewayError


class ManualNotFound(KeyError):
    """固定手册不存在或未能作为普通文件读取。"""


class ManualProvider:
    """按固定逻辑 ID 读取控制仓根目录中的项目与命令手册。"""

    _SPECS: dict[str, tuple[str, str]] = {
        "project": ("项目手册", "README.md"),
        "commands": ("命令手册", "COMMANDS.md"),
    }
    _REVISION = re.compile(r"^[0-9a-f]{7,40}$")

    def __init__(self, repository_root: Path | str):
        """绑定已由 Settings 校验的控制仓根目录；构造阶段不读取文件。"""
        self._root = Path(repository_root).resolve()

    def read(self, manual_id: str, *, max_chars: int = 500_000) -> dict[str, Any]:
        """读取固定手册并返回安全元数据、正文和当前控制仓 revision。

        ``manual_id`` 只能是注册表中的逻辑 ID；文件缺失、越界、编码错误或
        Git revision 不可信时均 fail-closed，不把底层路径或命令输出带给调用方。
        """
        spec = self._SPECS.get(manual_id)
        if spec is None:
            raise ManualNotFound
        if max_chars < 300 or max_chars > 500_000:
            raise ValueError("max_chars 无效")
        title, relative = spec
        path = (self._root / relative).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise GatewayError("手册服务不可用") from error
        if not path.is_file():
            raise ManualNotFound
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise GatewayError("手册服务不可用") from error
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        revision = self._revision()
        # 哈希基于实际 UTF-8 字节，避免不同平台换行转换让正文证据不一致。
        content_hash = hashlib.sha256(raw).hexdigest()
        return {
            "manual_id": manual_id,
            "title": title,
            "content": content,
            "content_hash": content_hash,
            "revision": revision,
            "truncated": truncated,
        }

    def _revision(self) -> str:
        """读取并校验当前控制仓 HEAD；不回传 Git 原始输出。"""
        try:
            result = subprocess.run(
                ["git", "-C", str(self._root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="ascii",
                errors="strict",
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise GatewayError("手册 revision 不可读取") from error
        revision = result.stdout.strip().lower()
        if not self._REVISION.fullmatch(revision):
            raise GatewayError("手册 revision 不可读取")
        return revision
