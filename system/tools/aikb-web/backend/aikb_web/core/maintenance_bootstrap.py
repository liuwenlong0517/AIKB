"""维护启动恢复的生产默认组合根。

本模块只装配已经验证的 Windows 只读适配器、事务/材料事实源、审计证据适配器、
共享 gate 与共享写锁。材料仅在扫描发现事务后惰性绑定，因此没有事务运行面时
仍执行真实空扫描且不创建目录；依赖不完整时返回 fail-closed 占位协调器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .maintenance_lock import MaintenanceWriteLock
from .maintenance_materials import MaintenanceMaterialStore
from .maintenance_recovery_evidence import MaintenanceRecoveryEvidenceAdapter
from .maintenance_recovery_gate import MaintenanceRecoveryGate
from .maintenance_startup_recovery import MaintenanceStartupRecovery
from .maintenance_transaction_store import MaintenanceTransactionStore
from .maintenance_preparation import MaintenancePreparationService
from .maintenance_execution import MaintenanceExecutor
from .maintenance_task import MaintenanceAuditAdapter, MaintenanceTaskCoordinator
from ..platform.maintenance import MaintenanceStep
from ..platform.windows.maintenance_agents import WindowsAgentMaintenanceAdapter, WindowsAgentProbeRunner
from ..platform.windows.maintenance_environment import WindowsEnvironmentMaintenanceAdapter
from ..platform.windows.maintenance_recovery import WindowsMaintenanceRecoveryPlatform
from ..platform.windows.maintenance_readonly import WindowsMaintenanceAdapter


class _UnavailableStartupRecovery:
    """生产依赖缺失时的 fail-closed 占位。"""

    def __init__(self, gate: MaintenanceRecoveryGate) -> None:
        self._gate = gate

    def recover_all(self) -> tuple[object, ...]:
        """固定失败，交由 lifespan 调用 gate.block 保持写入阻断。"""
        raise RuntimeError("维护启动恢复依赖不可用")


class _LazyMaintenanceMaterials:
    """在真实事务被扫描到后才校验固定材料根，空启动保持零副作用。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _store(self) -> MaintenanceMaterialStore:
        """每次读取重新验证普通目录与重解析点边界，拒绝启动后替换。"""
        return MaintenanceMaterialStore(self._root)

    def load(self, change_id: str) -> Any:
        return self._store().load(change_id)

    def read_leaf(self, change_id: str, leaf_id: str) -> Any:
        return self._store().read_leaf(change_id, leaf_id)

    def read_environment(self, change_id: str, name: str) -> Any:
        return self._store().read_environment(change_id, name)


class _WindowsMaintenanceDispatchAdapter:
    """按固定 target_id 把事务步骤分派到环境或 Agent 专属适配器。"""

    def __init__(self, readonly: Any, environment: Any, agents: Mapping[str, Any]) -> None:
        """绑定已验证适配器；构造阶段不读取或修改目标。"""
        self._readonly = readonly
        self._environment = environment
        self._agents = dict(agents)

    def _target(self, target_id: str) -> Any:
        """仅从代码内固定映射选择适配器。"""
        if target_id == "environment":
            return self._environment
        try:
            return self._agents[target_id]
        except KeyError as error:
            raise ValueError("维护目标未装配") from error

    def inspect(self, target_id: str) -> Any:
        """转发只读目标检查；目标解析仍由固定 Windows 适配器完成。"""
        return self._readonly.inspect(target_id)

    def plan(self, target_id: str, inspection: Any) -> Any:
        """转发无副作用预览规划，不创建事务材料。"""
        return self._readonly.plan(target_id, inspection)

    def capture(self, plan: Any) -> Any:
        """按目标捕获私有材料，返回值只交给准备服务。"""
        # environment 的捕获属于只读适配器：它负责从当前环境生成受指纹约束的
        # 私有材料；执行适配器只消费 prepared 事务，不能被误当成材料提供器。
        if plan.target_id == "environment":
            return self._readonly.capture_environment(plan)
        return self._target(plan.target_id).capture_agent(plan)

    def managed_fingerprint_part(self, target_id: str, leaf_id: str, raw: bytes | None) -> str:
        """委托 Agent 专属适配器验证给定正文中的受管摘要，保持生产边界固定。"""
        if target_id == "environment":
            raise ValueError("环境目标没有 Agent 受管摘要")
        verifier = self._target(target_id)
        method = getattr(verifier, "managed_fingerprint_part", None)
        if not callable(method):
            raise ValueError("Agent 受管摘要验证器未装配")
        return method(target_id, leaf_id, raw)

    def apply_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> Any:
        """转发已校验的固定写步骤，不接受浏览器自由参数。"""
        return self._target(target_id).apply_step(change_id, target_id, step)

    def verify(self, change_id: str, target_id: str) -> Any:
        """转发目标安全复核，并仅返回固定验证模型。"""
        return self._target(target_id).verify(change_id, target_id)

    def rollback_step(self, change_id: str, target_id: str, step: MaintenanceStep) -> Any:
        """转发逆序补偿步骤；第三方变化由专属适配器拒绝覆盖。"""
        return self._target(target_id).rollback_step(change_id, target_id, step)


