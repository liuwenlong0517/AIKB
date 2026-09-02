"""阶段 4B 维护变更的安全数据契约。

本模块只描述安装/修复事务的逻辑事实，不读取或写入用户配置，也不执行命令。
事务 JSON 与公开投影都故意不包含正文、完整 diff、物理路径、备份、环境值、
命令或秘密；未来的事务执行器只能把本模块作为其状态和安全元数据边界。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from .maintenance_targets import (
    MAINTENANCE_ACTION_BY_TARGET,
    MAINTENANCE_ACTION_IDS,
    MAINTENANCE_LEAVES_BY_TARGET,
    MAINTENANCE_RISK_LEVEL,
    MAINTENANCE_TARGET_IDS,
    MAINTENANCE_TARGET_REGISTRY,
    validate_logical_id,
)


class MaintenanceChangeError(ValueError):
    """维护事务契约无效，或请求了未声明的状态迁移。"""


MAINTENANCE_CHANGE_STATUSES = (
    "prepared",
    "expired",
    "applying",
    "verifying",
    "succeeded",
    "rolling_back",
    "rolled_back",
    "recovery_required",
)
TERMINAL_MAINTENANCE_CHANGE_STATUSES = frozenset(
    {"expired", "succeeded", "rolled_back", "recovery_required"}
)
MAINTENANCE_ROLLBACK_STATUSES = (
    "not_started",
    "pending",
    "not_applicable",
    "succeeded",
    "recovery_required",
)

# 状态转换图是声明式常量，执行器不得通过异常或用户输入增加隐式跳转。
MAINTENANCE_CHANGE_TRANSITIONS = MappingProxyType(
    {
        # prepared 表示尚未发生平台写入；确认上下文丢失时只能安全收敛为
        # expired，发现任务/写入证据时则由恢复器转入 recovery_required。
        "prepared": frozenset({"applying", "expired", "recovery_required"}),
        "expired": frozenset(),
        "applying": frozenset({"verifying", "rolling_back"}),
        "verifying": frozenset({"succeeded", "rolling_back"}),
        "succeeded": frozenset({"recovery_required"}),
        "rolling_back": frozenset({"rolled_back", "recovery_required"}),
        "rolled_back": frozenset({"recovery_required"}),
        "recovery_required": frozenset(),
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$")
_LEAF_PROGRESS = frozenset(
    {
        "pending",
        "applying",
        "applied",
        "verifying",
        "verified",
        "rolling_back",
        "rolled_back",
        "recovery_required",
    }
)

_LEAF_FIELDS = frozenset(
    {"leaf_id", "existence", "before_hash", "expected_hash", "progress"}
)
_CHANGE_FIELDS = frozenset(
    {
        "schema_version",
        "change_id",
        "target_id",
        "action_id",
        "risk_level",
        "status",
        "base_fingerprint",
        "before_fingerprint",
        "after_fingerprint",
        "step_summary",
        "task_id",
        "rollback_status",
        "restart_required",
        "created_at",
        "expires_at",
        "updated_at",
        "preview_digest",
        "leaf_states",
    }
)
_MAX_STEP_COUNT = 16
_MAX_LEAF_COUNT = 32
_MAX_SERIALIZED_BYTES = 16 * 1024


def _safe_id(value: Any, field_name: str) -> str:
    """校验逻辑标识；拒绝路径分隔符、空白、控制字符和过长文本。"""
    try:
        return validate_logical_id(value, field_name)
    except (TypeError, ValueError) as error:
        raise MaintenanceChangeError(f"{field_name} 格式无效") from error


def _safe_hash(value: Any, field_name: str) -> str:
    """只接受 64 位小写 SHA-256，避免把正文或路径伪装成指纹。"""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MaintenanceChangeError(f"{field_name} 必须是 64 位小写 SHA-256")
    return value


def _safe_time(value: Any, field_name: str) -> str:
    """校验 UTC ISO-8601 时间并规范化为带微秒的 ``Z`` 表示。"""
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise MaintenanceChangeError(f"{field_name} 必须是 UTC ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise MaintenanceChangeError(f"{field_name} 不是有效时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MaintenanceChangeError(f"{field_name} 必须使用 UTC")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """把已经校验过的标准 UTC 时间转为比较用对象。"""
    return datetime.fromisoformat(value[:-1] + "+00:00")


@dataclass(frozen=True)
class MaintenanceLeafState:
    """单个逻辑叶子的可恢复安全元数据。

    叶子只保留逻辑 ID、存在/缺失语义、事务前和期望哈希以及进度；物理路径、
    原始字节、环境变量值和备份位置属于执行器私有材料，不能由此模型承载。
    """

    leaf_id: str
    existence: str
    before_hash: str | None
    expected_hash: str
    progress: str = "pending"

    def __post_init__(self) -> None:
        """校验叶子字段集合的固定枚举、ID 和摘要预算。"""
        _safe_id(self.leaf_id, "leaf_id")
        if self.existence not in {"present", "missing"}:
            raise MaintenanceChangeError("leaf existence 无效")
        if self.existence == "missing":
            if self.before_hash is not None:
                raise MaintenanceChangeError("缺失叶子的 before_hash 必须为 null")
        else:
            _safe_hash(self.before_hash, "before_hash")
        _safe_hash(self.expected_hash, "expected_hash")
        if self.progress not in _LEAF_PROGRESS:
            raise MaintenanceChangeError("leaf progress 无效")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MaintenanceLeafState":
        """严格恢复叶子元数据；未知字段不会被静默忽略。"""
        if not isinstance(payload, Mapping):
            raise MaintenanceChangeError("叶子载荷必须是 JSON 对象")
        if set(payload) != _LEAF_FIELDS:
            raise MaintenanceChangeError("叶子载荷字段集合无效")
        return cls(**dict(payload))

    def to_dict(self) -> dict[str, Any]:
        """生成不含物理定位或正文材料的内部/公开叶子投影。"""
        return {
            "leaf_id": self.leaf_id,
            "existence": self.existence,
            "before_hash": self.before_hash,
            "expected_hash": self.expected_hash,
            "progress": self.progress,
        }

    to_public_dict = to_dict

    @property
    def exists(self) -> bool:
        """提供只读布尔便捷视图；落盘仍使用显式的 present/missing 语义。"""
        return self.existence == "present"


@dataclass(frozen=True)
class MaintenanceChange:
    """阶段 4B 跨文件维护事务的逻辑状态和安全摘要。

    本类不承担备份、原子替换、环境写入或恢复职责；``transition`` 仅返回新值，
    因此状态落盘和实际副作用仍必须由后续事务执行器显式完成并审计。
    """

    change_id: str
    target_id: str
    action_id: str
    risk_level: str
    status: str
    base_fingerprint: str
    before_fingerprint: str
    after_fingerprint: str
    step_summary: tuple[str, ...]
    preview_digest: str
    created_at: str
    expires_at: str
    updated_at: str
    task_id: str | None = None
    rollback_status: str = "not_started"
    restart_required: bool = False
    leaf_states: tuple[MaintenanceLeafState, ...] = field(default_factory=tuple)
    schema_version: int = 1

    def __post_init__(self) -> None:
        """校验全量事务契约，并规范化时间、序列字段以便稳定 JSON 化。"""
        _safe_id(self.change_id, "change_id")
        if self.target_id not in MAINTENANCE_TARGET_IDS:
            raise MaintenanceChangeError("target_id 不是固定维护目标")
        if self.action_id not in MAINTENANCE_ACTION_IDS:
            raise MaintenanceChangeError("action_id 不是固定维护动作")
        if MAINTENANCE_ACTION_BY_TARGET[self.target_id] != self.action_id:
            raise MaintenanceChangeError("action_id 与 target_id 不匹配")
        if self.risk_level != MAINTENANCE_RISK_LEVEL:
            raise MaintenanceChangeError("risk_level 必须为 user_config_write")
        if self.status not in MAINTENANCE_CHANGE_STATUSES:
            raise MaintenanceChangeError("维护事务状态无效")
        try:
            target_spec = MAINTENANCE_TARGET_REGISTRY.get(self.target_id)
        except (TypeError, ValueError) as error:
            raise MaintenanceChangeError("target_id 不是固定维护目标") from error
        for field_name in (
            "base_fingerprint",
            "before_fingerprint",
            "after_fingerprint",
            "preview_digest",
        ):
            _safe_hash(getattr(self, field_name), field_name)
        if not isinstance(self.step_summary, (tuple, list)):
            raise MaintenanceChangeError("step_summary 必须是步骤数组")
        if not 1 <= len(self.step_summary) <= _MAX_STEP_COUNT:
            raise MaintenanceChangeError("step_summary 超出数量预算")
        normalized_steps: list[str] = []
        for step in self.step_summary:
            if not isinstance(step, str):
                raise MaintenanceChangeError("step_summary 含未声明步骤")
            normalized_steps.append(step)
        if tuple(normalized_steps) != target_spec.steps:
            raise MaintenanceChangeError("step_summary 必须精确匹配目标步骤")
        if not isinstance(self.leaf_states, (tuple, list)):
            raise MaintenanceChangeError("leaf_states 必须是叶子数组")
        if not 1 <= len(self.leaf_states) <= _MAX_LEAF_COUNT:
            raise MaintenanceChangeError("leaf_states 超出数量预算")
        normalized_leaves: list[MaintenanceLeafState] = []
        seen_leaf_ids: set[str] = set()
        for leaf in self.leaf_states:
            if not isinstance(leaf, MaintenanceLeafState):
                raise MaintenanceChangeError("leaf_states 含无效叶子")
            if leaf.leaf_id in seen_leaf_ids:
                raise MaintenanceChangeError("leaf_states 不得重复逻辑叶子")
            seen_leaf_ids.add(leaf.leaf_id)
            normalized_leaves.append(leaf)
        expected_leaf_ids = tuple(MAINTENANCE_LEAVES_BY_TARGET[self.target_id])
        if tuple(leaf.leaf_id for leaf in normalized_leaves) != expected_leaf_ids:
            raise MaintenanceChangeError("leaf_states 必须精确匹配目标叶子及顺序")
        progress_by_status = {
            "prepared": frozenset({"pending"}),
            "expired": frozenset({"pending"}),
            "applying": frozenset({"pending", "applying", "applied"}),
            "verifying": frozenset({"applied", "verifying", "verified"}),
            "succeeded": frozenset({"verified"}),
            "rolling_back": frozenset({"pending", "applied", "verified", "rolling_back", "rolled_back"}),
            "rolled_back": frozenset({"pending", "rolled_back"}),
        }
        progresses = tuple(leaf.progress for leaf in normalized_leaves)
        if self.status == "recovery_required":
            # 进入人工恢复态的事务必须指出至少一个无法安全自动收敛的叶子；
            # 多个叶子同时恢复失败是合法结果，其余叶子可以停留在安全进度。
            if "recovery_required" not in progresses:
                raise MaintenanceChangeError("recovery_required 事务必须包含恢复叶子")
            safe_progresses = _LEAF_PROGRESS - {"recovery_required"}
            if any(
                progress not in safe_progresses
                for progress in progresses
                if progress != "recovery_required"
            ):
                raise MaintenanceChangeError("recovery_required 事务叶子进度无效")
        elif any(progress not in progress_by_status[self.status] for progress in progresses):
            raise MaintenanceChangeError("事务状态与叶子进度不一致")
        if not isinstance(self.restart_required, bool):
            raise MaintenanceChangeError("restart_required 必须是布尔值")
        if self.rollback_status not in MAINTENANCE_ROLLBACK_STATUSES:
            raise MaintenanceChangeError("rollback_status 无效")
        expected_rollback_status = {
            "prepared": "not_started",
            "expired": "not_started",
            "applying": "not_started",
            "verifying": "not_started",
            "succeeded": "not_applicable",
            "rolling_back": "pending",
            "rolled_back": "succeeded",
            "recovery_required": "recovery_required",
        }[self.status]
        if self.rollback_status != expected_rollback_status:
            raise MaintenanceChangeError("rollback_status 与事务状态不匹配")
        if self.status not in {"prepared", "expired"} and self.task_id is None:
            raise MaintenanceChangeError("非 prepared 事务必须关联 task_id")
        if self.task_id is not None:
            _safe_id(self.task_id, "task_id")
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise MaintenanceChangeError("schema_version 无效")
        created = _safe_time(self.created_at, "created_at")
        expires = _safe_time(self.expires_at, "expires_at")
        updated = _safe_time(self.updated_at, "updated_at")
        if _parse_time(expires) <= _parse_time(created):
            raise MaintenanceChangeError("expires_at 必须晚于 created_at")
        if _parse_time(updated) < _parse_time(created):
            raise MaintenanceChangeError("updated_at 不能早于 created_at")
        object.__setattr__(self, "step_summary", tuple(normalized_steps))
        object.__setattr__(self, "leaf_states", tuple(normalized_leaves))
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "updated_at", updated)
        # 序列化预算防止事务摘要被滥用为任意大文本容器，即使未来新增安全 ID。
        if len(json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_SERIALIZED_BYTES:
            raise MaintenanceChangeError("维护事务摘要超出大小预算")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MaintenanceChange":
        """严格从内部事务 JSON 恢复，拒绝所有未知字段和非叶子正文。"""
        if not isinstance(payload, Mapping):
            raise MaintenanceChangeError("事务载荷必须是 JSON 对象")
        if set(payload) != _CHANGE_FIELDS:
            raise MaintenanceChangeError("事务载荷字段集合无效")
        raw = dict(payload)
        if not isinstance(raw["step_summary"], list):
            raise MaintenanceChangeError("step_summary 必须是数组")
        if not isinstance(raw["leaf_states"], list):
            raise MaintenanceChangeError("leaf_states 必须是数组")
        raw["step_summary"] = tuple(raw["step_summary"])
        raw["leaf_states"] = tuple(MaintenanceLeafState.from_dict(item) for item in raw["leaf_states"])
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        """生成供事务事实源保存的固定内部序列化，仍不含敏感材料。"""
        return {
            "schema_version": self.schema_version,
            "change_id": self.change_id,
            "target_id": self.target_id,
            "action_id": self.action_id,
            "risk_level": self.risk_level,
            "status": self.status,
            "base_fingerprint": self.base_fingerprint,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "step_summary": list(self.step_summary),
            "task_id": self.task_id,
            "rollback_status": self.rollback_status,
            "restart_required": self.restart_required,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "preview_digest": self.preview_digest,
            "leaf_states": [leaf.to_dict() for leaf in self.leaf_states],
        }

    def to_public_dict(self) -> dict[str, Any]:
        """生成 API/任务可用投影；省略内部 schema 版本且不引入任何正文字段。"""
        data = self.to_dict()
        data.pop("schema_version")
        return data

    # 与 4A 事务模型保持调用习惯，同时保留显式命名以提醒调用方这是公开投影。
    public_dict = to_public_dict

    @property
    def safety_steps(self) -> tuple[str, ...]:
        """返回固定语义步骤的别名，避免调用方误解为可执行命令列表。"""
        return self.step_summary

    @property
    def leaves(self) -> tuple[MaintenanceLeafState, ...]:
        """返回逻辑叶子元数据别名；不暴露任何物理路径或原始内容。"""
        return self.leaf_states

    def can_transition(self, next_status: str) -> bool:
        """判断下一状态是否位于冻结转换图中，不改变事务。"""
        return next_status in MAINTENANCE_CHANGE_TRANSITIONS[self.status]

    def transition(
        self,
        next_status: str,
        *,
        updated_at: str | None = None,
        task_id: str | None = None,
        leaf_states: tuple[MaintenanceLeafState, ...] | None = None,
    ) -> "MaintenanceChange":
        """返回合法状态迁移后的新事务；非法跳转或叶子进度不一致时 fail-closed。

        ``leaf_states`` 由执行器在完成真实叶子步骤后显式传入；方法不会猜测写入
        是否成功，也不会为了迁移状态自动把 pending 伪造为 applied/verified。
        """
        if next_status not in MAINTENANCE_CHANGE_STATUSES or not self.can_transition(next_status):
            raise MaintenanceChangeError(f"不允许从 {self.status} 迁移到 {next_status}")
        next_time = self.updated_at if updated_at is None else _safe_time(updated_at, "updated_at")
        if _parse_time(next_time) < _parse_time(self.updated_at):
            raise MaintenanceChangeError("updated_at 不能回退")
        rollback_status = self.rollback_status
        if next_status == "rolling_back":
            rollback_status = "pending"
        elif next_status == "succeeded":
            rollback_status = "not_applicable"
        elif next_status == "rolled_back":
            rollback_status = "succeeded"
        elif next_status == "recovery_required":
            rollback_status = "recovery_required"
        next_task_id = self.task_id if task_id is None else task_id
        if next_status not in {"prepared", "expired"} and next_task_id is None:
            raise MaintenanceChangeError("非 prepared 事务必须关联 task_id")
        next_leaf_states = self.leaf_states if leaf_states is None else leaf_states
        return replace(
            self,
            status=next_status,
            rollback_status=rollback_status,
            updated_at=next_time,
            task_id=next_task_id,
            leaf_states=next_leaf_states,
        )


__all__ = [
    "MAINTENANCE_ACTION_BY_TARGET",
    "MAINTENANCE_ACTION_IDS",
    "MAINTENANCE_CHANGE_STATUSES",
    "MAINTENANCE_CHANGE_TRANSITIONS",
    "MAINTENANCE_RISK_LEVEL",
    "MAINTENANCE_TARGET_IDS",
    "MAINTENANCE_ROLLBACK_STATUSES",
    "MaintenanceChange",
    "MaintenanceChangeError",
    "MaintenanceLeafState",
    "TERMINAL_MAINTENANCE_CHANGE_STATUSES",
]
