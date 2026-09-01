"""Windows 阶段 4B 波次 1 的纯读取维护适配器。

适配器只读取调用方注入的隔离配置根和环境快照，按安装器已经冻结的受管标记
识别三个固定目标，生成逻辑状态与受管片段摘要。构造函数接受路径仅是为了让
测试注入临时 fixture；这些路径永远不会进入状态、规划、异常公开文本或 API
投影。波次 1 不创建目录、临时文件、事务、备份、审计事件或子进程。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from ...core.maintenance_targets import (
    MAINTENANCE_LEAVES_BY_TARGET,
    MaintenanceManagedDifference,
    MaintenanceStatus,
    MaintenanceTargetError,
    MaintenanceTargetStatus,
)
from ...core.maintenance_materials import (
    MaintenanceEnvironmentMaterial,
    MaintenanceLeafMaterial,
)
from ..maintenance import MaintenancePlan, MaintenanceStep


_ROOT_START = "<!-- >>> AIKB managed root instruction >>> -->"
_ROOT_END = "<!-- <<< AIKB managed root instruction <<< -->"
_CODEX_MCP_START = "# >>> AIKB managed MCP >>>"
_CODEX_MCP_END = "# <<< AIKB managed MCP <<<"
_HOOK_SCRIPT = "aikb-hook.ps1"
_EXPECTED_INSTRUCTION = (
    "每个新会话开始时，请从 Windows 用户环境变量 `AIKB_HOME` 获取 AIKB 根目录，并读取和持续遵循其中的 `ENTRY_RULES.md`。"
)
_EVENTS = ("SessionStart", "PreCompact", "Stop", "SessionEnd")
_ENV_KEYS = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = 0x400


class _JsonStructureError(ValueError):
    """JSON 结构键重复或根节点类型不符合配置契约。"""


def _json_pairs_preserving_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """保留原始键名，并拒绝精确或仅大小写不同的重复结构键。

    PowerShell ``AdapterConfig.psm1`` 的结构键规则是配置写入的共同契约：
    精确键优先、大小写不敏感兜底，但同一对象出现大小写变体时不能猜测。
    Python 的默认 ``json.loads`` 会静默覆盖重复键，因此必须在解析阶段拦截。
    """

    result: dict[str, object] = {}
    # JSON 标准层只拒绝同一对象内的精确重复键；仅大小写不同的键必须先
    # 保留，直到调用方按某个受管结构字段查询时再由 _find_json_key 判歧义。
    exact: set[str] = set()
    for key, value in pairs:
        if not isinstance(key, str):
            raise _JsonStructureError("JSON 结构键必须是字符串")
        if key in exact:
            raise _JsonStructureError("JSON 对象包含重复结构键")
        exact.add(key)
        result[key] = value
    return result


def _load_json_preserving_keys(raw: bytes) -> object:
    """按共享适配器规则解析 JSON，既保留键名又阻断大小写歧义。"""

    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_pairs_preserving_keys)


def _find_json_key(value: object, name: str) -> str | None:
    """精确匹配优先、大小写不敏感兜底定位一个结构键。"""

    if not isinstance(value, dict):
        return None
    if name in value:
        # 与 PowerShell Find-JsonPropertyName 一致：即使存在精确键，只要
        # 同一对象还有大小写变体，也不能猜测调用方意图。
        matches = [key for key in value if key.casefold() == name.casefold()]
        if len(matches) > 1:
            raise _JsonStructureError("JSON 对象包含多个仅大小写不同的结构键")
        return name
    folded = name.casefold()
    matches = [key for key in value if key.casefold() == folded]
    if len(matches) > 1:
        raise _JsonStructureError("JSON 对象包含多个仅大小写不同的结构键")
    return matches[0] if matches else None


def _canonical_hook_handler(value: dict[str, object]) -> dict[str, object]:
    """提取 AIKB handler 的语义字段，比较时不因键大小写产生伪漂移。"""

    result: dict[str, object] = {}
    for name in ("type", "command", "timeout", "shell"):
        key = _find_json_key(value, name)
        if key is not None:
            result[name] = value[key]
    return result


def _semantic_json(value: object) -> object:
    """把结构键按大小写折叠供受管对象比较，值和数组顺序保持不变。"""

    if isinstance(value, dict):
        return {key.casefold(): _semantic_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic_json(item) for item in value]
    return value


def _sha256(value: bytes) -> str:
    """只返回摘要，适配器不把输入正文交给公开模型。"""

    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    """以稳定 UTF-8 形式序列化受管片段，供摘要比较而非正文展示。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _expected_root() -> bytes:
    """生成与 install-root-instructions.ps1 相同的受管根指令区块。"""

    return f"{_ROOT_START}\n{_EXPECTED_INSTRUCTION}\n{_ROOT_END}\n".encode("utf-8")


