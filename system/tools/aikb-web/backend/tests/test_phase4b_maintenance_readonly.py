"""阶段 4B 波次 1 纯读取 inspect/plan 与隔离 Windows fixture 测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from aikb_web.core.maintenance_targets import MaintenanceManagedDifference, MaintenanceTargetError
from aikb_web.platform.windows.maintenance_readonly import (
    WindowsMaintenanceAdapter,
    _expected_claude_mcp,
    _expected_codex_mcp,
    _expected_hooks,
    _expected_root,
    build_windows_readonly_adapter,
)


def _hooks_config(agent: str) -> dict[str, object]:
    """把适配器标准受管 handler 摆成安装器使用的 JSON 组结构。"""

    hooks: dict[str, list[dict[str, object]]] = {}
    for item in _expected_hooks(agent):
        group: dict[str, object] = {"hooks": [item["handler"]]}
        if item["matcher"] is not None:
            group["matcher"] = item["matcher"]
        hooks.setdefault(str(item["event"]), []).append(group)
    return {"hooks": hooks}


class WindowsMaintenanceReadOnlyTests(unittest.TestCase):
    """所有测试只在临时 fixture 上运行，并验证 inspect/plan 无副作用。"""

    def _adapter(self, root: Path, *, environment: dict[str, str | None] | None = None) -> WindowsMaintenanceAdapter:
        return WindowsMaintenanceAdapter(
            fixture_root=root,
            environment=environment if environment is not None else {"AIKB_HOME": "fixture-home", "AIKB_KNOWLEDGE_HOME": "fixture-knowledge"},
            expected_environment={"AIKB_HOME": "fixture-home", "AIKB_KNOWLEDGE_HOME": "fixture-knowledge"},
        )

    def _write_ready_files(self, root: Path) -> None:
        (root / "codex").mkdir()
        (root / "claude").mkdir()
        (root / "codex" / "AGENTS.md").write_bytes(_expected_root())
        (root / "codex" / "config.toml").write_bytes(_expected_codex_mcp())
        (root / "codex" / "hooks.json").write_text(json.dumps(_hooks_config("codex")), encoding="utf-8")
        (root / "claude" / "CLAUDE.md").write_bytes(_expected_root())
        (root / ".claude.json").write_text(json.dumps({"mcpServers": {"aikb": _expected_claude_mcp()}}), encoding="utf-8")
        (root / "claude" / "settings.json").write_text(json.dumps(_hooks_config("claude-code")), encoding="utf-8")

    def test_ready_fixture_reports_only_safe_managed_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_ready_files(root)
            adapter = self._adapter(root)
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            for target_id in ("environment", "agent.codex", "agent.claude-code"):
                inspection = adapter.inspect(target_id)
                plan = adapter.plan(target_id, inspection)
                self.assertEqual(inspection.status, "ready")
                self.assertEqual(plan.summary_code, "no_change")
                self.assertTrue(all(item.difference_code == "unchanged" for item in inspection.differences))
                projection = {**inspection.public_dict(), **plan.public_dict()}
                serialized = json.dumps(projection, ensure_ascii=False)
                self.assertNotIn(str(root), serialized)
                for forbidden in ("physical_path", "backup_path", "content", "command", "environment_value"):
                    self.assertNotIn(forbidden, projection)
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_missing_and_drifted_states_have_structured_managed_differences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = self._adapter(root, environment={})
            environment = adapter.inspect("environment")
            self.assertEqual(environment.status, "missing")
            self.assertEqual({item.difference_code for item in environment.differences}, {"missing"})
            environment_difference = environment.differences[0]
            self.assertEqual(environment_difference.display_name, "AIKB 控制仓环境设置")
            self.assertEqual(environment_difference.change_action, "新增受管内容")
            self.assertIn("用户级 AIKB 控制仓设置", environment_difference.affected_fields)
            self.assertIn("其他用户环境变量", environment_difference.preserved_scope)
            self.assertNotIn("AIKB_HOME", json.dumps(environment_difference.public_dict(), ensure_ascii=False))
            self.assertEqual(adapter.plan("environment").summary_code, "repair_available")

            (root / "codex").mkdir()
            drifted_root = _expected_root().decode("utf-8").replace("ENTRY_RULES.md", "OTHER_RULES.md")
            (root / "codex" / "AGENTS.md").write_text("user\n" + drifted_root, encoding="utf-8")
            (root / "codex" / "config.toml").write_bytes(_expected_codex_mcp())
            (root / "codex" / "hooks.json").write_text(json.dumps(_hooks_config("codex")), encoding="utf-8")
            drifted = adapter.inspect("agent.codex")
            self.assertEqual(drifted.status, "drifted")
            self.assertEqual(drifted.differences[0].difference_code, "drifted")
            self.assertIsNotNone(drifted.differences[0].before_hash)
            self.assertIsNotNone(drifted.differences[0].after_hash)
            self.assertEqual(drifted.differences[0].change_action, "更新受管内容")
            self.assertEqual(drifted.differences[0].expected_summary, "替换为当前版本的受管内容")

    def test_conflict_invalid_and_reparse_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex").mkdir()
            (root / "codex" / "config.toml").write_text("[mcp_servers.aikb]\ncommand='other'\n", encoding="utf-8")
            conflict = self._adapter(root).inspect("agent.codex")
            self.assertEqual(conflict.status, "conflict")
            self.assertEqual(conflict.reason_code, "managed_conflict")

            (root / "claude").mkdir()
            (root / ".claude.json").write_text("{broken", encoding="utf-8")
            invalid = self._adapter(root).inspect("agent.claude-code")
            self.assertEqual(invalid.status, "invalid")
            self.assertEqual(invalid.reason_code, "target_invalid")

            try:
                (root / "codex" / "AGENTS.md").symlink_to(root / "missing-target")
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("当前 Windows 测试账户未启用创建符号链接权限")
                raise
            reparse = self._adapter(root).inspect("agent.codex")
            self.assertEqual(reparse.status, "invalid")

    def test_codex_external_mcp_inside_legacy_markers_is_not_drift(self) -> None:
        """Codex 插入结束标记前的外部表不属于 AIKB 受管正文。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_ready_files(root)
            path = root / "codex" / "config.toml"
            raw = path.read_bytes().replace(
                b"# <<< AIKB managed MCP <<<\n",
                b"[mcp_servers.cua_repl]\ncommand = \"host.exe\"\n\n# <<< AIKB managed MCP <<<\n",
            )
            path.write_bytes(raw)
            status = self._adapter(root).inspect("agent.codex")
            self.assertEqual(status.status, "ready")
            self.assertEqual([item.difference_code for item in status.differences], ["unchanged", "unchanged", "unchanged"])

    def test_codex_managed_value_drift_still_detected_with_external_table_inside_markers(self) -> None:
        """忽略外部表不能掩盖 AIKB 自身字段的真实漂移。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_ready_files(root)
            path = root / "codex" / "config.toml"
            raw = path.read_bytes().replace(b'command = "pwsh"', b'command = "other"').replace(
                b"# <<< AIKB managed MCP <<<\n",
                b"[mcp_servers.cua_repl]\ncommand = \"host.exe\"\n\n# <<< AIKB managed MCP <<<\n",
            )
            path.write_bytes(raw)
            status = self._adapter(root).inspect("agent.codex")
            self.assertEqual(status.status, "drifted")
            self.assertEqual(status.differences[1].difference_code, "drifted")

    def test_orphaned_managed_markers_are_invalid_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex").mkdir()
            (root / "codex" / "AGENTS.md").write_text("before\n<!-- >>> AIKB managed root instruction >>> -->\n", encoding="utf-8")
            orphan_root = self._adapter(root).inspect("agent.codex")
            self.assertEqual(orphan_root.status, "invalid")
            (root / "codex" / "AGENTS.md").unlink()
            (root / "codex" / "config.toml").write_text("# <<< AIKB managed MCP <<<\n", encoding="utf-8")
            orphan_mcp = self._adapter(root).inspect("agent.codex")
            self.assertEqual(orphan_mcp.status, "invalid")

    def test_production_factory_rejects_codex_home_outside_user_boundary(self) -> None:
        settings = SimpleNamespace(repo_root=Path("C:/aikb"), knowledge_root=Path("C:/aikb-content"))
        with patch.dict("os.environ", {"CODEX_HOME": "C:/other-user/.codex"}, clear=False):
            with self.assertRaises(MaintenanceTargetError):
                build_windows_readonly_adapter(settings)

    def test_production_factory_construction_does_not_scan_or_create_runtime_material(self) -> None:
        """真实工厂只组装固定路径和环境快照，配置读取延迟到 inspect。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(repo_root=root / "repo", knowledge_root=root / "knowledge")
            with patch.dict("os.environ", {"CODEX_HOME": ""}, clear=False):
                adapter = build_windows_readonly_adapter(settings)
            self.assertIsInstance(adapter, WindowsMaintenanceAdapter)
            self.assertFalse((root / "runtime").exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_production_environment_reader_is_lazy_and_queries_only_fixed_hkcu_values(self) -> None:
        """工厂不访问注册表；inspect 只查询两个固定 HKCU\Environment 值。"""

        calls: list[tuple[str, str]] = []

        class _Key:
            def __enter__(self) -> "_Key":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        class _Winreg:
            HKEY_CURRENT_USER = object()
            KEY_READ = 0x20019

            @staticmethod
            def OpenKey(_root: object, subkey: str, _reserved: int, _access: int) -> _Key:
                calls.append(("OpenKey", subkey))
                return _Key()

            @staticmethod
            def QueryValueEx(_key: _Key, name: str) -> tuple[object, int]:
                calls.append(("QueryValueEx", name))
                if name == "AIKB_HOME":
                    raise FileNotFoundError(name)
                return "", 1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(repo_root=root / "repo", knowledge_root=root / "knowledge")
            fake_winreg = _Winreg()
            with patch.dict(sys.modules, {"winreg": fake_winreg}), patch.dict(os.environ, {"CODEX_HOME": ""}, clear=False):
                adapter = build_windows_readonly_adapter(settings)
                self.assertEqual(calls, [])
                status = adapter.inspect("environment")
            self.assertEqual(calls, [("OpenKey", "Environment"), ("QueryValueEx", "AIKB_HOME"), ("QueryValueEx", "AIKB_KNOWLEDGE_HOME")])
            self.assertEqual(status.status, "drifted")
            self.assertEqual([item.difference_code for item in status.differences], ["missing", "drifted"])
            self.assertNotIn("AIKB_HOME", json.dumps(status.public_dict()))

    def test_create_app_default_factory_is_read_only_and_explicit_injection_wins(self) -> None:
        """应用构造只装配对象，不创建运行材料；显式适配器优先于默认工厂。"""

        from aikb_web.main import create_app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(repo_root=root / "repo", knowledge_root=root / "knowledge")
            gateway = SimpleNamespace(settings=settings, overview=lambda: {"index": {"available": False}})
            sentinel = object()
            with patch("aikb_web.main.platform_state", return_value=SimpleNamespace(platform="windows")), patch(
                "aikb_web.main.build_windows_readonly_adapter", return_value=sentinel
            ) as factory:
                app = create_app(gateway)
            self.assertIs(app.state.maintenance_adapter, sentinel)
            factory.assert_called_once_with(settings)
            self.assertFalse((root / "runtime").exists())
            explicit = object()
            with patch("aikb_web.main.build_windows_readonly_adapter") as unused_factory:
                explicit_app = create_app(gateway, maintenance_adapter=explicit)
            self.assertIs(explicit_app.state.maintenance_adapter, explicit)
            unused_factory.assert_not_called()

    def test_status_and_plan_reject_semantically_inconsistent_differences(self) -> None:
        from aikb_web.core.maintenance_targets import MaintenanceTargetStatus
        from aikb_web.platform.maintenance import MaintenancePlan, MaintenanceStep

        leaves = ("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks")
        steps = ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify")
        differences = tuple(MaintenanceManagedDifference(leaf, "drifted") for leaf in leaves)
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceTargetStatus(
                target_id="agent.codex",
                status="ready",
                logical_leaves=leaves,
                steps=steps,
                base_fingerprint="a" * 64,
                differences=differences,
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlan(
                target_id="agent.codex",
                steps=tuple(MaintenanceStep(step) for step in steps),
                logical_leaves=leaves,
                before_fingerprint="a" * 64,
                after_fingerprint="b" * 64,
                preview_digest="c" * 64,
                differences=differences,
                summary_code="no_change",
            )

    def test_difference_semantics_reject_free_text_injection(self) -> None:
        """公开语义只能来自叶子白名单，不能借模型回显路径或秘密。"""

        with self.assertRaises(MaintenanceTargetError):
            MaintenanceManagedDifference(
                "agent.codex.mcp",
                "drifted",
                affected_fields=("C:\\secret.json",),
            )

    def test_unknown_target_and_injected_environment_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(MaintenanceTargetError):
                self._adapter(root).inspect("../environment")
            with self.assertRaises(MaintenanceTargetError):
                WindowsMaintenanceAdapter(
                    fixture_root=root,
                    environment={"AIKB_HOME": "x", "AIKB_KNOWLEDGE_HOME": "y", "SECRET": "z"},
                    expected_environment={"AIKB_HOME": "x", "AIKB_KNOWLEDGE_HOME": "y"},
                )


if __name__ == "__main__":
    unittest.main()
