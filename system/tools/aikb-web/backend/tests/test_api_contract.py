"""Web 后端第一阶段 API 契约和安全边界测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from aikb_web.main import SPAStaticFiles, create_app


class FakeGateway:
    """模拟共享核心契约，验证 HTTP 层不依赖具体 SQLite 实现。"""

    settings = None

    def overview(self) -> dict[str, Any]:
        """返回最小 verified 总览。"""
        return {"document_count": 1, "by_type": {"knowledge": 1}, "by_tag": [], "recent_documents": [], "index": {"tokenizer": "trigram"}}

    def list_documents(self, **kwargs: Any) -> list[dict[str, Any]]:
        """混入候选条目，验证 HTTP 防御性过滤。"""
        return [
            {"id": "aikb:verified", "title": "已验证", "path": "content/knowledge/a.md", "type": "knowledge", "status": "verified", "tags": ["x"]},
            {"id": "aikb:candidate", "title": "候选", "path": "content/knowledge/b.md", "type": "knowledge", "status": "candidate", "tags": ["x"]},
        ]

    def list_tags(self) -> list[dict[str, Any]]:
        """返回标签契约夹具。"""
        return [{"name": "x", "count": 1}]

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        """返回包含候选项的搜索夹具。"""
        return {"query": query, "results": [{"id": "aikb:verified", "status": "verified"}, {"id": "aikb:candidate", "status": "candidate"}]}

    def read(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        """返回 verified 文档夹具。"""
        return {"id": identifier, "status": "verified", "path": "content/knowledge/a.md", "content": "ok"}


class ApiContractTests(unittest.TestCase):
    """验证响应包络、verified 边界和路径安全。"""

    def setUp(self) -> None:
        """为每个测试创建隔离客户端。"""
        self.client = TestClient(create_app(FakeGateway()))

    def test_success_response_has_request_meta(self) -> None:
        """确认请求标识进入响应包络和响应头。"""
        response = self.client.get("/api/v1/health", headers={"X-Request-ID": "test-request-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["request_id"], "test-request-1")
        self.assertEqual(response.headers["X-Request-ID"], "test-request-1")

    def test_tree_and_search_never_return_non_verified(self) -> None:
        """确认目录和搜索双重过滤候选条目。"""
        tree = self.client.get("/api/v1/knowledge/tree").json()["data"]["root"]
        self.assertEqual([item["id"] for item in tree["children"][0]["children"]], ["aikb:verified"])
        search = self.client.get("/api/v1/knowledge/search?q=x").json()["data"]
        self.assertEqual([item["id"] for item in search["results"]], ["aikb:verified"])

    def test_absolute_path_is_rejected_without_path_echo(self) -> None:
        """确认物理路径被拒绝且不会出现在错误响应。"""
        response = self.client.get("/api/v1/knowledge/document", params={"id_or_path": "C:\\Users\\private.md"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertNotIn("private.md", str(body))

    def test_document_requires_verified_result(self) -> None:
        """确认读取接口返回 verified 文档。"""
        response = self.client.get("/api/v1/knowledge/document", params={"id_or_path": "aikb:verified"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "verified")

    def test_logical_prefix_and_stable_id_are_strictly_validated(self) -> None:
        """确认 content 根前缀可用，而伪造的稳定 ID 被拒绝。"""
        self.assertEqual(self.client.get("/api/v1/knowledge/tree", params={"prefix": "content"}).status_code, 200)
        response = self.client.get("/api/v1/knowledge/document", params={"id_or_path": "aikb:Invalid/../id"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_api_uses_json_error_contract(self) -> None:
        """确认未知 API 不会被前端单页路由吞掉。"""
        response = self.client.get("/api/v1/not-present")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_spa_static_files_fall_back_to_index_for_page_route(self) -> None:
        """确认浏览器刷新深层页面时仍返回 React 单页入口。"""
        with tempfile.TemporaryDirectory(prefix="aikb-web-static-") as temp:
            root = Path(temp)
            (root / "index.html").write_text("<html>AIKB</html>", encoding="utf-8")
            app = FastAPI()
            app.mount("/", SPAStaticFiles(directory=root, html=True), name="test-frontend")
            response = TestClient(app).get("/knowledge/view")
            self.assertEqual(response.status_code, 200)
            self.assertIn("AIKB", response.text)


if __name__ == "__main__":
    unittest.main()