def _expected_codex_mcp() -> bytes:
    """生成 Codex 受管 TOML 区块；只引用环境变量，不嵌入仓库绝对路径。"""

    return (
        f"{_CODEX_MCP_START}\n"
        "[mcp_servers.aikb]\n"
        "command = \"pwsh\"\n"
        "args = [\"-NoProfile\", \"-ExecutionPolicy\", \"Bypass\", \"-Command\", \"& (Join-Path $env:AIKB_HOME 'system/tools/aikb-mcp/scripts/aikb.ps1') serve --agent codex\"]\n"
        "env_vars = [\"AIKB_HOME\", \"AIKB_KNOWLEDGE_HOME\"]\n"
        "startup_timeout_sec = 10\n"
        "tool_timeout_sec = 60\n"
        "enabled = true\n"
        f"{_CODEX_MCP_END}\n"
    ).encode("utf-8")


def _expected_hooks(agent: str) -> list[dict[str, object]]:
    """返回只含 AIKB handler 的标准结构，不包含用户其他 hooks。"""

    result: list[dict[str, object]] = []
    for event in _EVENTS:
        matcher = "startup|resume|clear|compact" if agent == "claude-code" and event == "SessionStart" else (
            "startup|resume|compact" if event == "SessionStart" else "manual|auto" if event == "PreCompact" else None
        )
        timeout = 3 if event == "SessionEnd" else 10
        event_argument = {
            "SessionStart": "session-start",
            "PreCompact": "pre-compact",
            "Stop": "stop",
            "SessionEnd": "session-end",
        }[event]
        command = f"& (Join-Path $env:AIKB_HOME 'system/adapters/shared/aikb-hook.ps1') -Agent {agent} -Event {event_argument}"
        if agent == "codex":
            command = f'pwsh -NoProfile -ExecutionPolicy Bypass -Command "{command}"'
        handler: dict[str, object] = {"type": "command", "command": command, "timeout": timeout}
        if agent == "claude-code":
            handler["shell"] = "powershell"
        result.append({"event": event, "matcher": matcher, "handler": handler})
    return result


def _expected_claude_mcp() -> dict[str, object]:
    """返回 Claude Code 受管 MCP 对象的固定结构，不含物理仓库路径。"""

    return {
        "type": "stdio",
        "command": "pwsh",
        "args": [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "& (Join-Path $env:AIKB_HOME 'system/tools/aikb-mcp/scripts/aikb.ps1') serve --agent claude-code",
        ],
        "env": {"AIKB_MANAGED": "1"},
    }


def _read_user_environment() -> Mapping[str, str | None]:
    """惰性读取 HKCU\\Environment 的两个固定 AIKB 值，不枚举或写入注册表。"""

    if os.name != "nt":
        raise OSError("Windows user environment is unavailable")
    import winreg

    values: dict[str, str | None] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        for name in _ENV_KEYS:
            try:
                value, _value_type = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                value = None
            if value is not None and not isinstance(value, str):
                raise TypeError("AIKB user environment value must be a string")
            values[name] = value
    return values


