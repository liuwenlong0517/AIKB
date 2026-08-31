"""Windows 用户环境目标的维护恢复平台适配器。

本适配器只接受逻辑 change/target/leaf 和 ``RecoveryStep``。路径来自已经验证的
只读适配器内部固定映射，正文来自私有材料存储；任何边界、摘要、重解析点或
第三方状态不满足时都 fail-closed。它不接受浏览器参数，不返回路径/正文，且
在非 Windows 主机上拒绝构造，避免测试或 macOS 误报支持。
"""

from __future__ import annotations

import hashlib
import os
from typing import Callable, Mapping

from ...core.maintenance_materials import MaintenanceMaterialStore
from ...core.maintenance_recovery import (
    EnvironmentObservation,
    RecoveryDecision,
    RecoveryStep,
)
from ...core.maintenance_targets import MAINTENANCE_LEAVES_BY_TARGET, MAINTENANCE_TARGET_REGISTRY, validate_logical_id
from ...platform.maintenance import MaintenanceStepResult
from .maintenance_readonly import WindowsMaintenanceAdapter


class WindowsMaintenanceRecoveryError(RuntimeError):
    """恢复边界、材料完整性或 Windows 写入失败；错误不回显底层信息。"""


def _sha256(value: bytes) -> str:
    """仅计算摘要，正文不进入返回模型。"""

    return hashlib.sha256(value).hexdigest()


