"""阶段 4B 安装与修复的静态目标和安全状态模型。

本模块只描述服务端固定的维护目标及其公开投影，不读取配置、不解析文件，
也不执行安装或修复。目标注册表刻意使用不可变的代码内常量，避免浏览器、
配置文件或调用参数把 WebUI 变成任意路径/命令执行入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class MaintenanceTargetError(ValueError):
    """维护目标或状态模型不满足固定安全契约时抛出的错误。"""


class MaintenanceStatus(str, Enum):
    """维护目标对外可见的有限状态集合。"""

    READY = "ready"
    MISSING = "missing"
    DRIFTED = "drifted"
    CONFLICT = "conflict"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    RESTART_REQUIRED = "restart_required"


MAINTENANCE_STATUSES = tuple(item.value for item in MaintenanceStatus)
MAINTENANCE_RISK_LEVEL = "user_config_write"
_LOGICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# action ID、step ID 和目标 ID 都是服务端代码内的闭合集合。它们用于把
# 浏览器请求约束到已审查的动作语义，不能通过配置文件或请求参数扩展。
MAINTENANCE_ACTION_IDS = (
    "maintenance.environment.update",
    "maintenance.agent.codex.repair",
    "maintenance.agent.claude-code.repair",
)
MAINTENANCE_ACTION_BY_TARGET: Mapping[str, str] = MappingProxyType(
    {
        "environment": "maintenance.environment.update",
        "agent.codex": "maintenance.agent.codex.repair",
        "agent.claude-code": "maintenance.agent.claude-code.repair",
    }
)
MAINTENANCE_STEP_IDS = (
    "preflight",
    "backup",
    "write_environment",
    "write_root_instructions",
    "write_mcp",
    "write_hooks",
    "verify",
    "rollback",
)

# 状态只返回固定 reason_code，避免把路径、底层异常或用户配置内容塞进
# summary/blocking_reason 等自由文本字段。每个状态与原因是一一对应关系。
MAINTENANCE_REASON_CODES = (
    "none",
    "target_missing",
    "managed_content_drifted",
    "managed_conflict",
    "target_invalid",
    "unsupported_platform",
    "restart_required",
)
MAINTENANCE_DIFFERENCE_CODES = (
    "unchanged",
    "missing",
    "drifted",
    "conflict",
    "invalid",
)
_REASON_BY_STATUS: Mapping[str, str] = MappingProxyType(
    {
        "ready": "none",
        "missing": "target_missing",
        "drifted": "managed_content_drifted",
        "conflict": "managed_conflict",
        "invalid": "target_invalid",
        "unsupported": "unsupported_platform",
        "restart_required": "restart_required",
    }
)

# 这些叶子是语义 ID，不是文件名或路径。平台适配器只能把它们映射到自己
# 已验证的固定位置，公共模型永远不携带该映射结果。
_TARGET_LEAVES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "environment": (
            "user_environment.aikb_home",
            "user_environment.aikb_knowledge_home",
        ),
        "agent.codex": (
            "agent.codex.root_instructions",
            "agent.codex.mcp",
            "agent.codex.hooks",
        ),
        "agent.claude-code": (
            "agent.claude-code.root_instructions",
            "agent.claude-code.mcp",
            "agent.claude-code.hooks",
        ),
    }
)
# 后续事务模型只应依赖这两个公开只读事实源，而不复制目标叶子清单。
# 保留 `_TARGET_LEAVES` 作为内部别名，便于平台实现替换内部构造方式而不改变
# 对外契约；映射值使用 tuple，调用方无法通过返回值修改注册表。
MAINTENANCE_LEAVES_BY_TARGET: Mapping[str, tuple[str, ...]] = _TARGET_LEAVES
MAINTENANCE_LEAF_IDS = tuple(
    dict.fromkeys(leaf for target_leaves in MAINTENANCE_LEAVES_BY_TARGET.values() for leaf in target_leaves)
)
_TARGET_STEPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "environment": (
            "preflight",
            "backup",
            "write_environment",
            "verify",
        ),
        "agent.codex": (
            "preflight",
            "backup",
            "write_root_instructions",
            "write_mcp",
            "write_hooks",
            "verify",
        ),
        "agent.claude-code": (
            "preflight",
            "backup",
            "write_root_instructions",
            "write_mcp",
            "write_hooks",
            "verify",
        ),
    }
)
_TARGET_EFFECTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "environment": ("write:user_environment:aikb",),
        "agent.codex": ("write:agent_config:codex",),
        "agent.claude-code": ("write:agent_config:claude-code",),
    }
)


def validate_logical_id(value: str, field_name: str = "logical_id") -> str:
    """校验不含路径分隔符、空白和控制字符的逻辑标识。

    参数只允许固定模型使用的逻辑 ID；调用方即使传入 ``../``、盘符或
    ``content/...`` 也不会得到路径解析或降级匹配。返回原值便于在模型中统一
    做类型和格式收敛。
    """

    if not isinstance(value, str) or _LOGICAL_ID_RE.fullmatch(value) is None:
        raise MaintenanceTargetError(f"{field_name} 格式无效")
    return value


@dataclass(frozen=True)
class MaintenanceTargetSpec:
    """一个不可变的静态维护目标定义。

    ``logical_leaves`` 和 ``steps`` 都是语义标识，不能承载物理路径、命令、
    配置正文或用户提供的自由参数。``public_dict`` 是可直接交给 API 的安全
    投影，字段集合固定，便于调用方进行严格 schema 校验。
    """

    target_id: str
    title: str
    description: str
    risk_level: str
    action_id: str
    effects: tuple[str, ...]
    logical_leaves: tuple[str, ...]
    steps: tuple[str, ...]
    supported_platforms: tuple[str, ...] = ("windows",)
    confirmation_required: bool = True

    def __post_init__(self) -> None:
        """校验目标定义与固定风险/平台约束，防止静态契约被意外扩展。"""

        validate_logical_id(self.target_id, "target_id")
        if self.risk_level != MAINTENANCE_RISK_LEVEL:
            raise MaintenanceTargetError("维护目标风险级别必须是 user_config_write")
        if self.action_id not in MAINTENANCE_ACTION_IDS:
            raise MaintenanceTargetError("维护目标动作 ID 未声明")
        if MAINTENANCE_ACTION_BY_TARGET.get(self.target_id) != self.action_id:
            raise MaintenanceTargetError("维护目标动作 ID 与目标不匹配")
        if not self.effects or any(not isinstance(item, str) or ":" not in item for item in self.effects):
            raise MaintenanceTargetError("维护目标副作用格式无效")
        if _TARGET_EFFECTS.get(self.target_id) != self.effects:
            raise MaintenanceTargetError("维护目标副作用必须使用静态定义")
        if not self.logical_leaves or any(_LOGICAL_ID_RE.fullmatch(item) is None for item in self.logical_leaves):
            raise MaintenanceTargetError("维护目标叶子必须是安全逻辑 ID")
        if _TARGET_LEAVES.get(self.target_id) != self.logical_leaves:
            raise MaintenanceTargetError("维护目标叶子必须使用静态定义")
        if not self.steps or any(item not in MAINTENANCE_STEP_IDS for item in self.steps):
            raise MaintenanceTargetError("维护目标步骤格式无效")
        if _TARGET_STEPS.get(self.target_id) != self.steps:
            raise MaintenanceTargetError("维护目标步骤必须使用静态定义")
        if self.supported_platforms != ("windows",):
            raise MaintenanceTargetError("首版维护目标只允许声明 Windows")
        if self.confirmation_required is not True:
            raise MaintenanceTargetError("维护目标必须要求逐目标确认")

    def public_dict(self) -> dict[str, Any]:
        """返回固定公开投影；不包含物理路径、命令、正文或任意参数。"""

        return {
            "target_id": self.target_id,
            "title": self.title,
            "description": self.description,
            "risk_level": self.risk_level,
            "action_id": self.action_id,
            "effects": list(self.effects),
            "logical_leaves": list(self.logical_leaves),
            "steps": list(self.steps),
            "supported_platforms": list(self.supported_platforms),
            "confirmation_required": self.confirmation_required,
        }

    # 与现有规则模型保持同一公开投影调用习惯。
    to_public_dict = public_dict


@dataclass(frozen=True)
class MaintenanceTargetStatus:
    """维护目标的安全状态投影，不携带文件内容或物理定位信息。"""

    target_id: str
    status: MaintenanceStatus | str
    logical_leaves: tuple[str, ...]
    steps: tuple[str, ...]
    reason_code: str = "none"
    base_fingerprint: str | None = None
    restart_required: bool = False
    differences: tuple["MaintenanceManagedDifference", ...] = ()

    def __post_init__(self) -> None:
        """校验状态、逻辑叶子和哈希，确保公开响应不可能回显路径类字段。"""

        # 状态不是可扩展的通用标签：目标、叶子和步骤必须从同一个静态
        # 注册表精确取值，避免调用方拼出“看似合法”的跨目标组合。
        target = MAINTENANCE_TARGET_REGISTRY.get(self.target_id)
        status = self.status.value if isinstance(self.status, MaintenanceStatus) else self.status
        if status not in MAINTENANCE_STATUSES:
            raise MaintenanceTargetError("维护目标状态无效")
        if self.reason_code not in MAINTENANCE_REASON_CODES:
            raise MaintenanceTargetError("维护目标 reason_code 无效")
        if self.reason_code != _REASON_BY_STATUS[status]:
            raise MaintenanceTargetError("reason_code 与维护目标状态不匹配")
        if not isinstance(self.logical_leaves, tuple) or tuple(self.logical_leaves) != target.logical_leaves:
            raise MaintenanceTargetError("状态叶子必须与目标静态定义完全一致")
        if not isinstance(self.steps, tuple) or tuple(self.steps) != target.steps:
            raise MaintenanceTargetError("状态步骤必须与目标静态定义完全一致")
        if self.base_fingerprint is not None and _HASH_RE.fullmatch(self.base_fingerprint) is None:
            raise MaintenanceTargetError("base_fingerprint 必须是 SHA-256")
        if not isinstance(self.restart_required, bool):
            raise MaintenanceTargetError("restart_required 必须是布尔值")
        if status == "restart_required" and not self.restart_required:
            raise MaintenanceTargetError("restart_required 状态必须要求重启")
        if status != "restart_required" and self.restart_required:
            raise MaintenanceTargetError("只有 restart_required 状态可以要求重启")
        if status in {"ready", "missing", "drifted", "restart_required"} and self.base_fingerprint is None:
            raise MaintenanceTargetError("该状态必须提供 base_fingerprint")
        if status == "unsupported" and self.base_fingerprint is not None:
            raise MaintenanceTargetError("unsupported 状态不得提供 base_fingerprint")
        if any(not isinstance(item, MaintenanceManagedDifference) for item in self.differences):
            raise MaintenanceTargetError("状态差异类型无效")
        if self.differences and tuple(item.leaf_id for item in self.differences) != target.logical_leaves:
            raise MaintenanceTargetError("状态差异必须与目标静态叶子完全一致")
        if self.differences:
            codes = {item.difference_code for item in self.differences}
            derived_status = (
                "invalid" if "invalid" in codes else
                "conflict" if "conflict" in codes else
                "ready" if codes == {"unchanged"} else
                "missing" if codes == {"missing"} else
                "drifted"
            )
            if status != derived_status:
                raise MaintenanceTargetError("状态与受管差异代码不一致")
        object.__setattr__(self, "status", status)

    def public_dict(self) -> dict[str, Any]:
        """生成字段固定的状态响应，不返回正文、路径、命令或敏感配置。"""

        return {
            "target_id": self.target_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "logical_leaves": list(self.logical_leaves),
            "steps": list(self.steps),
            "base_fingerprint": self.base_fingerprint,
            "restart_required": self.restart_required,
            "differences": [item.public_dict() for item in self.differences],
        }

    to_public_dict = public_dict


@dataclass(frozen=True)
class MaintenanceManagedDifference:
    """单个受管逻辑叶子的摘要差异，只允许固定代码和可选 SHA-256。

    该模型故意不携带整段 Markdown/TOML/JSON、非受管正文、环境值或路径；
    ``before_hash``/``after_hash`` 只用于让用户确认受管片段确实发生了什么
    语义变化，后续 API 可直接使用其公开投影。
    """

    leaf_id: str
    difference_code: str
    before_hash: str | None = None
    after_hash: str | None = None

    def __post_init__(self) -> None:
        """校验逻辑叶子、差异枚举和摘要格式，拒绝正文伪装输入。"""

        validate_logical_id(self.leaf_id, "leaf_id")
        if self.difference_code not in MAINTENANCE_DIFFERENCE_CODES:
            raise MaintenanceTargetError("受管差异代码未声明")
        for field_name in ("before_hash", "after_hash"):
            value = getattr(self, field_name)
            if value is not None and _HASH_RE.fullmatch(value) is None:
                raise MaintenanceTargetError(f"{field_name} 必须是 SHA-256")

    def public_dict(self) -> dict[str, Any]:
        """返回受管片段的最小结构化差异，不回显片段正文。"""

        return {
            "leaf_id": self.leaf_id,
            "difference_code": self.difference_code,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
        }

    to_public_dict = public_dict


class MaintenanceTargetRegistry:
    """固定目标注册表；只提供读取，不提供运行时注册或配置扩展入口。"""

    def __init__(self) -> None:
        """创建共享静态定义的只读视图，不复制或扫描用户配置。"""

        self._targets: Mapping[str, MaintenanceTargetSpec] = _TARGETS

    def list(self) -> list[MaintenanceTargetSpec]:
        """按稳定顺序返回三个固定目标的新列表，调用方修改列表不影响注册表。"""

        return list(self._targets.values())

    def get(self, target_id: str) -> MaintenanceTargetSpec:
        """按精确逻辑 ID 获取目标；未知 ID 和路径样式输入都直接拒绝。"""

        validate_logical_id(target_id, "target_id")
        try:
            return self._targets[target_id]
        except KeyError as error:
            raise MaintenanceTargetError("未知维护目标") from error


_TARGETS: Mapping[str, MaintenanceTargetSpec] = MappingProxyType(
    {
        "environment": MaintenanceTargetSpec(
            target_id="environment",
            title="AIKB 用户环境",
            description="维护当前 Windows 用户的 AIKB 控制仓与知识仓环境变量。",
            risk_level=MAINTENANCE_RISK_LEVEL,
            action_id=MAINTENANCE_ACTION_BY_TARGET["environment"],
            effects=_TARGET_EFFECTS["environment"],
            logical_leaves=_TARGET_LEAVES["environment"],
            steps=_TARGET_STEPS["environment"],
        ),
        "agent.codex": MaintenanceTargetSpec(
            target_id="agent.codex",
            title="Codex 安装修复",
            description="修复当前用户 Codex 的 AIKB 根指令、MCP 受管区块与 hooks。",
            risk_level=MAINTENANCE_RISK_LEVEL,
            action_id=MAINTENANCE_ACTION_BY_TARGET["agent.codex"],
            effects=_TARGET_EFFECTS["agent.codex"],
            logical_leaves=_TARGET_LEAVES["agent.codex"],
            steps=_TARGET_STEPS["agent.codex"],
        ),
        "agent.claude-code": MaintenanceTargetSpec(
            target_id="agent.claude-code",
            title="Claude Code 安装修复",
            description="修复当前用户 Claude Code 的 AIKB 根指令、MCP 受管对象与 hooks。",
            risk_level=MAINTENANCE_RISK_LEVEL,
            action_id=MAINTENANCE_ACTION_BY_TARGET["agent.claude-code"],
            effects=_TARGET_EFFECTS["agent.claude-code"],
            logical_leaves=_TARGET_LEAVES["agent.claude-code"],
            steps=_TARGET_STEPS["agent.claude-code"],
        ),
    }
)

MAINTENANCE_TARGET_IDS = tuple(_TARGETS)
MAINTENANCE_TARGET_REGISTRY = MaintenanceTargetRegistry()


__all__ = [
    "MAINTENANCE_RISK_LEVEL",
    "MAINTENANCE_ACTION_BY_TARGET",
    "MAINTENANCE_ACTION_IDS",
    "MAINTENANCE_DIFFERENCE_CODES",
    "MAINTENANCE_LEAF_IDS",
    "MAINTENANCE_LEAVES_BY_TARGET",
    "MAINTENANCE_REASON_CODES",
    "MAINTENANCE_STATUSES",
    "MAINTENANCE_STEP_IDS",
    "MAINTENANCE_TARGET_IDS",
    "MAINTENANCE_TARGET_REGISTRY",
    "MaintenanceStatus",
    "MaintenanceManagedDifference",
    "MaintenanceTargetError",
    "MaintenanceTargetRegistry",
    "MaintenanceTargetSpec",
    "MaintenanceTargetStatus",
    "validate_logical_id",
]
