"""将可信维护预览安全转换为 staged 或 prepared 事务。

本模块只接受服务端 ``MaintenancePlan``、检查状态和可信材料提供器；浏览器不会
提供路径、环境值或期望正文。apply 阶段才创建目录和私有材料，任何中途失败都
保留损坏现场供恢复扫描阻断，绝不删除或伪造可执行事务。
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol

from ..platform.maintenance import MaintenancePlan
from .actions import ConfirmationTokenService
from .maintenance_changes import MaintenanceChange, MaintenanceLeafState
from .maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial, MaintenanceMaterialStore
from .maintenance_targets import MaintenanceTargetStatus
from .maintenance_targets import MAINTENANCE_TARGET_REGISTRY
from .maintenance_transaction_store import MaintenanceTransactionStore

_MANAGED_FINGERPRINT_SUFFIX_RE = re.compile(r"^(?:missing|[0-9a-f]{64})$")


def _is_valid_managed_fingerprint_part(leaf_id: str, part: object) -> bool:
    """只接受固定叶子前缀和 missing/SHA-256 摘要，拒绝自由 provider 文本。"""
    if not isinstance(part, str):
        return False
    prefix = f"{leaf_id}:"
    return part.startswith(prefix) and bool(_MANAGED_FINGERPRINT_SUFFIX_RE.fullmatch(part[len(prefix):]))


class MaintenancePreparationError(ValueError):
    """预览过期、状态冲突、可信材料不完整或事务准备失败。"""


class MaintenanceMaterialProvider(Protocol):
    """服务端可信材料提供器，不接受浏览器正文、路径或命令。"""

    def capture(self, plan: MaintenancePlan) -> tuple[MaintenanceTargetStatus, Mapping[str, MaintenanceLeafMaterial], Mapping[str, MaintenanceEnvironmentMaterial]]: ...


# 兼容波次 3 environment 提供器的导入名；新代码应使用目标无关的协议名。
EnvironmentMaterialProvider = MaintenanceMaterialProvider


@dataclass(frozen=True)
class PreparedMaintenance:
    """prepared 事务和一次性确认令牌的安全包络，不包含材料正文。"""

    change: MaintenanceChange
    confirmation_token: str

    def to_dict(self) -> dict[str, object]:
        """返回安全事务摘要和令牌。"""
        return {"change": self.change.to_public_dict(), "confirmation_token": self.confirmation_token}


@dataclass(frozen=True)
class StagedMaintenancePreview:
    """只存在进程内的预览暂存；不含材料正文、路径或事务目录。"""

    change_id: str
    plan: MaintenancePlan
    status: MaintenanceTargetStatus
    confirmation_token: str
    expires_at: str

    def to_dict(self) -> dict[str, object]:
        """返回前端可用的预览绑定字段，不创建或读取磁盘事实。"""
        return {
            "change_id": self.change_id,
            "preview_digest": self.plan.preview_digest,
            "expires_at": self.expires_at,
            "expires_in_seconds": ConfirmationTokenService.TTL_SECONDS,
            "confirmation_token": self.confirmation_token,
        }


class MaintenancePreparationService:
    """将 environment/Agent 预览暂存并在 apply 阶段转换为 prepared 事务。"""

    def __init__(self, transactions: MaintenanceTransactionStore, material_store_factory: object, tokens: ConfirmationTokenService) -> None:
        self._transactions = transactions
        if not callable(material_store_factory):
            raise MaintenancePreparationError("材料存储工厂接口无效")
        self._material_store_factory = material_store_factory
        self._tokens = tokens
        self._staged_lock = threading.RLock()
        self._staged: dict[str, StagedMaintenancePreview] = {}

    def stage(self, plan: MaintenancePlan, status: MaintenanceTargetStatus) -> StagedMaintenancePreview:
        """把安全 plan/status 暂存五分钟；此方法绝不创建事务、材料或审计。"""
        self._validate_plan_status(plan, status)
        change_id = f"maintenance-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ConfirmationTokenService.TTL_SECONDS)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        target = MAINTENANCE_TARGET_REGISTRY.get(plan.target_id)
        token = self._tokens.issue(
            action_id=target.action_id,
            parameters={"change_id": change_id},
            risk_level="user_config_write",
            preview_digest=plan.preview_digest,
        )
        staged = StagedMaintenancePreview(change_id, plan, status, token, expires_at)
        with self._staged_lock:
            self._staged[change_id] = staged
        return staged

    def staged(self, change_id: str) -> StagedMaintenancePreview | None:
        """按逻辑 ID 读取当前进程暂存的预览；过期或未知时返回空。"""
        with self._staged_lock:
            value = self._staged.get(change_id)
            if value is None:
                return None
            try:
                self._tokens.validate(
                    value.confirmation_token,
                    action_id=MAINTENANCE_TARGET_REGISTRY.get(value.plan.target_id).action_id,
                    parameters={"change_id": value.change_id},
                    risk_level="user_config_write",
                    preview_digest=value.plan.preview_digest,
                )
            except Exception:
                self._staged.pop(change_id, None)
                return None
            return value

    def materialize(
        self,
        staged: StagedMaintenancePreview,
        plan: MaintenancePlan,
        status: MaintenanceTargetStatus,
        provider: MaintenanceMaterialProvider,
        confirmation_token: str,
    ) -> PreparedMaintenance:
        """在 apply 阶段复核新 plan/status 后才创建 prepared 事务和私有材料。"""
        with self._staged_lock:
            current = self._staged.get(staged.change_id)
            if current is None or current.confirmation_token != confirmation_token:
                raise MaintenancePreparationError("预览不存在或已过期")
            try:
                target = MAINTENANCE_TARGET_REGISTRY.get(current.plan.target_id)
                self._tokens.validate(
                    confirmation_token,
                    action_id=target.action_id,
                    parameters={"change_id": current.change_id},
                    risk_level="user_config_write",
                    preview_digest=current.plan.preview_digest,
                )
            except Exception as error:
                raise MaintenancePreparationError("预览不存在或已过期") from error
            if current.plan.public_dict() != plan.public_dict() or current.status.public_dict() != status.public_dict():
                raise MaintenancePreparationError("预览基线已变化，请重新读取")
            result = self._prepare(
                plan,
                status,
                provider,
                change_id=staged.change_id,
                confirmation_token=confirmation_token,
                issue_token=False,
            )
            self._staged.pop(staged.change_id, None)
            return result

    def prepare(self, plan: MaintenancePlan, status: MaintenanceTargetStatus, provider: MaintenanceMaterialProvider) -> PreparedMaintenance:
        """重新绑定当前基线后创建事实目录、私有材料并签发单次令牌。"""
        return self._prepare(plan, status, provider, change_id=None, confirmation_token=None, issue_token=True)

    @staticmethod
    def _validate_plan_status(plan: MaintenancePlan, status: MaintenanceTargetStatus) -> None:
        """验证 plan/status 的目标和可修复基线，不读取任何平台目标。"""
        if plan.target_id != status.target_id:
            raise MaintenancePreparationError("维护目标与状态不匹配")
        status_value = status.status.value if hasattr(status.status, "value") else status.status
        if status_value not in {"missing", "drifted"} or status.base_fingerprint != plan.before_fingerprint:
            raise MaintenancePreparationError("预览基线已变化或当前状态不可修复")

    def _prepare(
        self,
        plan: MaintenancePlan,
        status: MaintenanceTargetStatus,
        provider: MaintenanceMaterialProvider,
        *,
        change_id: str | None,
        confirmation_token: str | None,
        issue_token: bool,
    ) -> PreparedMaintenance:
        """捕获可信材料并创建事务；仅 apply materialize 或兼容直调用会进入。"""
        self._validate_plan_status(plan, status)
        try:
            capture = getattr(provider, "capture", None)
            if not callable(capture):
                capture = getattr(provider, "capture_environment", None) or getattr(provider, "capture_agent", None)
            if not callable(capture):
                raise MaintenancePreparationError("可信材料提供器接口无效")
            fresh_status, leaves, environments = capture(plan)
            if fresh_status.target_id != plan.target_id or fresh_status.base_fingerprint != plan.before_fingerprint:
                raise MaintenancePreparationError("当前环境状态已变化")
            target_leaves = tuple(getattr(plan, "logical_leaves", MAINTENANCE_TARGET_REGISTRY.get(plan.target_id).logical_leaves))
            if tuple(leaves) != target_leaves:
                raise MaintenancePreparationError("可信叶子材料不完整")
            if plan.target_id != "environment" and environments:
                raise MaintenancePreparationError("Agent 材料不得携带环境值")
            self._validate_captured_material(plan, provider, leaves, environments)
            change_id = change_id or f"maintenance-{uuid.uuid4().hex}"
            leaf_order = tuple(getattr(plan, "logical_leaves", MAINTENANCE_TARGET_REGISTRY.get(plan.target_id).logical_leaves))
            leaf_states = tuple(MaintenanceLeafState(key, leaves[key].existence, leaves[key].before_hash, leaves[key].expected_hash) for key in leaf_order)
            now = datetime.now(timezone.utc)
            created_at = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
            expires_at = (now + timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            change = MaintenanceChange(
                change_id=change_id, target_id=plan.target_id,
                action_id=MAINTENANCE_TARGET_REGISTRY.get(plan.target_id).action_id, risk_level="user_config_write",
                status="prepared", base_fingerprint=plan.before_fingerprint, before_fingerprint=plan.before_fingerprint, after_fingerprint=plan.after_fingerprint, step_summary=tuple(step.step_id for step in plan.steps), preview_digest=plan.preview_digest, created_at=created_at, expires_at=expires_at, updated_at=created_at, leaf_states=leaf_states,
            )
            self._transactions.create(change)
            materials = self._material_store_factory(self._transactions)
            if not callable(getattr(materials, "prepare", None)):
                raise MaintenancePreparationError("材料存储接口无效")
            materials.prepare(change_id, plan.target_id, leaves, environments)
            token = confirmation_token
            if issue_token:
                token = self._tokens.issue(action_id=change.action_id, parameters={"change_id": change_id}, risk_level=change.risk_level, preview_digest=change.preview_digest)
            if not isinstance(token, str) or not token:
                raise MaintenancePreparationError("维护确认令牌无效")
            return PreparedMaintenance(change, token)
        except MaintenancePreparationError:
            raise
        except Exception as error:
            raise MaintenancePreparationError("维护事务准备失败") from error

    @staticmethod
    def _validate_captured_material(
        plan: MaintenancePlan,
        provider: MaintenanceMaterialProvider,
        leaves: Mapping[str, MaintenanceLeafMaterial],
        environments: Mapping[str, MaintenanceEnvironmentMaterial],
    ) -> None:
        """用材料正文重算前后整体指纹，拒绝 provider 仅口头声明绑定。

        Agent 文件允许携带用户的非受管内容，因此通过生产只读适配器的固定
        ``managed_fingerprint_part`` 重新解析受管片段；整文件摘要仍单独校验，
        供 preflight/rollback 防止第三方在 apply 前后替换整个文件。
        """
        leaf_order = tuple(getattr(plan, "logical_leaves", MAINTENANCE_TARGET_REGISTRY.get(plan.target_id).logical_leaves))
        names = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
        if plan.target_id == "environment" and tuple(environments) != names:
            raise MaintenancePreparationError("可信环境材料不完整")
        if plan.target_id != "environment" and environments:
            raise MaintenancePreparationError("Agent 材料不得携带环境值")
        managed_part_provider = getattr(provider, "managed_fingerprint_part", None)
        if plan.target_id != "environment" and not callable(managed_part_provider):
            raise MaintenancePreparationError("Agent 材料缺少受管指纹验证器")
        before_parts: list[str] = []
        after_parts: list[str] = []
        for index, leaf_id in enumerate(leaf_order):
            leaf = leaves[leaf_id]
            before_bytes = b"<missing>" if leaf.existence == "missing" else leaf.before_bytes
            if before_bytes is None:
                raise MaintenancePreparationError("事务前材料无效")
            if leaf.existence == "present" and leaf.before_bytes is None:
                raise MaintenancePreparationError("事务前材料无效")
            if hashlib.sha256(leaf.expected_bytes).hexdigest() != leaf.expected_hash:
                raise MaintenancePreparationError("期望材料摘要无效")
            if plan.target_id != "environment":
                if leaf.before_hash is not None and hashlib.sha256(before_bytes).hexdigest() != leaf.before_hash:
                    raise MaintenancePreparationError("事务前摘要无效")
                try:
                    before_part = managed_part_provider(plan.target_id, leaf_id, None if leaf.existence == "missing" else leaf.before_bytes)
                    after_part = managed_part_provider(plan.target_id, leaf_id, leaf.expected_bytes)
                except Exception as error:
                    raise MaintenancePreparationError("Agent 受管材料无法安全验证") from error
                if not _is_valid_managed_fingerprint_part(leaf_id, before_part) or not _is_valid_managed_fingerprint_part(leaf_id, after_part):
                    raise MaintenancePreparationError("Agent 受管摘要无效")
                before_parts.append(before_part)
                after_parts.append(after_part)
                continue
            name = names[index]
            environment = environments[name]
            try:
                expected_value = leaf.expected_bytes.decode("utf-8")
                before_value = None if leaf.existence == "missing" else leaf.before_bytes.decode("utf-8")
            except UnicodeError as error:
                raise MaintenancePreparationError("环境材料编码无效") from error
            if "\x00" in expected_value or (before_value is not None and "\x00" in before_value):
                raise MaintenancePreparationError("环境材料包含无效字符")
            material_value = None if environment.state == "missing" else environment.value
            if material_value != before_value:
                raise MaintenancePreparationError("环境旧值与叶子材料不一致")
            before_parts.append(f"{leaf_id}:{hashlib.sha256(before_bytes).hexdigest()}")
            after_parts.append(f"{leaf_id}:{hashlib.sha256(expected_value.encode('utf-8')).hexdigest()}")
        before_fingerprint = hashlib.sha256("\n".join(before_parts).encode("utf-8")).hexdigest()
        after_fingerprint = hashlib.sha256("\n".join(after_parts).encode("utf-8")).hexdigest()
        if before_fingerprint != plan.before_fingerprint or after_fingerprint != plan.after_fingerprint:
            raise MaintenancePreparationError("环境材料与预览指纹不匹配")


__all__ = ["EnvironmentMaterialProvider", "MaintenanceMaterialProvider", "MaintenancePreparationError", "MaintenancePreparationService", "PreparedMaintenance", "StagedMaintenancePreview"]
