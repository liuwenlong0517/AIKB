"""阶段 4A 波次 3 发布与双仓边界终审。

本文件只做只读核对；规则写入验收必须使用其他测试创建的临时控制仓，绝不把
当前 AIKB checkout 当作写入夹具。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from aikb_web.main import create_app


class Phase4Wave3ReleaseSecurityTests(unittest.TestCase):
    """核对发布路由、双仓 checkout 和正式运行面边界。"""

    def test_openapi_rule_routes_and_methods_are_frozen(self) -> None:
        """OpenAPI 只暴露固定规则读、预览、apply 和 change 状态路由。"""
        app = create_app(gateway=object())
        paths = app.openapi()["paths"]
        rule_paths = {path: set(value) for path, value in paths.items() if path.startswith("/api/v1/rules")}
        self.assertEqual(
            rule_paths,
            {
                "/api/v1/rules": {"get"},
                "/api/v1/rules/{rule_id}": {"get"},
                "/api/v1/rules/{rule_id}/preview": {"post"},
                "/api/v1/rules/{rule_id}/apply": {"post"},
                "/api/v1/rules/changes/{change_id}": {"get"},
            },
        )
        self.assertNotIn("/api/v1/rule-changes/{change_id}", paths)
        self.assertNotIn("/api/v1/rules/{rule_id}/apply/", paths)

    def test_control_and_knowledge_checkout_are_unchanged_by_release_checks(self) -> None:
        """终审前后只读取双仓，提交哈希和工作树状态不发生变化。"""
        control = Path(os.environ["AIKB_HOME"]).resolve()
        knowledge = Path(os.environ.get("AIKB_KNOWLEDGE_HOME", str(control / "content"))).resolve()

        def snapshot(root: Path) -> tuple[str, str]:
            """读取指定 Git 根的完整 HEAD 与 porcelain 状态。"""
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                text=True, encoding="utf-8", check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain=v1"], capture_output=True,
                text=True, encoding="utf-8", check=True,
            ).stdout
            return revision, status

        before = (snapshot(control), snapshot(knowledge))
        # 仅构造应用和 OpenAPI；不进入 lifespan，不创建任务/恢复事实源。
        create_app(gateway=object()).openapi()
        after = (snapshot(control), snapshot(knowledge))
        self.assertEqual(before, after)

        # 不把某个开发提交写死进长期回归；提交波次 3 后 HEAD 合法变化，
        # 真正需要冻结的是同一次终审动作前后 revision/status 完全一致。
        self.assertRegex(before[0][0], r"^[0-9a-f]{40}$")
        self.assertRegex(before[1][0], r"^[0-9a-f]{40}$")

    def test_release_scripts_bind_loopback_and_do_not_offer_git_write(self) -> None:
        """启动脚本固定回环地址，发布脚本不包含 Git 写操作。"""
        web_root = Path(__file__).resolve().parents[1].parent
        start = (web_root / "scripts" / "start-aikb-web.ps1").read_text(encoding="utf-8")
        validate = (web_root / "scripts" / "validate-aikb-web.ps1").read_text(encoding="utf-8")
        self.assertIn("--host', '127.0.0.1'", start)
        self.assertNotIn("git commit", start.lower() + validate.lower())
        self.assertNotIn("git push", start.lower() + validate.lower())


if __name__ == "__main__":
    unittest.main()
