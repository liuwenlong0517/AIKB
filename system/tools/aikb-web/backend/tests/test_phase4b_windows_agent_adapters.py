"""阶段 4B Windows Agent 目标适配器的隔离幂等、冲突和回滚测试。

测试只使用临时配置根和内存事务材料，不启动真实 Codex/Claude 进程，也不访问
当前用户的 HKCU、``%USERPROFILE%`` 或 Agent 配置文件。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aikb_web.core.actions import ConfirmationTokenService
from aikb_web.core.maintenance_changes import MaintenanceChange, MaintenanceLeafState
from aikb_web.core.maintenance_materials import MaintenanceLeafMaterial, MaintenanceMaterialManifest
from aikb_web.core.maintenance_preparation import MaintenancePreparationError, MaintenancePreparationService
from aikb_web.platform.maintenance import MaintenanceStep
from aikb_web.platform.windows.maintenance_agents import (
    WindowsAgentMaintenanceAdapter,
    WindowsAgentMaintenanceError,
    WindowsAgentProbeRunner,
    _merge_codex_mcp,
    _merge_claude_mcp,
    _merge_hooks,
)
from aikb_web.platform.windows.maintenance_readonly import (
    WindowsMaintenanceAdapter,
    _expected_claude_mcp,
    _expected_codex_mcp,
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


class _PreparationTransactions:
    """只记录准备阶段创建的事务，不接触正式运行目录。"""

    def __init__(self) -> None:
        self.items: list[MaintenanceChange] = []

    def create(self, value: MaintenanceChange) -> None:
        self.items.append(value)


class _PreparationMaterials:
    """只记录私有材料正文，供测试断言非受管配置得到保留。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def prepare(self, *args: object) -> None:
        self.calls.append(args)


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

    def test_atomic_write_checks_existing_parent_chain_before_creating_directories(self) -> None:
        """重解析点在现存父链时应先拒绝，拒绝前不得留下新目录。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readonly = self._readonly(root)
            adapter = object.__new__(WindowsAgentMaintenanceAdapter)
            adapter._readonly = readonly
            path = root / "codex" / "new" / "config.toml"
            with patch.object(readonly, "_has_reparse_component", return_value=True):
                with self.assertRaises(WindowsAgentMaintenanceError):
                    adapter._atomic_write(path, b"model = 'x'\n", None)
            self.assertFalse(path.parent.exists())

    def test_atomic_write_rechecks_parent_chain_after_creating_directories(self) -> None:
        """创建缺失父目录后若边界复核失败，仍不得创建临时配置文件。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readonly = self._readonly(root)
            adapter = object.__new__(WindowsAgentMaintenanceAdapter)
            adapter._readonly = readonly
            path = root / "codex" / "new" / "config.toml"
            with patch.object(readonly, "_has_reparse_component", side_effect=[False, True]):
                with self.assertRaises(WindowsAgentMaintenanceError):
                    adapter._atomic_write(path, b"model = 'x'\n", None)
            self.assertTrue(path.parent.exists())
            self.assertFalse(path.exists())

    def test_claude_ready_mcp_merge_is_byte_for_byte_noop(self) -> None:
        """受管 MCP 已就绪时不重写整个配置文件，保留第三方格式和正文。"""
        raw = (
            b'{\r\n  "mcpServers": {\r\n    "aikb": {\r\n'
            b'      "type": "stdio", "command": "pwsh",\r\n'
            b'      "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", '
            b'"& (Join-Path $env:AIKB_HOME \'system/tools/aikb-mcp/scripts/aikb.ps1\') serve --agent claude-code"],\r\n'
            b'      "env": {"AIKB_MANAGED": "1"}\r\n'
            b'    }, "other": {"command": "third-party"}\r\n'
            b'  }, "private": "preserve"\r\n}\r\n'
        )
        self.assertEqual(_merge_claude_mcp(raw), raw)

    def test_claude_mcp_drift_is_merged_and_third_party_is_retained(self) -> None:
        """受管 MCP 漂移仍只修复 AIKB 对象，并保留外部服务。"""
        raw = json.dumps(
            {"mcpServers": {"aikb": {**_expected_claude_mcp(), "command": "private"}, "other": {"command": "third-party"}}, "private": True},
            ensure_ascii=False,
        ).encode("utf-8")
        merged = json.loads(_merge_claude_mcp(raw).decode("utf-8"))
        self.assertEqual(merged["mcpServers"]["aikb"], _expected_claude_mcp())
        self.assertEqual(merged["mcpServers"]["other"], {"command": "third-party"})
        self.assertTrue(merged["private"])

    def test_codex_ready_mcp_with_external_table_inside_legacy_markers_is_byte_noop(self) -> None:
        """外部 MCP 被 Codex 插入旧标记内时，语义就绪的配置不应被重写。"""

        raw = _expected_codex_mcp().replace(
            b"# <<< AIKB managed MCP <<<\n",
            b"[mcp_servers.cua_repl]\ncommand = \"host.exe\"\n\n# <<< AIKB managed MCP <<<\n",
        )
        self.assertEqual(_merge_codex_mcp(raw), raw)

    def test_codex_mcp_drift_repair_preserves_external_table_inside_legacy_markers(self) -> None:
        """修复 AIKB 表时只迁回结束标记，不得删除被夹入的外部 MCP 表。"""

        raw = _expected_codex_mcp().replace(b'command = "pwsh"', b'command = "old"').replace(
            b"# <<< AIKB managed MCP <<<\n",
            b"[mcp_servers.aikb.env]\nSTALE = \"managed-child\"\n\n"
            b"[mcp_servers.cua_repl]\ncommand = \"host.exe\"\n\n# <<< AIKB managed MCP <<<\n",
        )
        merged = _merge_codex_mcp(raw)
        parsed = tomllib.loads(merged.decode("utf-8"))
        self.assertEqual(parsed["mcp_servers"]["aikb"]["command"], "pwsh")
        self.assertNotIn("env", parsed["mcp_servers"]["aikb"])
        self.assertEqual(parsed["mcp_servers"]["cua_repl"]["command"], "host.exe")
        self.assertEqual(merged.count(b"# >>> AIKB managed MCP >>>"), 1)
        self.assertEqual(merged.count(b"# <<< AIKB managed MCP <<<"), 1)
        self.assertLess(merged.index(b"# <<< AIKB managed MCP <<<"), merged.index(b"[mcp_servers.cua_repl]"))

    def test_claude_root_drift_keeps_ready_mcp_material_bytes(self) -> None:
        """仅 root 漂移时，Claude MCP 期望材料必须沿用原始字节。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude").mkdir()
            (root / "claude" / "CLAUDE.md").write_bytes(_expected_root().replace(b"ENTRY_RULES.md", b"OTHER_RULES.md"))
            mcp_raw = (
                b'{"mcpServers":{"aikb":'
                + json.dumps(_expected_claude_mcp(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b'},"private":"preserve"}'
            )
            (root / ".claude.json").write_bytes(mcp_raw)
            hooks = {"hooks": {}}
            for item in _expected_hooks("claude-code"):
                group = {"hooks": [item["handler"]]}
                if item["matcher"] is not None:
                    group["matcher"] = item["matcher"]
                hooks["hooks"].setdefault(str(item["event"]), []).append(group)
            (root / "claude" / "settings.json").write_bytes(json.dumps(hooks).encode("utf-8"))
            readonly = self._readonly(root)
            self.assertEqual([item.difference_code for item in readonly.inspect("agent.claude-code").differences], ["drifted", "unchanged", "unchanged"])
            plan = readonly.plan("agent.claude-code")
            adapter = WindowsAgentMaintenanceAdapter(readonly, _Materials(), type("S", (), {"load": lambda _self, _id: None})())
            _fresh, captured, _ = adapter.capture_agent(plan)
            mcp = captured["agent.claude-code.mcp"]
            self.assertEqual(mcp.expected_hash, mcp.before_hash)
            self.assertEqual(mcp.expected_bytes, mcp_raw)

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

    def _write_agent_material_fixture(self, root: Path, target_id: str) -> None:
        """写入带第三方内容且仅受管 MCP 漂移的隔离 Codex/Claude fixture。"""
        if target_id == "agent.codex":
            home = root / "codex"
            home.mkdir()
            (home / "AGENTS.md").write_bytes(b"# private instructions\n")
            codex_mcp = _expected_codex_mcp().replace(b'command = "pwsh"', b'command = "private-pwsh"')
            (home / "config.toml").write_bytes(b'model = "private-model"\n' + codex_mcp)
            hooks = {"hooks": {"SessionStart": [{"hooks": [{"command": "third-party"}]}]}, "private": True}
            (home / "hooks.json").write_bytes(json.dumps(hooks, ensure_ascii=False).encode("utf-8"))
            return

        home = root / "claude"
        home.mkdir()
        (home / "CLAUDE.md").write_bytes(b"# private instructions\n")
        mcp = dict(_expected_claude_mcp())
        mcp["command"] = "private-pwsh"
        config = {"mcpServers": {"aikb": mcp, "other": {"command": "third-party"}}, "private": "保留"}
        (root / ".claude.json").write_bytes(json.dumps(config, ensure_ascii=False).encode("utf-8"))
        hooks = {"hooks": {"SessionStart": [{"hooks": [{"command": "third-party"}]}]}, "private": True}
        (home / "settings.json").write_bytes(json.dumps(hooks, ensure_ascii=False).encode("utf-8"))

    def _prepare_agent_fixture(self, root: Path, target_id: str):
        """通过真实准备服务材料化隔离 Agent，返回计划、适配器和材料记录。"""
        self._write_agent_material_fixture(root, target_id)
        readonly = self._readonly(root)
        status = readonly.inspect(target_id)
        plan = readonly.plan(target_id, status)
        transactions = _PreparationTransactions()
        materials = _PreparationMaterials()

        class _CaptureStore:
            def load(self, _change_id: str) -> MaintenanceChange:
                raise AssertionError("捕获阶段不应读取事务材料")

        adapter = WindowsAgentMaintenanceAdapter(readonly, _Materials(), _CaptureStore())
        service = MaintenancePreparationService(transactions, lambda _store: materials, ConfirmationTokenService())
        prepared = service.prepare(plan, status, adapter)
        return readonly, plan, status, adapter, prepared, materials

    def test_preparation_materializes_codex_and_claude_with_third_party_content(self) -> None:
        """真实 Agent 材料化只校验受管指纹，同时保留第三方正文。"""
        for target_id in ("agent.codex", "agent.claude-code"):
            with self.subTest(target_id=target_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                readonly, _plan, status, _adapter, prepared, materials = self._prepare_agent_fixture(root, target_id)
                self.assertEqual(getattr(status.status, "value", status.status), "drifted")
                self.assertEqual(prepared.change.status, "prepared")
                self.assertEqual(len(materials.calls), 1)
                leaves = materials.calls[0][2]
                self.assertIn(b"private instructions", leaves[f"{target_id}.root_instructions"].expected_bytes)
                if target_id == "agent.codex":
                    self.assertIn(b'private-model', leaves[f"{target_id}.mcp"].expected_bytes)
                    self.assertIn(b"third-party", leaves[f"{target_id}.hooks"].expected_bytes)
                else:
                    parsed = json.loads(leaves[f"{target_id}.mcp"].expected_bytes.decode("utf-8"))
                    self.assertEqual(parsed["mcpServers"]["other"], {"command": "third-party"})
                    self.assertEqual(parsed["private"], "保留")
                    self.assertIn(b"third-party", leaves[f"{target_id}.hooks"].expected_bytes)
                final_status = readonly.inspect(target_id)
                self.assertEqual(getattr(final_status.status, "value", final_status.status), "drifted")

    def test_preparation_rejects_managed_material_tampering_after_capture(self) -> None:
        """受管正文被篡改时，即使整文件摘要同步更新也不得生成 prepared。"""
        for target_id in ("agent.codex", "agent.claude-code"):
            with self.subTest(target_id=target_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write_agent_material_fixture(root, target_id)
                readonly = self._readonly(root)
                status = readonly.inspect(target_id)
                plan = readonly.plan(target_id, status)

                class _CaptureStore:
                    def load(self, _change_id: str) -> MaintenanceChange:
                        raise AssertionError("捕获阶段不应读取事务材料")

                adapter = WindowsAgentMaintenanceAdapter(readonly, _Materials(), _CaptureStore())
                fresh, captured, _ = adapter.capture_agent(plan)
                mcp_id = f"{target_id}.mcp"
                leaf = captured[mcp_id]
                needle = b'command = "pwsh"' if target_id == "agent.codex" else b'"command": "pwsh"'
                tampered = leaf.expected_bytes.replace(needle, b'command = "tampered"' if target_id == "agent.codex" else b'"command": "tampered"')
                captured[mcp_id] = replace(leaf, expected_bytes=tampered, expected_hash=hashlib.sha256(tampered).hexdigest())

                class _CapturedProvider:
                    def capture(self, _plan):
                        return fresh, captured, {}

                    def managed_fingerprint_part(self, verify_target_id, leaf_id, raw):
                        return adapter.managed_fingerprint_part(verify_target_id, leaf_id, raw)

                transactions = _PreparationTransactions()
                materials = _PreparationMaterials()
                service = MaintenancePreparationService(transactions, lambda _store: materials, ConfirmationTokenService())
                with self.assertRaises(MaintenancePreparationError):
                    service.prepare(plan, status, _CapturedProvider())
                self.assertEqual(transactions.items, [])

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