def _managed_block_pattern(start: str, end: str) -> re.Pattern[str]:
    """构造与安装器一致的整行受管标记匹配器，拒绝嵌入正文的伪标记。"""

    return re.compile(r"(?ms)^" + re.escape(start) + r".*?^" + re.escape(end) + r"\r?\n?")


class WindowsMaintenanceAdapter:
    """仅读取 Windows 用户配置的维护适配器。

    测试通过构造函数显式注入隔离 fixture；生产入口只允许 ``from_settings``
    按当前用户固定边界组装惰性读取器。后续写入波次应另建事务执行器，不能
    把本类扩展成写入器。
    """

    def __init__(
        self,
        *,
        fixture_root: Path | None = None,
        codex_home: Path | None = None,
        claude_home: Path | None = None,
        claude_user_config: Path | None = None,
        environment: Mapping[str, str | None] | None = None,
        environment_reader: Callable[[], Mapping[str, str | None]] | None = None,
        expected_environment: Mapping[str, str] | None = None,
    ) -> None:
        """注入隔离路径和环境快照；初始化不访问文件系统也不修改任何状态。"""

        if fixture_root is not None:
            root = Path(fixture_root)
            codex_home = codex_home or root / "codex"
            claude_home = claude_home or root / "claude"
            claude_user_config = claude_user_config or root / ".claude.json"
        if codex_home is None or claude_home is None or claude_user_config is None:
            raise MaintenanceTargetError("必须注入隔离的配置 fixture")
        if expected_environment is None or set(expected_environment) != set(_ENV_KEYS):
            raise MaintenanceTargetError("必须注入两个固定 AIKB 环境期望值")
        current_environment = dict(environment or {})
        if set(current_environment) - set(_ENV_KEYS):
            raise MaintenanceTargetError("环境快照包含未声明变量")
        if any(value is not None and not isinstance(value, str) for value in current_environment.values()):
            raise MaintenanceTargetError("环境快照值必须是字符串或缺失")
        if any(not isinstance(value, str) for value in expected_environment.values()):
            raise MaintenanceTargetError("环境期望值必须是字符串")
        if environment is not None and environment_reader is not None:
            raise MaintenanceTargetError("环境只能注入 mapping 或惰性 reader 之一")
        if environment is None and environment_reader is None:
            raise MaintenanceTargetError("必须注入环境 mapping 或惰性 reader")
        if environment_reader is not None and not callable(environment_reader):
            raise MaintenanceTargetError("环境 reader 必须可调用")
        self._codex_home = Path(codex_home)
        self._claude_home = Path(claude_home)
        self._claude_user_config = Path(claude_user_config)
        self._environment = current_environment if environment is not None else None
        self._environment_reader = environment_reader
        self._expected_environment = dict(expected_environment)

    @classmethod
    def from_settings(cls, settings: object) -> "WindowsMaintenanceAdapter":
        """从已验证 Web settings 创建生产只读适配器，但不读取或创建配置文件。

        Codex 仅接受当前用户目录内的 ``CODEX_HOME``；未设置时使用固定的
        ``%USERPROFILE%\\.codex``。Claude Code 使用当前用户固定的 ``.claude``
        和 ``.claude.json``。真正的存在性、普通文件和重解析点检查延迟到
        ``inspect``，因此应用导入/构造阶段不会产生扫描或运行材料。
        """

        repo_root = getattr(settings, "repo_root", None)
        knowledge_root = getattr(settings, "knowledge_root", None)
        if not isinstance(repo_root, Path) or not isinstance(knowledge_root, Path):
            raise MaintenanceTargetError("Web settings 缺少已验证双仓根")
        # 只做词法边界检查；构造阶段不 resolve、stat 或读取用户配置，重解析
        # 点和真实文件类型统一延迟到 inspect 的逐段检查。
        user_root = Path.home()
        configured_codex = os.environ.get("CODEX_HOME")
        codex_home = Path(configured_codex).expanduser() if configured_codex else user_root / ".codex"
        try:
            codex_home.absolute().relative_to(user_root)
        except ValueError as error:
            raise MaintenanceTargetError("CODEX_HOME 超出当前 Windows 用户边界") from error
        claude_home = user_root / ".claude"
        return cls(
            codex_home=codex_home,
            claude_home=claude_home,
            claude_user_config=user_root / ".claude.json",
            environment_reader=_read_user_environment,
            expected_environment={
                "AIKB_HOME": str(repo_root),
                "AIKB_KNOWLEDGE_HOME": str(knowledge_root),
            },
        )

    def inspect(self, target_id: str) -> MaintenanceTargetStatus:
        """只读检查固定目标并返回受管叶子摘要；绝不产生任何文件或进程副作用。"""

        target = self._target(target_id)
        observations = self._inspect_target(target_id)
        status = self._combine_status(observations)
        differences = tuple(item["difference"] for item in observations)
        before = self._target_fingerprint(observations)
        return MaintenanceTargetStatus(
            target_id=target.target_id,
            status=status,
            logical_leaves=target.logical_leaves,
            steps=target.steps,
            reason_code=self._reason_for(status),
            base_fingerprint=before,
            differences=differences,
        )

    def plan(self, target_id: str, inspection: MaintenanceTargetStatus | None = None) -> MaintenancePlan:
        """在再次只读检查基础上生成完整安全规划和受管片段结构化差异。"""

        target = self._target(target_id)
        current = self.inspect(target_id)
        if inspection is not None and inspection.public_dict() != current.public_dict():
            raise MaintenanceTargetError("预览状态已变化，请重新检查")
        observations = self._inspect_target(target_id)
        differences = tuple(item["difference"] for item in observations)
        before = self._target_fingerprint(observations)
        expected = self._expected_fingerprint(target_id)
        digest = _sha256(_canonical_json([item.public_dict() for item in differences]))
        summary_code = "no_change" if current.status == MaintenanceStatus.READY.value else (
            "blocked_conflict" if current.status == MaintenanceStatus.CONFLICT.value else
            "invalid_target" if current.status == MaintenanceStatus.INVALID.value else
            "unsupported_platform" if current.status == MaintenanceStatus.UNSUPPORTED.value else
            "repair_available"
        )
        return MaintenancePlan(
            target_id=target.target_id,
            steps=tuple(MaintenanceStep(step) for step in target.steps),
            logical_leaves=target.logical_leaves,
            before_fingerprint=before,
            after_fingerprint=expected,
            preview_digest=digest,
            differences=differences,
            summary_code=summary_code,
        )

    def capture_environment(
        self, plan: MaintenancePlan,
    ) -> tuple[MaintenanceTargetStatus, Mapping[str, MaintenanceLeafMaterial], Mapping[str, MaintenanceEnvironmentMaterial]]:
        """捕获 environment 事务所需的服务端材料，不接受浏览器提供的环境值。

        该方法只返回给 ``MaintenancePreparationService`` 的私有材料；公开 API
        仍只投影摘要。读取发生在准备事务的瞬间，调用方随后会用正文重新计算
        before/after fingerprint，避免仅凭预览摘要跨越 TOCTOU 窗口。
        """
        if plan.target_id != "environment":
            raise MaintenanceTargetError("仅支持 environment 材料")
        status = self.inspect("environment")
        values = self._read_environment()
        leaves: dict[str, MaintenanceLeafMaterial] = {}
        environments: dict[str, MaintenanceEnvironmentMaterial] = {}
        for leaf_id, name in zip(MAINTENANCE_LEAVES_BY_TARGET["environment"], _ENV_KEYS):
            current = values.get(name)
            before = None if current is None else current.encode("utf-8")
            expected = self._expected_environment[name].encode("utf-8")
            leaves[leaf_id] = MaintenanceLeafMaterial(
                leaf_id=leaf_id,
                existence="missing" if current is None else "present",
                before_hash=None if before is None else _sha256(before),
                expected_hash=_sha256(expected),
                file_mode=None,
                before_bytes=before,
                expected_bytes=expected,
            )
            environments[name] = MaintenanceEnvironmentMaterial(
                name=name,
                state="missing" if current is None else ("empty" if current == "" else "value"),
                value=current,
            )
        return status, leaves, environments

    def _target(self, target_id: str):
        """通过静态注册表解析目标，不提供路径或动态 fallback。"""

        from ...core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY

        return MAINTENANCE_TARGET_REGISTRY.get(target_id)

    def _inspect_target(self, target_id: str) -> list[dict[str, object]]:
        """按固定叶子顺序读取目标，返回内部摘要观察，不向调用方暴露路径。"""

        if target_id == "environment":
            try:
                environment = self._read_environment()
            except Exception:
                return [
                    {"difference": MaintenanceManagedDifference(leaf, "invalid"), "fingerprint_part": f"{leaf}:invalid"}
                    for leaf in MAINTENANCE_LEAVES_BY_TARGET[target_id]
                ]
            return [self._observe_environment(leaf, environment) for leaf in MAINTENANCE_LEAVES_BY_TARGET[target_id]]
        agent = "codex" if target_id == "agent.codex" else "claude-code"
        return [self._observe_file(agent, leaf) for leaf in MAINTENANCE_LEAVES_BY_TARGET[target_id]]

    def _read_environment(self) -> Mapping[str, str | None]:
        """读取 fixture mapping 或惰性 HKCU reader，并限制为两个固定键。"""

        if self._environment is not None:
            # fixture mapping 允许省略键，以表达用户级值缺失；构造函数已经
            # 拒绝了额外键，inspect 仍只读取这两个固定名称。
            values = self._environment
        else:
            values = self._environment_reader()
            if set(values) != set(_ENV_KEYS):
                raise MaintenanceTargetError("环境 reader 未返回固定键集合")
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            raise MaintenanceTargetError("环境 reader 返回值类型无效")
        return values

    def _observe_environment(self, leaf_id: str, environment: Mapping[str, str | None]) -> dict[str, object]:
        """比较固定环境变量但不保存或返回变量值。"""

        key = leaf_id.rsplit(".", 1)[-1].upper()
        key = "AIKB_HOME" if key == "AIKB_HOME" else "AIKB_KNOWLEDGE_HOME"
        current = environment.get(key)
        expected = self._expected_environment[key]
        if current == expected:
            code = "unchanged"
        elif current is None:
            code = "missing"
        else:
            code = "drifted"
        # 环境差异不提供逐值哈希，避免从投影侧推断用户路径或秘密。
        difference = MaintenanceManagedDifference(leaf_id=leaf_id, difference_code=code)
        current_fingerprint = _sha256(("<missing>" if current is None else current).encode())
        return {"difference": difference, "fingerprint_part": f"{leaf_id}:{current_fingerprint}"}

    def _observe_file(self, agent: str, leaf_id: str) -> dict[str, object]:
        """安全读取一个固定配置叶子并仅比较受管片段。"""

        path = self._leaf_path(agent, leaf_id)
        if self._has_reparse_component(path):
            return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
        if not path.exists():
            return {"difference": MaintenanceManagedDifference(leaf_id, "missing"), "fingerprint_part": f"{leaf_id}:missing"}
        if not path.is_file():
            return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
        try:
            raw = path.read_bytes()
            if leaf_id.endswith("root_instructions"):
                return self._observe_root(leaf_id, raw)
            if leaf_id.endswith(".mcp") and agent == "codex":
                return self._observe_codex_mcp(leaf_id, raw)
            if leaf_id.endswith(".mcp"):
                return self._observe_claude_mcp(leaf_id, raw)
            return self._observe_hooks(agent, leaf_id, raw)
        except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError):
            return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}

    def _observe_root(self, leaf_id: str, raw: bytes) -> dict[str, object]:
        """识别唯一根指令受管区块，用户非受管内容只参与存在性判断。"""

        text = raw.decode("utf-8")
        pattern = _managed_block_pattern(_ROOT_START, _ROOT_END)
        matches = pattern.findall(text)
        if len(matches) > 1:
            return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
        if not matches:
            if _ROOT_START in text or _ROOT_END in text:
                return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
            return {"difference": MaintenanceManagedDifference(leaf_id, "missing", after_hash=_sha256(_expected_root())), "fingerprint_part": f"{leaf_id}:missing"}
        actual = matches[0].replace("\r\n", "\n").encode("utf-8")
        expected = _expected_root()
        code = "unchanged" if actual == expected else "drifted"
        difference = MaintenanceManagedDifference(leaf_id, code, _sha256(actual), _sha256(expected))
        return {"difference": difference, "fingerprint_part": f"{leaf_id}:{_sha256(actual)}"}

    def _observe_codex_mcp(self, leaf_id: str, raw: bytes) -> dict[str, object]:
        """解析 Codex TOML 并区分受管标记与非受管同名冲突。"""

        text = raw.decode("utf-8")
        tomllib.loads(text)
        marker_pattern = _managed_block_pattern(_CODEX_MCP_START, _CODEX_MCP_END)
        matches = marker_pattern.findall(text)
        # TOML section 名同样遵循结构键契约；大小写变体不能被当作两个
        # 可安全合并的服务。受管块内部的标准 section 只计一次。
        section_count = len(re.findall(r"(?im)^\s*\[mcp_servers\.aikb\]\s*$", text))
        if section_count and not matches:
            code = "conflict"
            return {"difference": MaintenanceManagedDifference(leaf_id, code), "fingerprint_part": f"{leaf_id}:conflict"}
        if len(matches) > 1:
            return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
        if not matches:
            if _CODEX_MCP_START in text or _CODEX_MCP_END in text:
                return {"difference": MaintenanceManagedDifference(leaf_id, "invalid"), "fingerprint_part": f"{leaf_id}:invalid"}
            return {"difference": MaintenanceManagedDifference(leaf_id, "missing", after_hash=_sha256(_expected_codex_mcp())), "fingerprint_part": f"{leaf_id}:missing"}
        actual = matches[0].replace("\r\n", "\n").encode("utf-8")
        expected = _expected_codex_mcp()
        code = "unchanged" if actual == expected else "drifted"
        return {"difference": MaintenanceManagedDifference(leaf_id, code, _sha256(actual), _sha256(expected)), "fingerprint_part": f"{leaf_id}:{_sha256(actual)}"}

    def _observe_claude_mcp(self, leaf_id: str, raw: bytes) -> dict[str, object]:
        """解析 Claude MCP 对象，非 AIKB_MANAGED 对象一律冲突。"""

        config = _load_json_preserving_keys(raw)
        if not isinstance(config, dict):
            raise ValueError("Claude MCP 根必须是对象")
        servers_key = _find_json_key(config, "mcpServers")
        servers = config.get(servers_key) if servers_key is not None else None
        if servers is None or not isinstance(servers, dict):
            raise ValueError("mcpServers 必须是对象")
        aikb_key = _find_json_key(servers, "aikb")
        actual = servers.get(aikb_key) if aikb_key is not None else None
        expected = _expected_claude_mcp()
        if actual is None:
            return {"difference": MaintenanceManagedDifference(leaf_id, "missing", after_hash=_sha256(_canonical_json(expected))), "fingerprint_part": f"{leaf_id}:missing"}
        env_key = _find_json_key(actual, "env") if isinstance(actual, dict) else None
        env = actual.get(env_key) if env_key is not None else None
        marker_key = _find_json_key(env, "AIKB_MANAGED") if isinstance(env, dict) else None
        if not isinstance(actual, dict) or not isinstance(env, dict) or marker_key is None or env.get(marker_key) != "1":
            return {"difference": MaintenanceManagedDifference(leaf_id, "conflict"), "fingerprint_part": f"{leaf_id}:conflict"}
        actual_hash = _sha256(_canonical_json(_semantic_json(actual)))
        expected_hash = _sha256(_canonical_json(expected))
        code = "unchanged" if _semantic_json(actual) == _semantic_json(expected) else "drifted"
        return {"difference": MaintenanceManagedDifference(leaf_id, code, actual_hash, expected_hash), "fingerprint_part": f"{leaf_id}:{actual_hash}"}

    def _observe_hooks(self, agent: str, leaf_id: str, raw: bytes) -> dict[str, object]:
        """解析 hooks，只提取 AIKB handler 的结构化摘要，忽略用户其他 handlers。"""

        config = _load_json_preserving_keys(raw)
        hooks_key = _find_json_key(config, "hooks") if isinstance(config, dict) else None
        if not isinstance(config, dict) or (hooks_key is not None and not isinstance(config[hooks_key], dict)):
            raise ValueError("hooks 根必须是对象")
        hooks = config.get(hooks_key, {}) if hooks_key is not None else {}
        managed: list[dict[str, object]] = []
        for event in _EVENTS:
            if not isinstance(hooks, dict):
                raise ValueError("hooks 必须是对象")
            event_key = _find_json_key(hooks, event)
            groups = hooks.get(event_key, []) if event_key is not None else []
            if not isinstance(groups, list):
                raise ValueError("hook event 必须是数组")
            for group in groups:
                if not isinstance(group, dict):
                    raise ValueError("hook group 必须是对象")
                handlers_key = _find_json_key(group, "hooks")
                handlers = group.get(handlers_key, []) if handlers_key is not None else []
                if not isinstance(handlers, list):
                    raise ValueError("hook handlers 必须是数组")
                for handler in handlers:
                    if not isinstance(handler, dict):
                        raise ValueError("hook handler 必须是对象")
                    command_key = _find_json_key(handler, "command")
                    command = handler.get(command_key) if command_key is not None else None
                    if isinstance(command, str) and _HOOK_SCRIPT in command:
                        matcher_key = _find_json_key(group, "matcher")
                        managed.append({"event": event, "matcher": group.get(matcher_key) if matcher_key else None, "handler": _canonical_hook_handler(handler)})
        if not managed:
            expected = _expected_hooks(agent)
            return {"difference": MaintenanceManagedDifference(leaf_id, "missing", after_hash=_sha256(_canonical_json(expected))), "fingerprint_part": f"{leaf_id}:missing"}
        actual_hash = _sha256(_canonical_json(managed))
        expected = _expected_hooks(agent)
        expected_hash = _sha256(_canonical_json(expected))
        code = "unchanged" if managed == expected else "drifted"
        return {"difference": MaintenanceManagedDifference(leaf_id, code, actual_hash, expected_hash), "fingerprint_part": f"{leaf_id}:{actual_hash}"}

    def _leaf_path(self, agent: str, leaf_id: str) -> Path:
        """将已验证逻辑叶子映射到注入 fixture 的固定文件；不接受任意叶子。"""

        if agent == "codex":
            mapping = {
                "agent.codex.root_instructions": self._codex_home / "AGENTS.md",
                "agent.codex.mcp": self._codex_home / "config.toml",
                "agent.codex.hooks": self._codex_home / "hooks.json",
            }
        else:
            mapping = {
                "agent.claude-code.root_instructions": self._claude_home / "CLAUDE.md",
                "agent.claude-code.mcp": self._claude_user_config,
                "agent.claude-code.hooks": self._claude_home / "settings.json",
            }
        try:
            return mapping[leaf_id]
        except KeyError as error:
            raise MaintenanceTargetError("逻辑叶子与 Agent 不匹配") from error

    @staticmethod
    def _has_reparse_component(path: Path) -> bool:
        """拒绝 fixture 路径中的符号链接和 Windows 重解析点，不跟随读取。"""

        current = path
        while current != current.parent:
            try:
                if current.is_symlink():
                    return True
                stat = current.stat()
                if getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT:
                    return True
            except (FileNotFoundError, NotADirectoryError):
                # 叶子或中间目录可以尚未创建；继续向已存在的父目录检查，
                # 否则恶意的父级重解析点会被“叶子缺失”过早掩盖。
                current = current.parent
                continue
            except OSError as error:
                # 无法判定 ACL/占用等安全边界时由上层标记 invalid，绝不猜测
                # 这是普通缺失路径。
                raise MaintenanceTargetError("配置边界无法安全判定") from error
            current = current.parent
        return False

    @staticmethod
    def _combine_status(observations: list[dict[str, object]]) -> MaintenanceStatus:
        """按安全优先级合并叶子结果；冲突/损坏不会被可修复状态掩盖。"""

        codes = [item["difference"].difference_code for item in observations]
        if "invalid" in codes:
            return MaintenanceStatus.INVALID
        if "conflict" in codes:
            return MaintenanceStatus.CONFLICT
        if all(code == "unchanged" for code in codes):
            return MaintenanceStatus.READY
        if all(code == "missing" for code in codes):
            return MaintenanceStatus.MISSING
        return MaintenanceStatus.DRIFTED

    @staticmethod
    def _reason_for(status: MaintenanceStatus) -> str:
        """把状态映射到核心固定 reason_code，禁止自由说明文本。"""

        return {
            MaintenanceStatus.READY: "none",
            MaintenanceStatus.MISSING: "target_missing",
            MaintenanceStatus.DRIFTED: "managed_content_drifted",
            MaintenanceStatus.CONFLICT: "managed_conflict",
            MaintenanceStatus.INVALID: "target_invalid",
            MaintenanceStatus.UNSUPPORTED: "unsupported_platform",
            MaintenanceStatus.RESTART_REQUIRED: "restart_required",
        }[status]

    def _target_fingerprint(self, observations: list[dict[str, object]]) -> str:
        """由逻辑叶子与当前摘要组成整体指纹，不暴露底层路径或正文。"""

        return _sha256("\n".join(str(item["fingerprint_part"]) for item in observations).encode("utf-8"))

    def _expected_fingerprint(self, target_id: str) -> str:
        """计算固定期望状态摘要；环境期望值只进入摘要，不进入规划响应。"""

        if target_id == "environment":
            parts = [f"{leaf}:{_sha256(self._expected_environment[key].encode())}" for leaf, key in zip(MAINTENANCE_LEAVES_BY_TARGET[target_id], _ENV_KEYS)]
            return _sha256("\n".join(parts).encode())
        agent = "codex" if target_id == "agent.codex" else "claude-code"
        expected = {
            "root_instructions": _expected_root(),
            "mcp": _expected_codex_mcp() if agent == "codex" else _canonical_json(_expected_claude_mcp()),
            "hooks": _canonical_json(_expected_hooks(agent)),
        }
        return _sha256("\n".join(f"{leaf}:{_sha256(expected['root_instructions' if leaf.endswith('root_instructions') else 'mcp' if leaf.endswith('mcp') else 'hooks'])}" for leaf in MAINTENANCE_LEAVES_BY_TARGET[target_id]).encode())


def build_windows_readonly_adapter(settings: object) -> WindowsMaintenanceAdapter:
    """生产只读适配器安全工厂；失败由上层转为不可用，不泄露底层路径。"""

    return WindowsMaintenanceAdapter.from_settings(settings)


__all__ = ["WindowsMaintenanceAdapter", "build_windows_readonly_adapter"]