class _WindowsRecoveryDispatchAdapter:
    """按事务目标选择 environment 或 Agent 的启动恢复适配器。"""

    def __init__(self, environment: Any, agents: Mapping[str, Any]) -> None:
        """绑定恢复适配器；扫描和恢复仍由核心协调器持锁驱动。"""
        self._environment = environment
        self._agents = dict(agents)

    def _target(self, target_id: str) -> Any:
        if target_id == "environment":
            return self._environment
        try:
            return self._agents[target_id]
        except KeyError as error:
            raise ValueError("恢复目标未装配") from error

    def observe_leaf(self, change_id: str, target_id: str, leaf_id: str) -> Any:
        """读取固定逻辑叶子观察，不返回物理路径或正文。"""
        return self._target(target_id).observe_leaf(change_id, target_id, leaf_id)

    def recover_step(self, change_id: str, target_id: str, step: Any) -> Any:
        """转发启动恢复步骤，保持核心恢复门禁和审计绑定。"""
        return self._target(target_id).recover_step(change_id, target_id, step)


def _maintenance_material_factory(transactions: MaintenanceTransactionStore) -> MaintenanceMaterialStore:
    """在事务目录已创建后构造材料存储，避免预览/启动阶段产生目录副作用。"""
    root = transactions._runtime_root(create=False)  # type: ignore[attr-defined]
    return MaintenanceMaterialStore(root)


def build_default_maintenance_services(
    settings: Any,
    adapter: Any,
    gateway: Any,
    gate: MaintenanceRecoveryGate,
    lock: MaintenanceWriteLock,
    tokens: Any,
) -> tuple[Any, Any, Any] | None:
    """装配三固定目标的 preparation/executor/coordinator；不读取或写入目标。

    生产装配使用只读适配器解析固定边界，再把写操作分派给 environment 或
    Agent 专属适配器；真实目标写入仅会在用户明确调用 apply 后由 worker 执行。
    """
    workspace = getattr(settings, "workspace_root", None)
    if not isinstance(workspace, Path):
        return None
    if not all(callable(getattr(adapter, name, None)) for name in ("capture_environment", "_read_environment")):
        return None
    try:
        transactions = MaintenanceTransactionStore(workspace)
        preparation = MaintenancePreparationService(transactions, _maintenance_material_factory, tokens)
        materials = _LazyMaintenanceMaterials(workspace / "runtime" / "web" / "maintenance-transactions")
        environment = WindowsEnvironmentMaintenanceAdapter(
            transactions,
            materials,
            environment_reader=adapter._read_environment,
        )
        # Agent apply 必须经过固定 MCP/生命周期/UTF-8 probe；runner 内部只生成
        # 服务端固定命令，恢复适配器不复用它，避免启动恢复执行外部进程。
        probe_runner = WindowsAgentProbeRunner(adapter)
        agents = {
            target_id: WindowsAgentMaintenanceAdapter(adapter, materials, transactions, probe_runner=probe_runner)
            for target_id in ("agent.codex", "agent.claude-code")
        }
        platform_adapter = _WindowsMaintenanceDispatchAdapter(adapter, environment, agents)
        audit = MaintenanceAuditAdapter(getattr(gateway, "web_audit_write", None))
        executor = MaintenanceExecutor(
            transactions,
            platform_adapter,
            workspace,
            materials,
            audit,
            gate,
            lock=lock,
        )
        coordinator = MaintenanceTaskCoordinator(
            executor,
            transactions=transactions,
            token_service=tokens,
            workspace_root=workspace,
            audit_sink=getattr(gateway, "web_audit_write", None),
            recovery_gate=gate,
        )
        return preparation, executor, coordinator
    except Exception:
        return None


def build_default_maintenance_recovery(
    settings: Any,
    readonly: Any,
    gateway: Any,
    gate: MaintenanceRecoveryGate,
    lock: MaintenanceWriteLock,
) -> Any | None:
    """按可信依赖装配默认恢复器；非 Windows/不完整依赖返回 None。"""
    if not isinstance(gate, MaintenanceRecoveryGate) or not isinstance(lock, MaintenanceWriteLock):
        return _UnavailableStartupRecovery(gate)
    if not isinstance(readonly, WindowsMaintenanceAdapter):
        return None
    workspace = getattr(settings, "workspace_root", None)
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        return _UnavailableStartupRecovery(gate)
    transaction_root = workspace / "runtime" / "web" / "maintenance-transactions"
    try:
        transactions = MaintenanceTransactionStore(workspace)
        materials = _LazyMaintenanceMaterials(transaction_root)
        audit_store = gateway._audit() if callable(getattr(gateway, "_audit", None)) else None
        if audit_store is None:
            return _UnavailableStartupRecovery(gate)
        evidence = MaintenanceRecoveryEvidenceAdapter(transactions, audit_store)
        environment = WindowsMaintenanceRecoveryPlatform(readonly, materials)
        agents = {
            target_id: WindowsAgentMaintenanceAdapter(readonly, materials, transactions)
            for target_id in ("agent.codex", "agent.claude-code")
        }
        platform = _WindowsRecoveryDispatchAdapter(environment, agents)
        return MaintenanceStartupRecovery(transactions, materials, platform, evidence, gate, lock)
    except Exception:
        return _UnavailableStartupRecovery(gate)


__all__ = ["build_default_maintenance_recovery", "build_default_maintenance_services"]
