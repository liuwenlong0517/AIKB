"""将可信 environment 预览安全转换为 prepared 事务。

本模块只接受服务端 ``MaintenancePlan``、检查状态和可信材料提供器；浏览器不会
提供路径、环境值或期望正文。目录事实先创建、私有材料后准备，任何中途失败都
保留损坏现场供恢复扫描阻断，绝不删除或伪造可执行事务。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol

from ..platform.maintenance import MaintenancePlan
from .actions import ConfirmationTokenService
from .maintenance_changes import MaintenanceChange, MaintenanceLeafState
from .maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial, MaintenanceMaterialStore
from .maintenance_targets import MaintenanceTargetStatus
from .maintenance_transaction_store import MaintenanceTransactionStore


class MaintenancePreparationError(ValueError):
    """预览过期、状态冲突、可信材料不完整或事务准备失败。"""


class EnvironmentMaterialProvider(Protocol):
    """服务端可信 environment 材料提供器，不接受浏览器正文。"""

    def capture_environment(self, plan: MaintenancePlan) -> tuple[MaintenanceTargetStatus, Mapping[str, MaintenanceLeafMaterial], Mapping[str, MaintenanceEnvironmentMaterial]]: ...


@dataclass(frozen=True)
class PreparedMaintenance:
    """prepared 事务和一次性确认令牌的安全包络，不包含材料正文。"""

    change: MaintenanceChange
    confirmation_token: str

    def to_dict(self) -> dict[str, object]:
        """返回安全事务摘要和令牌。"""
        return {"change": self.change.to_public_dict(), "confirmation_token": self.confirmation_token}


class MaintenancePreparationService:
    """只支持 environment 的预览转事务服务。"""

    def __init__(self, transactions: MaintenanceTransactionStore, material_store_factory: object, tokens: ConfirmationTokenService) -> None:
        self._transactions = transactions
        if not callable(material_store_factory):
            raise MaintenancePreparationError("材料存储工厂接口无效")
        self._material_store_factory = material_store_factory
        self._tokens = tokens

    def prepare(self, plan: MaintenancePlan, status: MaintenanceTargetStatus, provider: EnvironmentMaterialProvider) -> PreparedMaintenance:
        """重新绑定当前基线后创建事实目录、私有材料并签发单次令牌。"""
        if plan.target_id != "environment" or status.target_id != "environment":
            raise MaintenancePreparationError("仅支持 environment 事务")
        status_value = status.status.value if hasattr(status.status, "value") else status.status
        if status_value not in {"missing", "drifted"} or status.base_fingerprint != plan.before_fingerprint:
            raise MaintenancePreparationError("预览基线已变化或当前状态不可修复")
        try:
            fresh_status, leaves, environments = provider.capture_environment(plan)
            if fresh_status.target_id != "environment" or fresh_status.base_fingerprint != plan.before_fingerprint:
                raise MaintenancePreparationError("当前环境状态已变化")
            if tuple(leaves) != ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"):
                raise MaintenancePreparationError("可信叶子材料不完整")
            self._validate_captured_material(plan, leaves, environments)
            change_id = f"maintenance-{uuid.uuid4().hex}"
            leaf_order = ("user_environment.aikb_home", "user_environment.aikb_knowledge_home")
            leaf_states = tuple(MaintenanceLeafState(key, leaves[key].existence, leaves[key].before_hash, leaves[key].expected_hash) for key in leaf_order)
            now = datetime.now(timezone.utc)
            created_at = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
            expires_at = (now + timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            change = MaintenanceChange(
                change_id=change_id, target_id="environment", action_id="maintenance.environment.update", risk_level="user_config_write",
                status="prepared", base_fingerprint=plan.before_fingerprint, before_fingerprint=plan.before_fingerprint, after_fingerprint=plan.after_fingerprint, step_summary=tuple(step.step_id for step in plan.steps), preview_digest=plan.preview_digest, created_at=created_at, expires_at=expires_at, updated_at=created_at, leaf_states=leaf_states,
            )
            self._transactions.create(change)
            materials = self._material_store_factory(self._transactions)
            if not callable(getattr(materials, "prepare", None)):
                raise MaintenancePreparationError("材料存储接口无效")
            materials.prepare(change_id, "environment", leaves, environments)
            token = self._tokens.issue(action_id=change.action_id, parameters={"change_id": change_id}, risk_level=change.risk_level, preview_digest=change.preview_digest)
            return PreparedMaintenance(change, token)
        except MaintenancePreparationError:
            raise
        except Exception as error:
            raise MaintenancePreparationError("维护事务准备失败") from error

    @staticmethod
    def _validate_captured_material(
        plan: MaintenancePlan,
        leaves: Mapping[str, MaintenanceLeafMaterial],
        environments: Mapping[str, MaintenanceEnvironmentMaterial],
    ) -> None:
        """用材料正文重算前后整体指纹，拒绝 provider 仅口头声明绑定。"""
        leaf_order = ("user_environment.aikb_home", "user_environment.aikb_knowledge_home")
        names = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
        if tuple(environments) != names:
            raise MaintenancePreparationError("可信环境材料不完整")
        before_parts: list[str] = []
        after_parts: list[str] = []
        for leaf_id, name in zip(leaf_order, names):
            leaf = leaves[leaf_id]
            environment = environments[name]
            before_bytes = b"<missing>" if leaf.existence == "missing" else leaf.before_bytes
            if before_bytes is None:
                raise MaintenancePreparationError("事务前材料无效")
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


__all__ = ["EnvironmentMaterialProvider", "MaintenancePreparationError", "MaintenancePreparationService", "PreparedMaintenance"]
