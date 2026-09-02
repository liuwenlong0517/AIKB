"""Web 公共响应尾门的路径脱敏回归。"""

from __future__ import annotations

import unittest

from aikb_web.api.v1.common import _sanitize_public


class PublicSanitizerTests(unittest.TestCase):
    """验证未来扩展字段不能绕过物理路径边界，同时保留公开事实内容。"""

    def test_unknown_path_keys_are_removed_case_insensitively(self) -> None:
        source = {
            "work_dir": r"C:\Users\alice\.aikb\workspace",
            "config_path": r"C:\Users\alice\.codex\config.toml",
            "install_directory": "/opt/aikb",
            "Workspace_Path": r"D:\private\workspace",
            "safe_name": "AIKB",
        }

        self.assertEqual(_sanitize_public(source), {"safe_name": "AIKB"})

    def test_absolute_values_are_redacted_recursively(self) -> None:
        source = {
            "location": r"C:\Users\alice\project",
            "nested": {
                "backup": r"\\server\private-share\backup.json",
                "items": ["safe", "/home/alice/private.json", ("content/knowledge/a.md", "file:///tmp/a")],
            },
        }

        self.assertEqual(_sanitize_public(source), {
            "location": "[LOCAL_PATH]",
            "nested": {
                "backup": "[LOCAL_PATH]",
                "items": ["safe", "[LOCAL_PATH]", ["content/knowledge/a.md", "[LOCAL_PATH]"]],
            },
        })

    def test_logical_paths_urls_and_public_content_are_preserved(self) -> None:
        markdown = "# 示例\n\n路径示例：C:\\Users\\alice\\demo.txt"
        source = {
            "path": "content/knowledge/a.md",
            "source_path": "content/solutions/b.md",
            "homepage": "https://example.test/docs",
            "content": markdown,
        }

        self.assertEqual(_sanitize_public(source), source)

    def test_generic_path_fields_still_reject_nonlogical_values(self) -> None:
        sanitized = _sanitize_public({
            "path": r"C:\private\a.md",
            "source_path": "/srv/private/b.md",
            "title": "安全标题",
        })

        self.assertEqual(sanitized, {"title": "安全标题"})


if __name__ == "__main__":
    unittest.main()