class WindowsMaintenanceRecoveryPlatform:
    """在固定 Windows 用户边界内观察并补偿维护叶子。"""

    def __init__(
        self,
        readonly: WindowsMaintenanceAdapter,
        materials: MaintenanceMaterialStore,
        *,
        environment_reader: Callable[[], Mapping[str, str | None]] | None = None,
        environment_writer: Callable[[str, str | None], None] | None = None,
        environment_broadcaster: Callable[[], bool] | None = None,
    ) -> None:
        """绑定只读路径解析器和私有材料；不访问文件或注册表。"""

        if os.name != "nt":
            raise WindowsMaintenanceRecoveryError("当前平台不支持 Windows 恢复")
        if not isinstance(readonly, WindowsMaintenanceAdapter):
            raise WindowsMaintenanceRecoveryError("只读平台适配器类型无效")
        if not all(callable(getattr(materials, name, None)) for name in ("read_leaf", "read_environment")):
            raise WindowsMaintenanceRecoveryError("私有材料存储类型无效")
        self._readonly = readonly
        self._materials = materials
        self._environment_reader = environment_reader or readonly._read_environment
        self._environment_writer = environment_writer or self._write_user_environment
        self._environment_broadcaster = environment_broadcaster or self._broadcast_environment
        if not callable(self._environment_reader) or not callable(self._environment_writer) or not callable(self._environment_broadcaster):
            raise WindowsMaintenanceRecoveryError("环境读写接口无效")

    def observe_leaf(
        self,
        change_id: str,
        target_id: str,
        leaf_id: str,
    ) -> EnvironmentObservation:
        """观察固定叶子的存在语义和摘要，不返回路径、正文或环境值。"""

        self._validate_ids(change_id, target_id, leaf_id)
        if target_id != "environment":
            raise WindowsMaintenanceRecoveryError("当前波次仅支持环境目标")
        values = self._read_environment()
        name = self._environment_name(leaf_id)
        value = values.get(name)
        if value is None:
            return EnvironmentObservation("missing", None)
        return EnvironmentObservation("empty" if value == "" else "value", _sha256(value.encode("utf-8")))

    def recover_step(self, change_id: str, target_id: str, step: RecoveryStep) -> MaintenanceStepResult:
        """按步骤内逐叶子决定恢复或删除，并验证每次操作前后的摘要。"""

        self._validate_ids(change_id, target_id, None)
        if not isinstance(step, RecoveryStep):
            raise WindowsMaintenanceRecoveryError("恢复步骤类型无效")
        if target_id != "environment":
            raise WindowsMaintenanceRecoveryError("当前波次仅支持环境目标")
        if tuple(step.leaf_ids) != tuple(MAINTENANCE_LEAVES_BY_TARGET[target_id]):
            raise WindowsMaintenanceRecoveryError("恢复步骤叶子与环境目标不匹配")
        if step.step_id != "write_environment":
            raise WindowsMaintenanceRecoveryError("恢复步骤不属于目标")
        try:
            # 当前正式波次仅允许环境目标；两个固定变量必须作为一个逻辑组处理。
            self._recover_environment_group(change_id, step)
            return MaintenanceStepResult(change_id, target_id, step.step_id, True, "rolled_back")
        except WindowsMaintenanceRecoveryError:
            raise
        except Exception as error:
            raise WindowsMaintenanceRecoveryError("恢复步骤失败") from error

    def _recover_environment_group(self, change_id: str, step: RecoveryStep) -> None:
        """两键先预检后成组写入，失败时把已改键恢复到调用前 expected 状态。"""

        expected_names = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
        if tuple(self._environment_name(item.leaf_id) for item in step.leaf_decisions) != expected_names:
            raise WindowsMaintenanceRecoveryError("环境叶子顺序无效")
        current_values = dict(self._read_environment())
        desired: dict[str, str | None] = {}
        for decision in step.leaf_decisions:
            name = self._environment_name(decision.leaf_id)
            current = self.observe_leaf(change_id, "environment", decision.leaf_id)
            material = self._materials.read_environment(change_id, name)
            leaf = self._materials.read_leaf(change_id, decision.leaf_id)
            if not isinstance(current, EnvironmentObservation) or current.state == "missing" or current.value_hash != leaf.expected_hash:
                raise WindowsMaintenanceRecoveryError("环境当前摘要不匹配")
            if decision.decision is RecoveryDecision.NOOP:
                desired[name] = current_values[name]
            elif decision.decision is RecoveryDecision.REMOVE_CREATED and material.state == "missing":
                desired[name] = None
            elif decision.decision is RecoveryDecision.RESTORE_BEFORE and material.state in {"empty", "value"}:
                desired[name] = material.value
            elif decision.decision is RecoveryDecision.RESTORE_BEFORE and material.state == "missing":
                desired[name] = None
            else:
                raise WindowsMaintenanceRecoveryError("环境恢复决定与材料不一致")
        attempted: list[str] = []
        try:
            for name in expected_names:
                if desired[name] != current_values[name]:
                    attempted.append(name)
                    self._environment_writer(name, desired[name])
            if attempted and not self._environment_broadcaster():
                raise WindowsMaintenanceRecoveryError("环境变化广播失败")
            self._verify_environment_values(expected_names, desired)
        except Exception as error:
            # 广播/验证失败也回到调用前 expected；若补偿无法证明，向上层报告
            # 安全异常并由事务协调器进入人工恢复。
            try:
                for name in attempted:
                    self._environment_writer(name, current_values[name])
                if attempted and not self._environment_broadcaster():
                    raise WindowsMaintenanceRecoveryError("环境补偿广播失败")
                self._verify_environment_values(expected_names, current_values)
            except Exception as rollback_error:
                raise WindowsMaintenanceRecoveryError("环境成组补偿无法证明") from rollback_error
            raise WindowsMaintenanceRecoveryError("环境成组恢复失败") from error

    def _verify_environment_values(self, names: tuple[str, ...], expected: Mapping[str, str | None]) -> None:
        """全量验证两个固定变量的 missing/empty/value 及摘要，不返回值。"""

        actual = self._read_environment()
        for name in names:
            if actual.get(name) != expected[name]:
                raise WindowsMaintenanceRecoveryError("环境成组验证失败")

    def _read_environment(self) -> Mapping[str, str | None]:
        """只读取两个固定环境键并校验集合，不枚举其他变量。"""

        values = dict(self._environment_reader())
        if set(values) != {"AIKB_HOME", "AIKB_KNOWLEDGE_HOME"}:
            raise WindowsMaintenanceRecoveryError("环境读取结果无效")
        return values

    @staticmethod
    def _validate_ids(change_id: str, target_id: str, leaf_id: str | None) -> None:
        """只接受静态逻辑 ID，不接受路径或动态目标。"""

        if not isinstance(change_id, str) or "/" in change_id or "\\" in change_id:
            raise WindowsMaintenanceRecoveryError("逻辑变更 ID 无效")
        try:
            validate_logical_id(change_id, "change_id")
        except Exception as error:
            raise WindowsMaintenanceRecoveryError("逻辑变更 ID 无效") from error
        if MAINTENANCE_TARGET_REGISTRY.get(target_id) is None:
            raise WindowsMaintenanceRecoveryError("维护目标无效")
        if leaf_id is not None and leaf_id not in MAINTENANCE_LEAVES_BY_TARGET[target_id]:
            raise WindowsMaintenanceRecoveryError("维护叶子无效")

    @staticmethod
    def _environment_name(leaf_id: str) -> str:
        """由固定环境叶子映射固定注册表名称。"""

        mapping = {
            "user_environment.aikb_home": "AIKB_HOME",
            "user_environment.aikb_knowledge_home": "AIKB_KNOWLEDGE_HOME",
        }
        try:
            return mapping[leaf_id]
        except KeyError as error:
            raise WindowsMaintenanceRecoveryError("环境叶子未声明") from error

    @staticmethod
    def _write_user_environment(name: str, value: str | None) -> None:
        """仅写 HKCU Environment 的两个已验证名称，不枚举或修改其他值。"""

        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)

    @staticmethod
    def _broadcast_environment() -> bool:
        """通知当前桌面环境刷新用户变量；失败交由事务执行器补偿。"""

        try:
            import ctypes

            result = ctypes.c_ulong()
            sent = ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
            )
            # 对 WM_SETTINGCHANGE，lpdwResult 的内容可能为 0；成功语义只
            # 由 SendMessageTimeoutW 本身的非零返回值决定。
            return bool(sent)
        except Exception:
            return False


__all__ = ["WindowsMaintenanceRecoveryError", "WindowsMaintenanceRecoveryPlatform"]
