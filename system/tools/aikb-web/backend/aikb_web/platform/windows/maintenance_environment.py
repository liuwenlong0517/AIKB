"""Windows environment 目标的 MaintenanceExecutor 运行适配器。

本模块只接受服务端事务、私有材料和固定环境读写回调，不接受浏览器正文、路径
或命令。两个 AIKB 用户变量始终作为同一逻辑组处理；任一写入、广播或验证失败
都会尽力恢复调用前状态，并由上层事务决定是否进入人工恢复。
"""

from __future__ import annotations

import os
import hashlib
from typing import Callable, Mapping

from ...core.maintenance_changes import MaintenanceChange
from ...core.maintenance_materials import MaintenanceMaterialManifest
from ...core.maintenance_recovery import EnvironmentObservation
from ...core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY, validate_logical_id
from ...platform.maintenance import MaintenanceStep, MaintenanceStepResult, MaintenanceVerification


class WindowsEnvironmentExecutionError(RuntimeError):
    """环境执行失败或安全边界不满足；不携带路径、正文和底层异常。"""


_ENV_NAMES = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
_ENV_LEAVES = ("user_environment.aikb_home", "user_environment.aikb_knowledge_home")


class WindowsEnvironmentMaintenanceAdapter:
    """实现 environment 目标的 preflight/backup/apply/verify/rollback 语义。"""

    def __init__(
        self,
        transaction_store: object,
        material_store: object,
        *,
        environment_reader: Callable[[], Mapping[str, str | None]],
        environment_writer: Callable[[str, str | None], None] | None = None,
        environment_broadcaster: Callable[[], bool] | None = None,
    ) -> None:
        """绑定注入 fixture；构造阶段不读取事务、环境或私有材料。"""

        if os.name != "nt":
            raise WindowsEnvironmentExecutionError("当前平台不支持 Windows 环境执行")
        if not callable(getattr(transaction_store, "load", None)):
            raise WindowsEnvironmentExecutionError("事务事实源接口无效")
        # 执行器只依赖按 change_id 读取已完整校验的 manifest；read_leaf 等
        # 细粒度方法属于材料存储的可选扩展，不能把惰性 bootstrap 实现排除在外。
        if not callable(getattr(material_store, "load", None)):
            raise WindowsEnvironmentExecutionError("材料存储接口无效")
        environment_writer = environment_writer or self._write_user_environment
        environment_broadcaster = environment_broadcaster or self._broadcast_environment
        if not callable(environment_reader) or not callable(environment_writer) or not callable(environment_broadcaster):
            raise WindowsEnvironmentExecutionError("环境读写接口无效")
        self._transactions = transaction_store
        self._materials = material_store
        self._reader = environment_reader
        self._writer = environment_writer
        self._broadcaster = environment_broadcaster

    def apply_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """执行固定 environment 步骤；preflight/backup 只读，写步骤成组更新 expected。"""

        transaction = self._transaction(change_id, target_id)
        if not isinstance(step, MaintenanceStep) or step.step_id not in transaction.step_summary:
            raise WindowsEnvironmentExecutionError("维护步骤无效")
        manifest = self._validate_material(transaction)
        if step.step_id in {"preflight", "backup"}:
            self._assert_before(transaction)
        elif step.step_id == "write_environment":
            self._write_group(transaction, self._expected_values(manifest), self._before_values(transaction))
        else:
            raise WindowsEnvironmentExecutionError("当前适配器仅支持环境步骤")
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "applied")

    def verify(self, change_id: str, target_id: str) -> MaintenanceVerification:
        """只读验证两个变量等于 expected，并返回需要重启的固定结果。"""

        transaction = self._transaction(change_id, target_id)
        manifest = self._validate_material(transaction)
        current = self._read_values()
        expected = self._expected_values(manifest)
        if current != expected or self._fingerprint(expected) != transaction.after_fingerprint:
            raise WindowsEnvironmentExecutionError("环境期望状态未满足")
        return MaintenanceVerification(change_id, target_id, "restart_required", True, transaction.after_fingerprint)

    def rollback_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> MaintenanceStepResult:
        """将两个变量成组恢复为材料记录的 missing/empty/value before 状态。"""

        transaction = self._transaction(change_id, target_id)
        if not isinstance(step, MaintenanceStep) or step.step_id != "write_environment":
            raise WindowsEnvironmentExecutionError("回滚步骤无效")
        manifest = self._validate_material(transaction)
        self._write_group(transaction, self._before_values(transaction), self._expected_values(manifest))
        return MaintenanceStepResult(change_id, target_id, step.step_id, True, "rolled_back")

    def _transaction(self, change_id: str, target_id: str) -> MaintenanceChange:
        """读取并校验固定 environment 事务，不接受任意目标。"""

        if not isinstance(change_id, str):
            raise WindowsEnvironmentExecutionError("变更 ID 无效")
        try:
            validate_logical_id(change_id, "change_id")
        except (TypeError, ValueError):
            raise WindowsEnvironmentExecutionError("变更 ID 无效") from None
        if target_id != "environment" or MAINTENANCE_TARGET_REGISTRY.get(target_id) is None:
            raise WindowsEnvironmentExecutionError("当前适配器仅支持环境目标")
        try:
            transaction = self._transactions.load(change_id)
        except Exception as error:
            raise WindowsEnvironmentExecutionError("事务读取失败") from error
        if not isinstance(transaction, MaintenanceChange) or transaction.change_id != change_id or transaction.target_id != target_id:
            raise WindowsEnvironmentExecutionError("事务绑定无效")
        return transaction

    def _validate_material(self, transaction: MaintenanceChange) -> MaintenanceMaterialManifest:
        """严格校验真实 manifest 与事务叶子/环境名称和存在语义一致。"""

        try:
            manifest = self._materials.load(transaction.change_id)
        except Exception as error:
            raise WindowsEnvironmentExecutionError("材料读取失败") from error
        if not isinstance(manifest, MaintenanceMaterialManifest) or manifest.change_id != transaction.change_id or manifest.target_id != "environment":
            raise WindowsEnvironmentExecutionError("材料绑定无效")
        if tuple(item.leaf_id for item in manifest.leaves) != _ENV_LEAVES or tuple(item.name for item in manifest.environments) != _ENV_NAMES:
            raise WindowsEnvironmentExecutionError("环境材料叶子无效")
        for item, leaf in zip(manifest.leaves, transaction.leaf_states):
            if (item.existence, item.before_hash, item.expected_hash) != (leaf.existence, leaf.before_hash, leaf.expected_hash):
                raise WindowsEnvironmentExecutionError("环境材料摘要不匹配")
        return manifest

    def _read_values(self) -> dict[str, str | None]:
        """只读取两个固定键，缺失保留为 None，拒绝其他键或非字符串。"""

        try:
            values = dict(self._reader())
        except Exception as error:
            raise WindowsEnvironmentExecutionError("环境读取失败") from error
        if set(values) != set(_ENV_NAMES) or any(value is not None and not isinstance(value, str) for value in values.values()):
            raise WindowsEnvironmentExecutionError("环境读取结果无效")
        return values

    def _before_values(self, transaction: MaintenanceChange) -> dict[str, str | None]:
        """从私有环境材料恢复 missing/empty/value 旧值，不读取公开输入。"""

        manifest = self._validate_material(transaction)
        values: dict[str, str | None] = {}
        for item in manifest.environments:
            values[item.name] = None if item.state == "missing" else item.value
        return values

    def _expected_values(self, manifest: MaintenanceMaterialManifest) -> dict[str, str]:
        """从两个固定叶子的 expected_bytes 解码期望值，禁止独立自由源。"""

        values: dict[str, str] = {}
        for leaf, name in zip(manifest.leaves, _ENV_NAMES):
            try:
                value = leaf.expected_bytes.decode("utf-8")
            except (UnicodeDecodeError, AttributeError) as error:
                raise WindowsEnvironmentExecutionError("环境期望材料不是 UTF-8") from error
            if "\x00" in value or hashlib.sha256(leaf.expected_bytes).hexdigest() != leaf.expected_hash:
                raise WindowsEnvironmentExecutionError("环境期望材料摘要无效")
            values[name] = value
        if tuple(values) != _ENV_NAMES:
            raise WindowsEnvironmentExecutionError("环境期望材料不完整")
        return values

    @staticmethod
    def _fingerprint(values: Mapping[str, str]) -> str:
        """按只读 environment 目标算法计算整体 after fingerprint。"""

        parts = [
            f"{leaf}:{hashlib.sha256(values[name].encode('utf-8')).hexdigest()}"
            for leaf, name in zip(_ENV_LEAVES, _ENV_NAMES)
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _assert_before(self, transaction: MaintenanceChange) -> None:
        """preflight/backup 阶段确认两个变量仍为事务前状态，且不写入。"""

        before = self._before_values(transaction)
        if self._read_values() != before:
            raise WindowsEnvironmentExecutionError("环境事务前状态已变化")

    def _write_group(self, transaction: MaintenanceChange, desired: Mapping[str, str | None], rollback: Mapping[str, str | None]) -> None:
        """先拒绝第三方漂移，再写两键；失败时恢复已改键并验证 rollback。"""

        current = self._read_values()
        if current == desired:
            return
        if current != rollback and current != desired:
            raise WindowsEnvironmentExecutionError("环境当前状态冲突")
        changed: list[str] = []
        try:
            for name in _ENV_NAMES:
                if current.get(name) != desired[name]:
                    changed.append(name)
                    self._writer(name, desired[name])
            if changed and not self._broadcaster():
                raise WindowsEnvironmentExecutionError("环境变化广播失败")
            if self._read_values() != dict(desired):
                raise WindowsEnvironmentExecutionError("环境写入验证失败")
        except Exception as error:
            try:
                for name in changed:
                    self._writer(name, rollback[name])
                if changed and not self._broadcaster():
                    raise WindowsEnvironmentExecutionError("环境补偿广播失败")
                if self._read_values() != dict(rollback):
                    raise WindowsEnvironmentExecutionError("环境补偿验证失败")
            except Exception as rollback_error:
                raise WindowsEnvironmentExecutionError("环境成组恢复无法证明") from rollback_error
            raise WindowsEnvironmentExecutionError("环境成组写入失败") from error

    @staticmethod
    def _write_user_environment(name: str, value: str | None) -> None:
        """仅写入 HKCU 的两个固定环境名称，并保留已有字符串值类型。

        ``REG_SZ`` 与 ``REG_EXPAND_SZ`` 都是受支持的字符串类型；缺失值采用
        ``REG_EXPAND_SZ``，以保持默认配置对展开变量的兼容性。其他注册表类型
        不属于本适配器的目标，直接拒绝，避免把无关值转换成字符串。
        """

        if name not in _ENV_NAMES:
            raise WindowsEnvironmentExecutionError("环境名称不受支持")
        import winreg

        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, access) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value_type = winreg.REG_EXPAND_SZ
                try:
                    _current_value, current_type = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    # 新建值默认可展开字符串；这是唯一允许的缺失值降级。
                    pass
                else:
                    if current_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        raise WindowsEnvironmentExecutionError("环境值注册表类型不受支持")
                    value_type = current_type
                winreg.SetValueEx(key, name, 0, value_type, value)

    @staticmethod
    def _broadcast_environment() -> bool:
        """广播环境刷新；成功只由 API 返回值决定。"""

        try:
            import ctypes

            result = ctypes.c_ulong()
            sent = ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
            return bool(sent)
        except Exception:
            return False


__all__ = ["WindowsEnvironmentExecutionError", "WindowsEnvironmentMaintenanceAdapter"]
