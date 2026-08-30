"""阶段 4A 规则变更事务、状态机和内部动作契约。

事务模型只携带逻辑 ID、哈希、状态和安全时间字段。候选正文、完整 diff、
备份以及物理路径属于后续本机事务目录材料，不能进入此模型、任务参数或公开
投影；本模块也不执行写入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from .rules import RuleError, RULE_RISK_LEVEL, _validate_hash, _validate_revision


RULE_UPDATE_ACTION_ID = "rule.user.update"
RULE_UPDATE_EFFECT = "write:control_rule:user"
RULE_CHANGE_STATUSES = (
    "prepared",
    "applying",
    "validating",
    "succeeded",
    "expired",
    "rejected",
    "rolling_back",
    "rolled_back",
    "recovery_required",
)
TERMINAL_RULE_CHANGE_STATUSES = frozenset({"succeeded", "expired", "rejected", "rolled_back", "recovery_required"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T.*(?:Z|[+]00:00)$")
_ALLOWED_TRANSACTION_FIELDS = frozenset(
    {
        "change_id", "rule_id", "action_id", "risk_level", "status", "before_hash", "after_hash",
        "diff_hash", "preview_digest", "validator_version", "repository_revision", "created_at",
        "expires_at", "updated_at", "task_id", "rollback_status",
    }
)
_TRANSITIONS = MappingProxyType(
    {
        "prepared": frozenset({"applying", "expired", "rejected"}),
        "applying": frozenset({"validating", "rolling_back"}),
        "validating": frozenset({"succeeded", "rolling_back"}),
        "rolling_back": frozenset({"rolled_back", "recovery_required"}),
        "succeeded": frozenset(),
        "expired": frozenset(),
        "rejected": frozenset(),
        "rolled_back": frozenset(),
        "recovery_required": frozenset(),
    }
)


def _safe_id(value: str, field_name: str) -> str:
    """校验事务、任务和版本等逻辑标识，拒绝路径、空白和过长文本。"""
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise RuleError(f"{field_name} 格式无效")
    return value


def _safe_time(value: str, field_name: str) -> str:
    """要求带 UTC 偏移的 ISO-8601 时间，并统一为 ``Z`` 形式保存。"""
    if not isinstance(value, str) or _ISO_UTC_RE.fullmatch(value) is None:
        raise RuleError(f"{field_name} 必须是 UTC ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise RuleError(f"{field_name} 不是有效时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RuleError(f"{field_name} 必须使用 UTC")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RuleChangeTransaction:
    """不含正文/diff/路径的规则变更事务事实模型。"""

    change_id: str
    rule_id: str
    action_id: str
    risk_level: str
    status: str
    before_hash: str
    after_hash: str
    diff_hash: str
    preview_digest: str
    validator_version: str
    repository_revision: str
    created_at: str
    expires_at: str
    updated_at: str
    task_id: str | None = None
    rollback_status: str = "not_started"

    def __post_init__(self) -> None:
        """校验固定枚举、逻辑 ID、摘要和时间字段，防止不安全事务落盘。"""
        _safe_id(self.change_id, "change_id")
        if len(self.change_id) > 120:
            raise RuleError("change_id 格式无效")
        if self.rule_id != "user":
            raise RuleError("阶段 4A 事务只允许 user 规则")
        if self.action_id != RULE_UPDATE_ACTION_ID:
            raise RuleError("规则事务动作 ID 无效")
        if self.risk_level != RULE_RISK_LEVEL:
            raise RuleError("规则事务风险级别无效")
        if self.status not in RULE_CHANGE_STATUSES:
            raise RuleError("规则事务状态无效")
        for field_name in ("before_hash", "after_hash", "diff_hash", "preview_digest"):
            _validate_hash(getattr(self, field_name), field_name)
        _safe_id(self.validator_version, "validator_version")
        _validate_revision(self.repository_revision)
        created = _safe_time(self.created_at, "created_at")
        expires = _safe_time(self.expires_at, "expires_at")
        updated = _safe_time(self.updated_at, "updated_at")
        if datetime.fromisoformat(expires[:-1] + "+00:00") <= datetime.fromisoformat(created[:-1] + "+00:00"):
            raise RuleError("expires_at 必须晚于 created_at")
        if datetime.fromisoformat(updated[:-1] + "+00:00") < datetime.fromisoformat(created[:-1] + "+00:00"):
            raise RuleError("updated_at 不能早于 created_at")
        if self.task_id is not None:
            _safe_id(self.task_id, "task_id")
        if self.rollback_status not in {
            "not_applicable", "not_started", "pending", "succeeded", "recovery_required",
        }:
            raise RuleError("rollback_status 无效")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "updated_at", updated)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuleChangeTransaction":
        """严格从事务 JSON 载荷恢复；候选正文、diff 和路径字段一律拒绝。"""
        if not isinstance(payload, Mapping):
            raise RuleError("事务载荷必须是 JSON 对象")
        unknown = set(payload) - _ALLOWED_TRANSACTION_FIELDS
        if unknown:
            raise RuleError("事务载荷包含不允许字段")
        required = _ALLOWED_TRANSACTION_FIELDS - {"task_id", "rollback_status"}
        missing = required - set(payload)
        if missing:
            raise RuleError("事务载荷缺少字段")
        return cls(**dict(payload))

    def to_dict(self) -> dict[str, Any]:
        """生成可落盘的安全事务投影，字段集合固定且不含正文。"""
        return {
            "change_id": self.change_id,
            "rule_id": self.rule_id,
            "action_id": self.action_id,
            "risk_level": self.risk_level,
            "status": self.status,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "diff_hash": self.diff_hash,
            "preview_digest": self.preview_digest,
            "validator_version": self.validator_version,
            "repository_revision": self.repository_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "task_id": self.task_id,
            "rollback_status": self.rollback_status,
        }

    public_dict = to_dict

    def can_transition(self, next_status: str) -> bool:
        """判断状态机是否允许下一状态，不改变事务。"""
        return next_status in _TRANSITIONS[self.status]

    def transition(self, next_status: str, *, updated_at: str | None = None) -> "RuleChangeTransaction":
        """返回状态迁移后的新事务；非法迁移不会静默覆盖状态。"""
        if next_status not in RULE_CHANGE_STATUSES or not self.can_transition(next_status):
            raise RuleError(f"不允许从 {self.status} 迁移到 {next_status}")
        rollback_status = self.rollback_status
        if next_status == "rolling_back":
            rollback_status = "pending"
        elif next_status == "succeeded":
            rollback_status = "not_applicable"
        elif next_status == "rolled_back":
            rollback_status = "succeeded"
        elif next_status == "recovery_required":
            rollback_status = "recovery_required"
        return replace(self, status=next_status, updated_at=updated_at or self.updated_at, rollback_status=rollback_status)


@dataclass(frozen=True)
class RuleUpdateActionSpec:
    """内部 ``rule.user.update`` 动作的最小准入契约，不注册到公共动作 API。"""

    action_id: str = RULE_UPDATE_ACTION_ID
    title: str = "应用个人规则变更"
    description: str = "使用服务端规则事务应用已确认的 USER_RULES.md 变更。"
    supported_platforms: tuple[str, ...] = ("windows",)
    risk_level: str = RULE_RISK_LEVEL
    effects: tuple[str, ...] = (RULE_UPDATE_EFFECT,)
    executor_kind: str = "rule_transaction"
    confirmation_required: bool = True
    parameter_schema: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType(
            {
                "type": "object",
                "required": ["change_id"],
                "properties": {"change_id": {"type": "string", "maxLength": 120, "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$"}},
                "additionalProperties": False,
            }
        )
    )

    def validate_parameters(self, parameters: Mapping[str, Any]) -> dict[str, str]:
        """只接受服务端生成的 change_id；拒绝正文、路径、命令和额外字段。"""
        if not isinstance(parameters, Mapping) or set(parameters) != {"change_id"}:
            raise RuleError("rule.user.update 只接受 change_id")
        change_id = _safe_id(parameters["change_id"], "change_id")
        return {"change_id": change_id}


RULE_USER_UPDATE_SPEC = RuleUpdateActionSpec()
