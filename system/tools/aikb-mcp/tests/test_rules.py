"""阶段 4 共享规则注册与候选覆盖验证回归测试。"""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.rules import (
    RuleValidationError,
    get_rule,
    list_rules,
    validate_auxiliary_file,
    validate_candidate_file,
    validate_content,
    validate_registered_rules,
    validate_rule_file,
    target_path,
)


class RuleRegistryTests(unittest.TestCase):
    """验证静态规则能力和共享核心约束。"""

    def setUp(self) -> None:
        """读取当前控制仓作为正式规则回归样本。"""
        self.root = Path(__file__).resolve().parents[4]
        self.user_file = self.root / "system" / "rules" / "USER_RULES.md"
        self.user_text = self.user_file.read_text(encoding="utf-8")

    def test_registry_contains_only_four_static_ids_and_user_is_only_writable(self) -> None:
        """未知 ID 不可动态扩展，首批只有 user 允许写入。"""
        self.assertEqual([item.rule_id for item in list_rules()], ["entry", "user", "agent", "contributing"])
        self.assertEqual([item.rule_id for item in list_rules() if item.writable], ["user"])
        with self.assertRaises(RuleValidationError):
            get_rule("INDEX")

    def test_existing_rules_pass_shared_validation(self) -> None:
        """正式四项规则均通过与结构脚本共享的验证器。"""
        report = validate_registered_rules(self.root)
        self.assertTrue(report["valid"], report)
        self.assertEqual({item["rule_id"] for item in report["rules"]}, {"entry", "user", "agent", "contributing"})

    def test_missing_duty_and_forbidden_terms_are_rejected(self) -> None:
        """职责词缺失或混入其他层禁止词时，候选只返回失败不触碰文件。"""
        missing = validate_content("user", "# 个人规则\n\n普通正文\n")
        self.assertFalse(missing.valid)
        self.assertTrue(any("职责闭环" in error for error in missing.errors))
        forbidden = validate_content("user", self.user_text + "\nsearch_knowledge\n")
        self.assertFalse(forbidden.valid)
        self.assertTrue(any("混入其他层职责" in error for error in forbidden.errors))

    def test_budget_absolute_path_and_bad_unicode_are_rejected(self) -> None:
        """验证字符/字节预算、绝对路径、U+FFFD、NUL、BOM 和超长行边界。"""
        self.assertFalse(validate_content("user", self.user_text + "x" * 200).valid)
        for path in ("C:\\secret\\rule.md", "\\\\server\\share\\rule.md", "/etc/passwd"):
            result = validate_content("user", self.user_text + "\n" + path)
            self.assertFalse(result.valid, path)
            self.assertTrue(any("绝对路径" in error for error in result.errors))
        for bad in (self.user_text + "\ufffd", self.user_text + "\x00"):
            self.assertFalse(validate_content("user", bad).valid)
        self.assertFalse(validate_content("user", b"\xef\xbb\xbf" + self.user_text.encode()).valid)
        self.assertFalse(validate_content("user", self.user_text + "\n" + "a" * 4097).valid)

    def test_readonly_rule_and_candidate_do_not_modify_formal_file(self) -> None:
        """只读规则拒绝覆盖；合法 user 候选规范化后仍不写正式文件。"""
        with tempfile.TemporaryDirectory(prefix="aikb-rules-") as directory:
            candidate = Path(directory) / "candidate.md"
            candidate.write_bytes(self.user_text.replace("\n", "\r\n").encode("utf-8"))
            before = self.user_file.read_bytes()
            result = validate_candidate_file("user", candidate)
            self.assertTrue(result.valid, result.errors)
            self.assertNotIn("\r\n", result.normalized_content)
            self.assertEqual(before, self.user_file.read_bytes())
            readonly = validate_candidate_file("entry", candidate)
            self.assertFalse(readonly.valid)
            self.assertIn("只读", readonly.errors[0])

    def test_candidate_file_must_be_regular_file(self) -> None:
        """候选必须是服务端已创建的普通文件，目录和符号链接不进入验证。"""
        with tempfile.TemporaryDirectory(prefix="aikb-rules-") as directory:
            folder = Path(directory) / "folder"
            folder.mkdir()
            result = validate_rule_file("user", folder)
            self.assertFalse(result.valid)
            self.assertIn("普通文件", result.errors[0])
            auxiliary = validate_auxiliary_file("INDEX.md", folder)
            self.assertFalse(auxiliary.valid)
            self.assertIn("普通文件", auxiliary.errors[0])

    def test_formal_rule_target_cannot_be_symlink(self) -> None:
        """静态路径即使仍位于仓库内，也不能通过符号链接替换正式规则目标。"""
        with tempfile.TemporaryDirectory(prefix="aikb-rule-target-") as directory:
            root = Path(directory)
            target = root / "ENTRY_RULES.md"
            actual = root / "actual.md"
            actual.write_text("placeholder", encoding="utf-8")
            try:
                target.symlink_to(actual)
            except OSError:
                self.skipTest("当前 Windows 环境不允许创建符号链接")
            with self.assertRaises(RuleValidationError):
                target_path(root, "entry")


if __name__ == "__main__":
    unittest.main()
