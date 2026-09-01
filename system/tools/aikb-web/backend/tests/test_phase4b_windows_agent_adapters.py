"""阶段 4B Windows Agent 目标适配器的隔离幂等、冲突和回滚测试。

测试只使用临时配置根和内存事务材料，不启动真实 Codex/Claude 进程，也不访问
当前用户的 HKCU、``%USERPROFILE%`` 或 Agent 配置文件。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_materials import MaintenanceLeafMaterial, MaintenanceMaterialManifest
from aikb_web.platform.maintenance import MaintenanceStep
from aikb_web.platform.windows.maintenance_agents import (
    WindowsAgentMaintenanceAdapter,
    WindowsAgentMaintenanceError,
    WindowsAgentProbeRunner,
    _merge_claude_mcp,
    _merge_hooks,
)
from aikb_web.platform.windows.maintenance_readonly import (
    WindowsMaintenanceAdapter,
    _expected_claude_mcp,
    _expected_hooks,
    _expected_root,
    _load_json_preserving_keys,
)


class _Store:
    def __init__(self, transaction: MaintenanceChange) -> None:
        self.transaction = transaction

    def load(self, change_id: str) -> MaintenanceChange:
        if change_id != self.transaction.change_id:
            raise ValueError("missing")
        return self.transaction


class _Materials:
    def __init__(self) -> None:
        self.manifest: MaintenanceMaterialManifest | None = None

    def load(self, _change_id: str) -> MaintenanceMaterialManifest:
        assert self.manifest is not None
        return self.manifest


def _make_transaction(target_id: str, leaves: tuple[MaintenanceLeafMaterial, ...]) -> MaintenanceChange:
    prefix = "codex" if target_id == "agent.codex" else "claude-code"
    logical = tuple(item.leaf_id for item in leaves)
    return MaintenanceChange(
        change_id=f"agent-{prefix}-fixture",
        target_id=target_id,
        action_id=f"maintenance.agent.{prefix}.repair",
        risk_level="user_config_write",
        status="prepared",
        base_fingerprint="a" * 64,
        before_fingerprint="b" * 64,
        after_fingerprint="c" * 64,
        step_summary=("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify"),
        preview_digest="d" * 64,
        created_at="2026-09-01T01:00:00Z",
        expires_at="2099-09-01T01:05:00Z",
        updated_at="2026-09-01T01:00:00Z",
        leaf_states=tuple(MaintenanceLeafState(item.leaf_id, item.existence, item.before_hash, item.expected_hash) for item in leaves),
    )


class WindowsAgentAdapterTests(unittest.TestCase):
    def _readonly(self, root: Path) -> WindowsMaintenanceAdapter:
        return WindowsMaintenanceAdapter(
            fixture_root=root,
            environment={"AIKB_HOME": "home", "AIKB_KNOWLEDGE_HOME": "knowledge"},
            expected_environment={"AIKB_HOME": "home", "AIKB_KNOWLEDGE_HOME": "knowledge"},
        )

    def test_json_structure_keys_are_case_insensitive_and_preserve_names(self) -> None:
        raw = json.dumps(
            {"McpServers": {"AIKB": {"Type": "stdio", "Command": "old", "ENV": {"aikb_managed": "1"}}, "other": {"x": 1}}, "private": "保留"},
            ensure_ascii=False,
        ).encode("utf-8")
        updated = json.loads(_merge_claude_mcp(raw).decode("utf-8"))
        self.assertIn("McpServers", updated)
        self.assertIn("AIKB", updated["McpServers"])
        self.assertEqual(updated["McpServers"]["AIKB"]["Command"], "pwsh")
        self.assertEqual(updated["McpServers"]["AIKB"]["ENV"]["aikb_managed"], "1")
        self.assertEqual(updated["McpServers"]["other"], {"x": 1})
        self.assertEqual(updated["private"], "保留")

    def test_json_case_duplicate_is_rejected_before_write(self) -> None:
        raw = b'{"mcpServers": {}, "MCPSERVERS": {}}'
        # 结构歧义在共享 JSON 读取层即被拒绝，尚未进入 Agent 写适配器，
        # 因此这里验证通用结构错误边界而不是要求外层重新包装异常类型。
        with self.assertRaises(ValueError):
            _merge_claude_mcp(raw)

    def test_nonmanaged_nested_case_variants_are_retained_until_not_queried(self) -> None:
        """非 AIKB 对象的 Foo/foo 不应被 loader 或序列化器静默折叠。"""

        raw = b'{"mcpServers":{"other":{"Foo":1,"foo":2}}}'
        updated = _merge_claude_mcp(raw)
        parsed = _load_json_preserving_keys(updated)
        self.assertEqual(parsed["mcpServers"]["other"], {"Foo": 1, "foo": 2})

    def test_managed_hooks_case_variants_are_rejected_when_structure_is_accessed(self) -> None:
        raw = b'{"hooks":{},"Hooks":{}}'
        with self.assertRaises(ValueError):
            _merge_hooks(raw, "claude-code")

    def test_hooks_keep_non_aikb_groups_and_are_idempotent(self) -> None:
        original = {
            "hooks": {
                "SESSIONSTART": [{"hooks": [{"Type": "command", "Command": "other", "timeout": 1}, {"command": "aikb-hook.ps1-old"}]}],
                "OtherEvent": [{"hooks": [{"command": "private"}]}],
            },
            "private": {"x": 1},
        }
        first = _merge_hooks(json.dumps(original, ensure_ascii=False).encode(), "claude-code")
        second = _merge_hooks(first, "claude-code")
        first_value = json.loads(first.decode())
        second_value = json.loads(second.decode())
        self.assertEqual(first_value["private"], {"x": 1})
        self.assertEqual(first_value["hooks"]["SESSIONSTART"][0]["hooks"][0]["Command"], "other")
        self.assertEqual(first_value, second_value)

    def test_capture_and_apply_codex_fixture_preserves_user_text_then_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex").mkdir()
            (root / "codex" / "AGENTS.md").write_bytes(b"# user\r\n\r\n")
            (root / "codex" / "config.toml").write_bytes('model = "private"\r\n'.encode("utf-8"))
            (root / "codex" / "hooks.json").write_text('{"hooks": {}, "private": true}', encoding="utf-8")
            readonly = self._readonly(root)
            plan = readonly.plan("agent.codex")
            materials = _Materials()
            adapter = WindowsAgentMaintenanceAdapter(readonly, materials, _Store.__new__(_Store))
            # Replace the minimal transaction Store only after capture; this keeps
            # capture independent of transaction facts, as required by preparation.
            fresh, captured, _ = adapter.capture_agent(plan)
            transaction = _make_transaction("agent.codex", tuple(captured.values()))
            adapter._transactions = _Store(transaction)
            materials.manifest = MaintenanceMaterialManifest(transaction.change_id, transaction.target_id, tuple(captured.values()), (), "e" * 64)
            for step in ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks"):
                adapter.apply_step(transaction.change_id, transaction.target_id, MaintenanceStep(step))
            self.assertEqual(adapter._readonly.inspect("agent.codex").status, "ready")
            self.assertIn(b"# user\r\n", (root / "codex" / "AGENTS.md").read_bytes())
            for step in ("write_hooks", "write_mcp", "write_root_instructions"):
                adapter.rollback_step(transaction.change_id, transaction.target_id, MaintenanceStep(step))
            self.assertEqual((root / "codex" / "AGENTS.md").read_bytes(), b"# user\r\n\r\n")
            self.assertEqual((root / "codex" / "config.toml").read_bytes(), b'model = "private"\r\n')

    def test_claude_nonmanaged_mcp_conflict_is_never_overwritten(self) -> None:
        with self.assertRaises(WindowsAgentMaintenanceError):
            _merge_claude_mcp(b'{"mcpServers":{"aikb":{"command":"private"}}}')

    def test_fixed_probe_uses_only_server_commands_and_utf8_fixture_protocol(self) -> None:
        """探针请求固定、包含中文且只消费安全的进程结果。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list[tuple[tuple[str, ...], bytes, float, int]] = []

            class _Process:
                def run(self, command, payload, timeout, budget, _environment):
                    calls.append((command, payload, timeout, budget))
                    if "serve" in command:
                        return 0, (
                            b'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"aikb"}}}\n'
                            b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n'
                        ), b""
                    return 0, '{"ok":"中文"}\n'.encode("utf-8"), b""

            readonly = self._readonly(root)
            runner = WindowsAgentProbeRunner(readonly, process_executor=_Process())
            self.assertTrue(runner("codex"))
            self.assertEqual(len(calls), 5)
            self.assertTrue(all(item[2] == 10.0 and item[3] == 64 * 1024 for item in calls))
            self.assertTrue(any("aikb-probe-中文" in item[1].decode("utf-8") for item in calls))
            self.assertTrue(all("AIKB_HOME" not in str(item[0]) for item in calls))

    def test_fixed_probe_rejects_timeout_or_output_budget_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class _TooMuch:
                def run(self, *_args):
                    return 0, b"x" * (64 * 1024 + 1), b""

            self.assertFalse(WindowsAgentProbeRunner(self._readonly(root), process_executor=_TooMuch())("claude-code"))


if __name__ == "__main__":
    unittest.main()
