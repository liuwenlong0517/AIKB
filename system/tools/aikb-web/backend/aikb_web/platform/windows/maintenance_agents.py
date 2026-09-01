"""Windows Codex/Claude Code 配置维护适配器。

本模块是阶段 4B Agent 目标的唯一写入边界：路径只能由已经固定用户边界的
``WindowsMaintenanceAdapter`` 解析，正文只能来自事务私有材料或与共享安装器
一致的受管模板。JSON 采用“精确优先、大小写兜底、大小写变体拒绝”的结构键
规则；更新时保留既有键名和非 AIKB 内容。所有写入均通过同目录临时文件原子
替换，测试可注入 probe，但本模块不会启动真实 Codex/Claude 进程。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from ...core.maintenance_changes import MaintenanceChange
from ...core.maintenance_materials import MaintenanceLeafMaterial, MaintenanceMaterialStore
from ...core.maintenance_recovery import (
    CurrentLeafObservation,
    RecoveryDecision,
    RecoveryStep,
)
from ...core.maintenance_targets import MAINTENANCE_LEAVES_BY_TARGET, MAINTENANCE_TARGET_REGISTRY, validate_logical_id
from ...platform.maintenance import MaintenanceStep, MaintenanceStepResult, MaintenanceVerification
from .maintenance_readonly import (
    _CODEX_MCP_END,
    _CODEX_MCP_START,
    _EVENTS,
    _HOOK_SCRIPT,
    _ROOT_END,
    _ROOT_START,
    _canonical_hook_handler,
    _expected_claude_mcp,
    _expected_codex_mcp,
    _expected_hooks,
    _expected_root,
    _find_json_key,
    _json_pairs_preserving_keys,
    _managed_block_pattern,
    _semantic_json,
    _sha256,
    _load_json_preserving_keys,
    WindowsMaintenanceAdapter,
)


class WindowsAgentMaintenanceError(RuntimeError):
    """Agent 配置边界、结构、原子写入或固定验证失败。"""


class _DefaultProbeProcessExecutor:
    """受限子进程执行器；仅供固定 probe 使用，不接受浏览器命令。"""

    def run(
        self,
        command: tuple[str, ...],
        input_data: bytes,
        timeout_seconds: float,
        output_budget: int,
        environment: Mapping[str, str],
    ) -> tuple[int, bytes, bytes]:
        """运行服务端生成的固定命令，并在返回前执行超时/输出预算检查。"""

        try:
            completed = subprocess.run(
                list(command),
                input=input_data,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=dict(environment),
            )
        except (OSError, subprocess.SubprocessError):
            return 1, b"", b""
        stdout = bytes(completed.stdout or b"")
        stderr = bytes(completed.stderr or b"")
        if len(stdout) > output_budget or len(stderr) > output_budget:
            return 1, b"", b""
        return int(completed.returncode), stdout, stderr


class WindowsAgentProbeRunner:
    """执行 Agent 目标的固定 MCP/生命周期/UTF-8 探针。

    所有命令、事件、参数和输出预算都在服务端固定；``process_executor`` 只为
    隔离测试注入。该 runner 只返回 ``bool``，原始 stdout/stderr 不会进入维护
    事务、任务或审计模型。
    """

    _TIMEOUT_SECONDS = 10.0
    _OUTPUT_BUDGET = 64 * 1024
    _HOOK_EVENTS = ("session-start", "pre-compact", "stop", "session-end")

    def __init__(self, readonly: WindowsMaintenanceAdapter, *, process_executor: object | None = None) -> None:
        """绑定固定只读适配器和可选隔离执行器，不在构造时启动进程。"""

        if not isinstance(readonly, WindowsMaintenanceAdapter):
            raise WindowsAgentMaintenanceError("只读适配器类型无效")
        if process_executor is not None and not callable(getattr(process_executor, "run", None)) and not callable(process_executor):
            raise WindowsAgentMaintenanceError("probe 执行器接口无效")
        self._readonly = readonly
        self._process = process_executor or _DefaultProbeProcessExecutor()

    def __call__(self, agent: str) -> bool:
        """运行固定 MCP、生命周期 hooks 和中文 UTF-8 往返检查。"""

        if agent not in {"codex", "claude-code"}:
            return False
        try:
            if not self._mcp_probe(agent):
                return False
            return all(self._hook_probe(agent, event) for event in self._HOOK_EVENTS)
        except Exception:
            # probe 是验证门禁，不得将命令、路径或底层异常传播到任务/审计。
            return False

    probe = __call__

    def _mcp_probe(self, agent: str) -> bool:
        """使用固定 stdio 请求验证 initialize/tools/list 和 UTF-8。"""

        request = (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "aikb-maintenance-probe-中文", "version": "1"},
            }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        code, stdout, _stderr = self._run(self._mcp_command(agent), self._json_lines(request))
        if code != 0:
            return False
        text = stdout.decode("utf-8")
        responses = [json.loads(line) for line in text.splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in responses if isinstance(item, dict)}
        initialized = by_id.get(1, {})
        listed = by_id.get(2, {})
        initialize_result = initialized.get("result")
        list_result = listed.get("result")
        return (
            isinstance(initialize_result, dict)
            and isinstance(list_result, dict)
            and isinstance(list_result.get("tools"), list)
            and bool(initialize_result.get("serverInfo"))
        )

    def _hook_probe(self, agent: str, event: str) -> bool:
        """通过与安装器一致的固定 PowerShell handler 入口验证生命周期和 UTF-8。"""

        payload = json.dumps({"session_id": "aikb-probe-中文"}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        code, stdout, _stderr = self._run(self._hook_command(agent, event), payload)
        # wrapper 的 stop 等故障必须 fail-open 为零退出；输出若存在必须是有效
        # UTF-8 JSON，防止活动代码页把中文反馈变成 U+FFFD。
        if code != 0:
            return False
        text = stdout.decode("utf-8")
        if "�" in text:
            return False
        if not text.strip():
            return True
        value = json.loads(text)
        return isinstance(value, (dict, list))

    def _run(self, command: tuple[str, ...], input_data: bytes) -> tuple[int, bytes, bytes]:
        """调用注入或默认执行器；统一输出预算和安全返回形状。"""

        environment = dict(os.environ)
        expected = getattr(self._readonly, "_expected_environment", {})
        for name in ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME"):
            value = expected.get(name)
            if not isinstance(value, str) or not value:
                return 1, b"", b""
            environment[name] = value
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        runner = self._process
        try:
            if callable(getattr(runner, "run", None)):
                result = runner.run(command, input_data, self._TIMEOUT_SECONDS, self._OUTPUT_BUDGET, environment)
            else:
                result = runner(command, input_data, self._TIMEOUT_SECONDS, self._OUTPUT_BUDGET, environment)
            if not isinstance(result, tuple) or len(result) != 3:
                return 1, b"", b""
            code, stdout, stderr = result
            if not isinstance(code, int) or not isinstance(stdout, (bytes, bytearray)) or not isinstance(stderr, (bytes, bytearray)):
                return 1, b"", b""
            if len(stdout) > self._OUTPUT_BUDGET or len(stderr) > self._OUTPUT_BUDGET:
                return 1, b"", b""
            return code, bytes(stdout), bytes(stderr)
        except Exception:
            return 1, b"", b""

    @staticmethod
    def _json_lines(items: tuple[dict[str, object], ...]) -> bytes:
        """编码固定 MCP 请求，显式使用 UTF-8 而非系统活动代码页。"""

        return b"".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for item in items)

    def _mcp_command(self, agent: str) -> tuple[str, ...]:
        """生成固定 AIKB MCP 启动命令；agent 仅来自静态集合。"""

        root = Path(self._readonly._expected_environment["AIKB_HOME"])
        script = root / "system" / "tools" / "aikb-mcp" / "scripts" / "aikb.ps1"
        return ("pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "serve", "--agent", agent)

    def _hook_command(self, agent: str, event: str) -> tuple[str, ...]:
        """生成与目标配置一致的固定 handler 命令，事件不可由浏览器传入。"""

        if event not in self._HOOK_EVENTS:
            raise WindowsAgentMaintenanceError("probe 事件未声明")
        root = Path(self._readonly._expected_environment["AIKB_HOME"])
        script = root / "system" / "adapters" / "shared" / "aikb-hook.ps1"
        shell = "powershell" if agent == "claude-code" else "pwsh"
        return (shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Agent", agent, "-Event", event)


def _json_bytes(value: object, *, newline: str = "\n") -> bytes:
    """按稳定、可读格式序列化已保留键名的 JSON 对象。"""

    text = json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
    return text.replace("\n", newline).encode("utf-8")


def _set_json_key(obj: dict[str, object], name: str, value: object) -> str:
    """使用精确优先/大小写兜底定位并更新，返回实际保留的键名。"""

    key = _find_json_key(obj, name)
    if key is None:
        key = name
    obj[key] = value
    return key


def _merge_owned_object(existing: object, expected: Mapping[str, object]) -> dict[str, object]:
    """更新 AIKB 自有对象并保留其键大小写；多余字段不属于用户其他配置。"""

    if existing is not None and not isinstance(existing, dict):
        raise WindowsAgentMaintenanceError("受管 JSON 对象类型无效")
    result = dict(existing) if isinstance(existing, dict) else {}
    for name, value in expected.items():
        old_key = _find_json_key(result, name)
        if isinstance(value, dict):
            old_value = result.get(old_key) if old_key is not None else None
            value = _merge_owned_object(old_value, value)
        _set_json_key(result, name, value)
    # AIKB 自有对象允许模板淘汰旧字段；嵌套非 AIKB 配置不在此对象边界内。
    expected_folded = {name.casefold() for name in expected}
    for key in tuple(result):
        if key.casefold() not in expected_folded:
            del result[key]
    return result


def _merge_root(raw: bytes | None) -> bytes:
    """替换唯一根指令区块，保留用户前后正文及其换行风格。"""

    text = (raw or b"").decode("utf-8-sig")
    pattern = _managed_block_pattern(_ROOT_START, _ROOT_END)
    matches = pattern.findall(text)
    if len(matches) > 1 or (not matches and (_ROOT_START in text or _ROOT_END in text)):
        raise WindowsAgentMaintenanceError("根指令受管标记不完整或重复")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _expected_root().decode("utf-8").replace("\n", newline).rstrip("\r\n")
    if matches:
        return pattern.sub(block + newline, text, count=1).encode("utf-8")
    # 与共享安装器兼容：清掉已知旧版单行入口，再追加当前受管区块。
    legacy = re.compile(r"(?m)^\s*每个新会话开始时，请读取并持续遵循\s+`[^`]*ENTRY_RULES\.md`。\s*\r?$")
    clean = legacy.sub("", text).rstrip("\r\n")
    output = (clean + newline + newline if clean else "") + block + newline
    return output.encode("utf-8")


def _merge_codex_mcp(raw: bytes | None) -> bytes:
    """替换 Codex 受管 TOML 块，拒绝任意大小写同名的非受管服务。"""

    text = (raw or b"").decode("utf-8-sig")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise WindowsAgentMaintenanceError("Codex TOML 无效") from error
    marker_pattern = _managed_block_pattern(_CODEX_MCP_START, _CODEX_MCP_END)
    matches = marker_pattern.findall(text)
    sections = re.findall(r"(?im)^\s*\[mcp_servers\.aikb\]\s*$", text)
    if len(matches) > 1:
        raise WindowsAgentMaintenanceError("Codex 受管 MCP 区块重复")
    # 未包裹标记的 exact/case-variant section 都是第三方冲突；受管块内部
    # 的 section 在这里允许恰好一个，并由 marker 保护。
    if sections and not matches:
        raise WindowsAgentMaintenanceError("Codex MCP 同名服务冲突")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _expected_codex_mcp().decode("utf-8").replace("\n", newline).rstrip("\r\n")
    if matches:
        return marker_pattern.sub(block + newline, text, count=1).encode("utf-8")
    clean = text.rstrip("\r\n")
    return ((clean + newline + newline if clean else "") + block + newline).encode("utf-8")


def _merge_claude_mcp(raw: bytes | None) -> bytes:
    """仅更新 Claude ``mcpServers.aikb``，保留外部服务、根键和键大小写。"""

    try:
        config = _load_json_preserving_keys(raw or b"{}")
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise WindowsAgentMaintenanceError("Claude MCP JSON 无效") from error
    if not isinstance(config, dict):
        raise WindowsAgentMaintenanceError("Claude MCP 根必须是对象")
    servers_key = _find_json_key(config, "mcpServers")
    if servers_key is None:
        servers: dict[str, object] = {}
        _set_json_key(config, "mcpServers", servers)
    else:
        servers = config[servers_key]
        if not isinstance(servers, dict):
            raise WindowsAgentMaintenanceError("Claude mcpServers 必须是对象")
    aikb_key = _find_json_key(servers, "aikb")
    existing = servers.get(aikb_key) if aikb_key is not None else None
    if existing is not None:
        env_key = _find_json_key(existing, "env") if isinstance(existing, dict) else None
        env = existing.get(env_key) if env_key is not None else None
        marker_key = _find_json_key(env, "AIKB_MANAGED") if isinstance(env, dict) else None
        if marker_key is None or env.get(marker_key) != "1":
            raise WindowsAgentMaintenanceError("Claude MCP 同名服务冲突")
    expected = _expected_claude_mcp()
    # 结构、键名歧义和 AIKB_MANAGED 标记已经在上面完成校验；语义完全一致时
    # 原样保留整个文件，避免无关第三方配置、空白和换行被不必要地重写。
    if raw is not None and isinstance(existing, dict) and _semantic_json(existing) == _semantic_json(expected):
        return raw
    managed = _merge_owned_object(existing, expected)
    if aikb_key is None:
        servers["aikb"] = managed
    else:
        servers[aikb_key] = managed
    newline = "\r\n" if b"\r\n" in (raw or b"") else "\n"
    return _json_bytes(config, newline=newline)


def _merge_hooks(raw: bytes | None, agent: str) -> bytes:
    """按事件移除旧 AIKB handlers，追加唯一标准组并保留全部用户 handlers。"""

    try:
        config = _load_json_preserving_keys(raw or b"{}")
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise WindowsAgentMaintenanceError("hooks JSON 无效") from error
    if not isinstance(config, dict):
        raise WindowsAgentMaintenanceError("hooks 根必须是对象")
    hooks_key = _find_json_key(config, "hooks")
    if hooks_key is None:
        hooks: dict[str, object] = {}
        _set_json_key(config, "hooks", hooks)
    else:
        hooks = config[hooks_key]
        if not isinstance(hooks, dict):
            raise WindowsAgentMaintenanceError("hooks 必须是对象")
    expected = _expected_hooks(agent)
    for item in expected:
        event = str(item["event"])
        event_key = _find_json_key(hooks, event)
        groups_value = hooks.get(event_key, []) if event_key is not None else []
        if not isinstance(groups_value, list):
            raise WindowsAgentMaintenanceError("hook event 必须是数组")
        kept: list[dict[str, object]] = []
        for group in groups_value:
            if not isinstance(group, dict):
                raise WindowsAgentMaintenanceError("hook group 必须是对象")
            handlers_key = _find_json_key(group, "hooks")
            if handlers_key is None:
                kept.append(group)
                continue
            handlers = group[handlers_key]
            if not isinstance(handlers, list):
                raise WindowsAgentMaintenanceError("hook handlers 必须是数组")
            retained: list[object] = []
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise WindowsAgentMaintenanceError("hook handler 必须是对象")
                command_key = _find_json_key(handler, "command")
                command = handler.get(command_key) if command_key is not None else None
                if not (isinstance(command, str) and _HOOK_SCRIPT in command):
                    retained.append(handler)
            if retained:
                group[handlers_key] = retained
                kept.append(group)
            elif not handlers:
                kept.append(group)
        new_group: dict[str, object] = {"hooks": [item["handler"]]}
        if item["matcher"] is not None:
            new_group = {"matcher": item["matcher"], "hooks": [item["handler"]]}
        kept.append(new_group)
        if event_key is None:
            hooks[event] = kept
        else:
            hooks[event_key] = kept
    newline = "\r\n" if b"\r\n" in (raw or b"") else "\n"
    return _json_bytes(config, newline=newline)


class WindowsAgentMaintenanceAdapter:
    """在固定用户配置根内执行 Codex 或 Claude Code 的三叶子维护事务。

    ``capture_agent`` 只读取 fixture 并生成私有材料；``apply_step``、``verify``
    和 ``rollback_step`` 只消费服务端事务与材料。``probe_runner`` 是隔离测试或
    上层验收注入的固定探针，默认不启动进程，避免 Web 请求间接控制 Agent。
    """

    def __init__(
        self,
        readonly: WindowsMaintenanceAdapter,
        materials: MaintenanceMaterialStore,
        transaction_store: object | None = None,
        *,
        probe_runner: Callable[[str], bool] | None = None,
    ) -> None:
        """绑定只读路径和私有材料；构造阶段不读取配置或创建文件。"""

        if not isinstance(readonly, WindowsMaintenanceAdapter):
            raise WindowsAgentMaintenanceError("只读适配器类型无效")
        if not callable(getattr(materials, "load", None)):
            raise WindowsAgentMaintenanceError("维护材料接口无效")
        if probe_runner is not None and not callable(probe_runner):
            raise WindowsAgentMaintenanceError("固定 probe 接口无效")
        self._readonly = readonly
        self._materials = materials
        # 材料存储通常由维护应用共享，但不要求其暴露私有事务 Store；显式
        # 注入优先，兼容测试中把 store 挂到材料桩上的最小 duck typing。
        self._transactions = transaction_store or getattr(materials, "_transactions", None)
        if self._transactions is None or not callable(getattr(self._transactions, "load", None)):
            raise WindowsAgentMaintenanceError("事务事实源接口无效")
        self._probe = probe_runner

    def capture_agent(self, plan: object) -> tuple[object, Mapping[str, MaintenanceLeafMaterial], Mapping[str, object]]:
        """按计划重新读取 Agent，生成与事务绑定的三叶子私有材料。"""

        target_id = getattr(plan, "target_id", None)
        if target_id not in {"agent.codex", "agent.claude-code"}:
            raise WindowsAgentMaintenanceError("仅支持 Codex 或 Claude Code 目标")
        fresh = self._readonly.inspect(target_id)
        if getattr(fresh, "base_fingerprint", None) != getattr(plan, "before_fingerprint", None):
            raise WindowsAgentMaintenanceError("Agent 配置在预览后发生变化")
        if fresh.status in {"conflict", "invalid", "unsupported"}:
            raise WindowsAgentMaintenanceError("Agent 配置不可自动修复")
        agent = "codex" if target_id == "agent.codex" else "claude-code"
        leaves: dict[str, MaintenanceLeafMaterial] = {}
        for leaf_id in MAINTENANCE_LEAVES_BY_TARGET[target_id]:
            path = self._readonly._leaf_path(agent, leaf_id)
            raw = self._read_current(path)
            expected = self._merge_leaf(agent, leaf_id, raw)
            mode = stat.S_IMODE(path.stat().st_mode) if raw is not None else None
            leaves[leaf_id] = MaintenanceLeafMaterial(
                leaf_id=leaf_id,
                existence="present" if raw is not None else "missing",
                before_hash=None if raw is None else _sha256(raw),
                expected_hash=_sha256(expected),
                file_mode=mode,
                before_bytes=raw,
                expected_bytes=expected,
            )
        return fresh, leaves, {}

    # 便于上层准备服务按 Agent 目标使用统一 provider 名称。
    capture = capture_agent
    capture_configuration = capture_agent

    def managed_fingerprint_part(self, target_id: str, leaf_id: str, raw: bytes | None) -> str:
        """委托只读适配器按受管结构解析材料，整文件摘要仍由执行器保留。"""
        return self._readonly.managed_fingerprint_part(target_id, leaf_id, raw)

    def apply_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """执行 preflight/backup 或单个固定配置叶子写入。"""

        transaction = self._transaction(change_id, target_id)
        if not isinstance(step, MaintenanceStep) or step.step_id not in transaction.step_summary:
            raise WindowsAgentMaintenanceError("维护步骤无效")
        manifest = self._materials.load(change_id)
        self._validate_manifest(transaction, manifest)
        if step.step_id in {"preflight", "backup"}:
            for leaf in manifest.leaves:
                self._assert_before(leaf.leaf_id, target_id, leaf.before_hash, leaf.existence)
        elif step.step_id in {"write_root_instructions", "write_mcp", "write_hooks"}:
            for leaf_id in self._write_leaves(target_id, step.step_id):
                leaf = self._leaf(manifest, leaf_id)
                self._write_leaf(target_id, leaf)
        else:
            raise WindowsAgentMaintenanceError("当前适配器不支持该步骤")
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "applied")

    def verify(self, change_id: str, target_id: str) -> MaintenanceVerification:
        """验证三叶子受管结构和可选固定 probe；成功后提示人工重启 Agent。"""

        transaction = self._transaction(change_id, target_id)
        manifest = self._materials.load(change_id)
        self._validate_manifest(transaction, manifest)
        for leaf in manifest.leaves:
            current = self._read_current(self._readonly._leaf_path(self._agent(target_id), leaf.leaf_id))
            if current is None or _sha256(current) != leaf.expected_hash:
                raise WindowsAgentMaintenanceError("Agent 期望配置未满足")
        inspection = self._readonly.inspect(target_id)
        if inspection.status != "ready" or inspection.base_fingerprint != transaction.after_fingerprint:
            raise WindowsAgentMaintenanceError("Agent 受管结构验证失败")
        if self._probe is not None and not self._probe(self._agent(target_id)):
            raise WindowsAgentMaintenanceError("Agent 固定 probe 失败")
        return MaintenanceVerification(change_id, target_id, "restart_required", True, transaction.after_fingerprint)

    def rollback_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """按材料恢复一个写步骤涉及的叶子，第三方改动绝不覆盖。"""

        transaction = self._transaction(change_id, target_id)
        if not isinstance(step, MaintenanceStep) or step.step_id not in {"write_root_instructions", "write_mcp", "write_hooks"}:
            raise WindowsAgentMaintenanceError("回滚步骤无效")
        manifest = self._materials.load(change_id)
        self._validate_manifest(transaction, manifest)
        for leaf_id in reversed(self._write_leaves(target_id, step.step_id)):
            leaf = self._leaf(manifest, leaf_id)
            path = self._readonly._leaf_path(self._agent(target_id), leaf_id)
            current = self._read_current(path)
            if current is None and leaf.existence == "missing":
                continue
            if current is None or _sha256(current) != leaf.expected_hash:
                raise WindowsAgentMaintenanceError("回滚目标已被第三方修改")
            if leaf.existence == "missing":
                self._unlink_leaf(path)
            else:
                assert leaf.before_bytes is not None
                self._atomic_write(path, leaf.before_bytes, leaf.file_mode)
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "rolled_back")

    def observe_leaf(self, change_id: str, target_id: str, leaf_id: str) -> CurrentLeafObservation:
        """为启动恢复提供固定 Agent 叶子的存在与整文件摘要。"""

        self._validate_ids(change_id, target_id, leaf_id)
        path = self._readonly._leaf_path(self._agent(target_id), leaf_id)
        current = self._read_current(path)
        return CurrentLeafObservation("missing", None) if current is None else CurrentLeafObservation("present", _sha256(current))

    def recover_step(self, change_id: str, target_id: str, step: RecoveryStep) -> MaintenanceStepResult:
        """按启动恢复决定逐叶子恢复/移除已创建文件。"""

        self._validate_ids(change_id, target_id, None)
        if not isinstance(step, RecoveryStep) or step.step_id not in {"write_root_instructions", "write_mcp", "write_hooks"}:
            raise WindowsAgentMaintenanceError("恢复步骤无效")
        for decision in step.leaf_decisions:
            if decision.decision is RecoveryDecision.NOOP:
                continue
            if decision.decision not in {RecoveryDecision.RESTORE_BEFORE, RecoveryDecision.REMOVE_CREATED}:
                raise WindowsAgentMaintenanceError("恢复决定要求人工处理")
        # RecoveryStep 已由核心按 expected 摘要门禁验证；这里复用 rollback，
        # 逐叶子决定仍需再次确保材料和当前摘要绑定。
        self.rollback_step(change_id, target_id, MaintenanceStep(step.step_id))
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "rolled_back")

    def fixed_probe_spec(self, target_id: str) -> tuple[str, ...]:
        """返回固定 probe 语义步骤，不返回可由浏览器控制的命令文本。"""

        self._validate_ids("probe-id", target_id, None)
        return ("mcp_initialize", "mcp_tools_list", "lifecycle_handler", "utf8_roundtrip")

    def _transaction(self, change_id: str, target_id: str) -> MaintenanceChange:
        """读取并校验仅绑定当前 Agent 的事务。"""

        try:
            validate_logical_id(change_id, "change_id")
            target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
            transaction = self._transactions.load(change_id)
        except Exception as error:
            raise WindowsAgentMaintenanceError("事务绑定无效") from error
        if not isinstance(transaction, MaintenanceChange) or transaction.change_id != change_id or transaction.target_id != target_id:
            raise WindowsAgentMaintenanceError("事务绑定无效")
        return transaction

    def _validate_manifest(self, transaction: MaintenanceChange, manifest: object) -> None:
        """确认材料叶子顺序、存在语义和摘要与事务事实完全一致。"""

        if getattr(manifest, "change_id", None) != transaction.change_id or getattr(manifest, "target_id", None) != transaction.target_id:
            raise WindowsAgentMaintenanceError("材料目标不匹配")
        if tuple(item.leaf_id for item in manifest.leaves) != tuple(item.leaf_id for item in transaction.leaf_states):
            raise WindowsAgentMaintenanceError("材料叶子不完整")
        for item, state in zip(manifest.leaves, transaction.leaf_states):
            if (item.existence, item.before_hash, item.expected_hash) != (state.existence, state.before_hash, state.expected_hash):
                raise WindowsAgentMaintenanceError("材料摘要不匹配")
        if getattr(manifest, "environments", ()):
            raise WindowsAgentMaintenanceError("Agent 材料不得携带环境值")

    @staticmethod
    def _leaf(manifest: object, leaf_id: str) -> MaintenanceLeafMaterial:
        """从已校验 manifest 读取固定叶子。"""

        for leaf in manifest.leaves:
            if leaf.leaf_id == leaf_id:
                return leaf
        raise WindowsAgentMaintenanceError("材料叶子不存在")

    def _write_leaf(self, target_id: str, leaf: MaintenanceLeafMaterial) -> None:
        """写入 expected 字节；目标当前必须仍为 before 或 expected。"""

        path = self._readonly._leaf_path(self._agent(target_id), leaf.leaf_id)
        current = self._read_current(path)
        if current is not None and _sha256(current) == leaf.expected_hash:
            return
        if (current is None) != (leaf.existence == "missing") or (current is not None and _sha256(current) != leaf.before_hash):
            raise WindowsAgentMaintenanceError("Agent 配置当前状态冲突")
        self._atomic_write(path, leaf.expected_bytes, leaf.file_mode)
        after = self._read_current(path)
        if after is None or _sha256(after) != leaf.expected_hash:
            raise WindowsAgentMaintenanceError("Agent 原子写入验证失败")

    def _assert_before(self, leaf_id: str, target_id: str, before_hash: str | None, existence: str) -> None:
        """预检每个叶子的存在语义和事务前摘要。"""

        current = self._read_current(self._readonly._leaf_path(self._agent(target_id), leaf_id))
        if existence == "missing":
            if current is not None:
                raise WindowsAgentMaintenanceError("Agent 事务前状态已变化")
        elif current is None or _sha256(current) != before_hash:
            raise WindowsAgentMaintenanceError("Agent 事务前摘要已变化")

    def _merge_leaf(self, agent: str, leaf_id: str, raw: bytes | None) -> bytes:
        """按固定叶子选择对应模板合并函数。"""

        if leaf_id.endswith("root_instructions"):
            return _merge_root(raw)
        if leaf_id.endswith("mcp"):
            return _merge_codex_mcp(raw) if agent == "codex" else _merge_claude_mcp(raw)
        return _merge_hooks(raw, agent)

    @staticmethod
    def _write_leaves(target_id: str, step_id: str) -> tuple[str, ...]:
        """从静态目标映射取得一个固定步骤的逻辑叶子。"""

        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        mapping = {
            "write_root_instructions": (target.logical_leaves[0],),
            "write_mcp": (target.logical_leaves[1],),
            "write_hooks": (target.logical_leaves[2],),
        }
        return mapping[step_id]

    @staticmethod
    def _agent(target_id: str) -> str:
        if target_id == "agent.codex":
            return "codex"
        if target_id == "agent.claude-code":
            return "claude-code"
        raise WindowsAgentMaintenanceError("当前适配器仅支持 Agent 目标")

    def _read_current(self, path: Path) -> bytes | None:
        """安全读取固定叶子；缺失父目录表示逻辑 missing。"""

        try:
            if self._readonly._has_reparse_component(path):
                raise WindowsAgentMaintenanceError("Agent 配置包含重解析点")
            if not path.exists():
                return None
            if not path.is_file():
                raise WindowsAgentMaintenanceError("Agent 配置叶子类型无效")
            return path.read_bytes()
        except WindowsAgentMaintenanceError:
            raise
        except (OSError, ValueError) as error:
            raise WindowsAgentMaintenanceError("Agent 配置读取失败") from error

    def _atomic_write(self, path: Path, data: bytes, mode: int | None) -> None:
        """同目录临时文件 + flush/fsync + os.replace，拒绝链接和越界父目录。"""

        parent = path.parent
        try:
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            if self._readonly._has_reparse_component(path) or path.is_symlink() or (path.exists() and not path.is_file()):
                raise WindowsAgentMaintenanceError("Agent 配置边界无效")
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if mode is not None:
                    os.chmod(temporary, mode)
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except WindowsAgentMaintenanceError:
            raise
        except (OSError, ValueError) as error:
            raise WindowsAgentMaintenanceError("Agent 配置原子写入失败") from error

    def _unlink_leaf(self, path: Path) -> None:
        """仅删除事务新建且仍等于 expected 的固定叶子。"""

        if self._readonly._has_reparse_component(path) or path.is_symlink():
            raise WindowsAgentMaintenanceError("Agent 删除边界无效")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise WindowsAgentMaintenanceError("Agent 叶子回滚失败") from error

    @staticmethod
    def _validate_ids(change_id: str, target_id: str, leaf_id: str | None) -> None:
        """仅允许固定逻辑标识，拒绝路径样式输入。"""

        try:
            validate_logical_id(change_id, "change_id")
            target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        except Exception as error:
            raise WindowsAgentMaintenanceError("逻辑标识无效") from error
        if target_id not in {"agent.codex", "agent.claude-code"}:
            raise WindowsAgentMaintenanceError("当前适配器仅支持 Agent 目标")
        if leaf_id is not None and leaf_id not in MAINTENANCE_LEAVES_BY_TARGET[target_id]:
            raise WindowsAgentMaintenanceError("维护叶子无效")


__all__ = [
    "WindowsAgentMaintenanceAdapter",
    "WindowsAgentMaintenanceError",
    "WindowsAgentProbeRunner",
]
