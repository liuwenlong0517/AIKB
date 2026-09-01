"""控制仓两份固定人类手册的 HTTP 契约与白名单测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from aikb_web.main import create_app


class _Gateway:
    """仅提供应用初始化所需的最小网关夹具。"""

    settings = None

    def overview(self) -> dict[str, Any]:
        return {}


class _Provider:
    """记录逻辑 ID，确认路由不会把路径交给 provider。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def read(self, manual_id: str, *, max_chars: int = 500_000) -> dict[str, Any]:
        self.calls.append(manual_id)
        return {
            "manual_id": manual_id,
            "title": "项目手册" if manual_id == "project" else "命令手册",
            "content": "# 手册",
            "content_hash": "a" * 64,
            "revision": "b" * 40,
        }


class ManualsApiTests(unittest.TestCase):
    """固定手册 API 只接受白名单逻辑 ID，并返回安全正文投影。"""

    def test_fixed_manual_ids_return_markdown_and_metadata(self) -> None:
        provider = _Provider()
        client = TestClient(create_app(_Gateway(), manual_provider=provider))
        response = client.get("/api/v1/manuals/project")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["manual_id"], "project")
        self.assertEqual(response.json()["data"]["content"], "# 手册")
        self.assertEqual(provider.calls, ["project"])

    def test_path_like_or_unknown_ids_are_not_forwarded(self) -> None:
        provider = _Provider()
        client = TestClient(create_app(_Gateway(), manual_provider=provider))
        for identifier in ("README.md", "../README.md", "project/README.md"):
            response = client.get(f"/api/v1/manuals/{identifier}")
            self.assertIn(response.status_code, (400, 404, 422))
            self.assertNotIn("README.md", response.text)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
