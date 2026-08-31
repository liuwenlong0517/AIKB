"""维护事务逐叶子恢复的纯逻辑判定。

本模块只消费事务摘要和平台适配器提供的逻辑观察，不读取文件、环境，不调用
回滚执行器。所有输出只包含静态目标、叶子、写步骤和固定 decision；环境值、
物理路径、命令和备份材料永远不进入模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .maintenance_targets import (
    MAINTENANCE_LEAVES_BY_TARGET,
    MAINTENANCE_TARGET_IDS,
    MAINTENANCE_TARGET_REGISTRY,
    MAINTENANCE_WRITE_LEAVES_BY_TARGET,
    validate_logical_id,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


class RecoveryContractError(ValueError):
    """恢复输入未满足静态目标、叶子、标识或摘要契约。"""


class RecoveryDecision(str, Enum):
    """恢复判定的固定枚举。"""

    NOOP = "already_before/noop"
    RESTORE_BEFORE = "restore_before"
    REMOVE_CREATED = "remove_created"
    THIRD_PARTY_CHANGED = "third_party_changed/recovery_required"
    MATERIAL_INVALID = "material_invalid"


class LeafExistence(str, Enum):
    """文件叶子的逻辑存在性，不携带物理定位。"""

    MISSING = "missing"
    PRESENT = "present"


class EnvironmentState(str, Enum):
    """环境叶子的私有观察状态，明确区分 missing 与 empty。"""

    MISSING = "missing"
    EMPTY = "empty"
    VALUE = "value"


@dataclass(frozen=True)
class RecoveryLeaf:
    """事务叶子的恢复前摘要和期望摘要。"""

    leaf_id: str
    before_existence: str
    before_hash: str | None
    expected_hash: str
    progress: str = "applied"

    def __post_init__(self) -> None:
        _validate_leaf(self)


@dataclass(frozen=True)
class CurrentLeafObservation:
    """平台返回的当前逻辑观察；只允许 missing/present 与摘要。"""

    existence: str
    current_hash: str | None

    def __post_init__(self) -> None:
        if self.existence not in {item.value for item in LeafExistence}:
            raise RecoveryContractError("current existence 无效")
        _check_hash(self.current_hash, "current_hash", nullable=self.existence == "missing")


@dataclass(frozen=True)
class EnvironmentObservation:
    """环境私有观察；value 本身不允许进入此模型，只记录状态和摘要。"""

    state: str
    value_hash: str | None

    def __post_init__(self) -> None:
        if self.state not in {item.value for item in EnvironmentState}:
            raise RecoveryContractError("environment state 无效")
        _check_hash(self.value_hash, "value_hash", nullable=self.state == "missing")


@dataclass(frozen=True)
class LeafRecoveryDecision:
    """单叶子的固定恢复决定。"""

    leaf_id: str
    decision: RecoveryDecision

    def __post_init__(self) -> None:
        _check_id(self.leaf_id, "leaf_id")
        if not isinstance(self.decision, RecoveryDecision):
            raise RecoveryContractError("恢复决定无效")

    def to_dict(self) -> dict[str, str]:
        """生成仅含逻辑 ID 和固定决策的安全投影。"""
        return {"leaf_id": self.leaf_id, "decision": self.decision.value}


@dataclass(frozen=True)
class RecoveryStep:
    """逆序写步骤及聚合决定，不携带命令或材料位置。"""

    step_id: str
    decision: RecoveryDecision
    leaf_ids: tuple[str, ...]
    leaf_decisions: tuple[LeafRecoveryDecision, ...]

    def __post_init__(self) -> None:
        _check_id(self.step_id, "step_id")
        if self.step_id not in {"write_environment", "write_root_instructions", "write_mcp", "write_hooks"}:
            raise RecoveryContractError("恢复步骤不是静态写步骤")
        if tuple(item.leaf_id for item in self.leaf_decisions) != self.leaf_ids:
            raise RecoveryContractError("恢复步骤叶子决定不匹配")
        if self.decision != _aggregate(self.leaf_decisions):
            raise RecoveryContractError("恢复步骤聚合决定不匹配")

    def to_dict(self) -> dict[str, Any]:
        """生成固定逻辑恢复步骤。"""
        return {
            "step_id": self.step_id,
            "decision": self.decision.value,
            "leaf_ids": list(self.leaf_ids),
            "leaf_decisions": [item.to_dict() for item in self.leaf_decisions],
        }


def _check_id(value: Any, field: str) -> str:
    """校验逻辑标识，不回显用户输入。"""
    try:
        return validate_logical_id(value, field)
    except (TypeError, ValueError) as error:
        raise RecoveryContractError(f"{field} 格式无效") from error


def _check_hash(value: Any, field: str, *, nullable: bool = False) -> str | None:
    """只接受小写 SHA-256；缺失叶子允许显式 null。"""
    if nullable:
        if value is not None:
            raise RecoveryContractError(f"{field} 缺失语义必须为 null")
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise RecoveryContractError(f"{field} 必须是 64 位小写 SHA-256")
    return value


def _validate_leaf(leaf: RecoveryLeaf) -> None:
    """校验叶子摘要与存在性语义。"""
    if not isinstance(leaf, RecoveryLeaf):
        raise RecoveryContractError("恢复叶子类型无效")
    _check_id(leaf.leaf_id, "leaf_id")
    if leaf.before_existence not in {item.value for item in LeafExistence}:
        raise RecoveryContractError("before existence 无效")
    _check_hash(leaf.before_hash, "before_hash", nullable=leaf.before_existence == "missing")
    _check_hash(leaf.expected_hash, "expected_hash")
    if leaf.progress not in {"pending", "applying", "applied", "verifying", "verified", "rolling_back", "rolled_back"}:
        raise RecoveryContractError("leaf progress 无效")


def _normalize_observation(value: CurrentLeafObservation | EnvironmentObservation) -> CurrentLeafObservation | None:
    """规范化平台观察；环境 empty/value 保留存在语义但不暴露值。"""
    if isinstance(value, CurrentLeafObservation):
        if value.existence not in {item.value for item in LeafExistence}:
            return None
        try:
            current_hash = _check_hash(value.current_hash, "current_hash", nullable=value.existence == "missing")
        except RecoveryContractError:
            return None
        return CurrentLeafObservation(value.existence, current_hash)
    if isinstance(value, EnvironmentObservation):
        if value.state not in {item.value for item in EnvironmentState}:
            return None
        existence = "missing" if value.state == "missing" else "present"
        try:
            current_hash = _check_hash(value.value_hash, "value_hash", nullable=value.state == "missing")
        except RecoveryContractError:
            return None
        return CurrentLeafObservation(existence, current_hash)
    return None


def decide_leaf(leaf: RecoveryLeaf, current: CurrentLeafObservation | EnvironmentObservation) -> LeafRecoveryDecision:
    """按事务前/期望摘要判定单叶子，第三方状态绝不允许覆盖。"""
    _validate_leaf(leaf)
    observation = _normalize_observation(current)
    if observation is None:
        return LeafRecoveryDecision(leaf.leaf_id, RecoveryDecision.MATERIAL_INVALID)
    if leaf.before_existence == "missing" and observation.existence == "missing":
        return LeafRecoveryDecision(leaf.leaf_id, RecoveryDecision.NOOP)
    if (
        leaf.before_existence == "present"
        and observation.existence == "present"
        and observation.current_hash == leaf.before_hash
    ):
        return LeafRecoveryDecision(leaf.leaf_id, RecoveryDecision.NOOP)
    # pending 叶子即使碰巧等于 expected，也没有唯一证据证明是本事务写入；
    # 必须转人工恢复，不能覆盖潜在第三方修改。
    if observation.existence == "present" and observation.current_hash == leaf.expected_hash and leaf.progress not in {"pending", "rolled_back"}:
        decision = RecoveryDecision.RESTORE_BEFORE if leaf.before_existence == "present" else RecoveryDecision.REMOVE_CREATED
        return LeafRecoveryDecision(leaf.leaf_id, decision)
    return LeafRecoveryDecision(leaf.leaf_id, RecoveryDecision.THIRD_PARTY_CHANGED)


def _aggregate(decisions: tuple[LeafRecoveryDecision, ...]) -> RecoveryDecision:
    """按最保守优先级聚合一个写步骤的叶子判定。"""
    values = {item.decision for item in decisions}
    for decision in (RecoveryDecision.MATERIAL_INVALID, RecoveryDecision.THIRD_PARTY_CHANGED,
                     RecoveryDecision.RESTORE_BEFORE, RecoveryDecision.REMOVE_CREATED):
        if decision in values:
            return decision
    return RecoveryDecision.NOOP


def build_recovery_plan(
    target_id: str,
    leaves: tuple[RecoveryLeaf, ...],
    observations: Mapping[str, CurrentLeafObservation | EnvironmentObservation],
    steps: tuple[str, ...],
) -> tuple[RecoveryStep, ...]:
    """校验目标叶子顺序并按静态写步骤逆序生成逻辑恢复计划。"""
    _check_id(target_id, "target_id")
    if target_id not in MAINTENANCE_TARGET_IDS:
        raise RecoveryContractError("target_id 不是固定维护目标")
    expected_leaves = MAINTENANCE_LEAVES_BY_TARGET[target_id]
    if not isinstance(leaves, tuple) or tuple(item.leaf_id for item in leaves) != expected_leaves:
        raise RecoveryContractError("恢复叶子顺序必须匹配目标")
    if not isinstance(steps, tuple) or steps != tuple(MAINTENANCE_TARGET_REGISTRY.get(target_id).steps):
        raise RecoveryContractError("恢复步骤必须匹配目标")
    if not isinstance(observations, Mapping) or set(observations) != set(expected_leaves):
        raise RecoveryContractError("恢复观察必须匹配目标叶子")
    decisions = tuple(decide_leaf(leaf, observations[leaf.leaf_id]) for leaf in leaves)
    plans: list[RecoveryStep] = []
    write_map = MAINTENANCE_WRITE_LEAVES_BY_TARGET[target_id]
    for step_id in reversed(steps):
        leaf_ids = tuple(write_map.get(step_id, ()))
        if not leaf_ids:
            continue
        selected = tuple(item for item in decisions if item.leaf_id in leaf_ids)
        plans.append(RecoveryStep(step_id, _aggregate(selected), leaf_ids, selected))
    return tuple(plans)


__all__ = [
    "CurrentLeafObservation", "EnvironmentObservation", "EnvironmentState", "LeafExistence",
    "LeafRecoveryDecision", "RecoveryContractError", "RecoveryDecision", "RecoveryLeaf",
    "RecoveryStep", "build_recovery_plan", "decide_leaf",
]
