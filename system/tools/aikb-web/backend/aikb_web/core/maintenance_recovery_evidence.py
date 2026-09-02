"""维护恢复终态的 JSONL 审计证据适配器。

适配器只读取既有审计 JSONL 的安全关联字段，并以同一事实源追加固定恢复事件；
不保存路径、材料、环境值或异常文本。真实审计后端无需改变，调用方注入其
``read_events``/``write`` 最小接口。任何损坏或绑定冲突都 fail-closed。
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Mapping, Protocol
from .maintenance_changes import MaintenanceChange
from .maintenance_startup_recovery import MaintenanceTerminalEvidence


class MaintenanceRecoveryEvidenceError(RuntimeError):
    """审计事实源不可读、格式损坏或终态证据写入失败。"""


_OUTCOMES = frozenset({"succeeded", "rolled_back"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_LINE = 64 * 1024


class MaintenanceAuditStore(Protocol):
    """真实审计存储的最小注入协议，负责按日/fallback 与跨进程写锁。"""
    def read_events(self) -> Any: ...
    def write(self, record: Mapping[str, Any]) -> Any: ...


class MaintenanceRecoveryEvidenceAdapter:
    """严格读取并追加维护恢复终态事件的 JSONL 适配器。"""

    def __init__(self, transaction_store: Any, audit_store: MaintenanceAuditStore) -> None:
        """绑定事务读取器和真实审计存储；跨进程锁由审计存储负责。"""
        if not callable(getattr(transaction_store, "load", None)):
            raise MaintenanceRecoveryEvidenceError("事务事实源接口无效")
        self._transactions = transaction_store
        if not callable(getattr(audit_store, "read_events", None)) or not callable(getattr(audit_store, "write", None)):
            raise MaintenanceRecoveryEvidenceError("审计存储接口无效")
        self._audit = audit_store
        self._lock = threading.Lock()

    def terminal_evidence(self, change_id: str) -> MaintenanceTerminalEvidence:
        """逐行解析审计事实并返回唯一终态或固定冲突状态。"""
        if not isinstance(change_id, str) or _ID_RE.fullmatch(change_id) is None:
            raise MaintenanceRecoveryEvidenceError("恢复证据 change_id 无效")
        transaction = self._transactions.load(change_id)
        binding = self._binding_from_transaction(transaction)
        return self._terminal_evidence_for(binding)

    def task_evidence(self, change_id: str) -> str | None:
        """查找 apply 已开始但尚未产生终态的任务证据。

        ``MaintenanceTaskCoordinator`` 会先写 invocation_started，再消费令牌/认领
        事务；启动恢复据此区分“只有 prepared 摘要”与可能已经进入执行流程的现场。
        返回的 task_id 仅用于 recovery_required 的逻辑绑定，不返回任何材料正文。
        """

        if not isinstance(change_id, str) or _ID_RE.fullmatch(change_id) is None:
            raise MaintenanceRecoveryEvidenceError("恢复证据 change_id 无效")
        lines, damaged = self._read_events()
        if damaged:
            raise MaintenanceRecoveryEvidenceError("审计事实源损坏")
        task_ids: set[str] = set()
        for raw in lines:
            try:
                if isinstance(raw, Mapping):
                    item = dict(raw)
                elif isinstance(raw, bytes):
                    if len(raw) > _MAX_LINE or not raw.strip():
                        raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                    item = json.loads(raw.decode("utf-8"), object_pairs_hook=self._no_duplicates)
                elif isinstance(raw, str):
                    if len(raw.encode("utf-8")) > _MAX_LINE or not raw.strip():
                        raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                    item = json.loads(raw, object_pairs_hook=self._no_duplicates)
                else:
                    raise MaintenanceRecoveryEvidenceError("审计事实行无效")
            except (UnicodeError, json.JSONDecodeError, TypeError) as error:
                raise MaintenanceRecoveryEvidenceError("审计事实行无效") from error
            if not isinstance(item, dict):
                raise MaintenanceRecoveryEvidenceError("审计事实行无效")
            if (
                item.get("record_type") != "invocation_started"
                or item.get("operation") != "maintenance.apply"
                or item.get("change_id") != change_id
            ):
                continue
            candidate = item.get("task_id") or item.get("target_task_id")
            if not isinstance(candidate, str) or _ID_RE.fullmatch(candidate) is None:
                raise MaintenanceRecoveryEvidenceError("任务证据绑定无效")
            task_ids.add(candidate)
        if len(task_ids) > 1:
            raise MaintenanceRecoveryEvidenceError("任务证据存在冲突")
        return next(iter(task_ids), None)

    def finish_recovery(self, transaction: MaintenanceChange, outcome: str) -> bool:
        """追加可唯一识别的安全终态事件；已有终态或写入异常均拒绝。"""
        if outcome not in _OUTCOMES:
            raise MaintenanceRecoveryEvidenceError("终态审计状态无效")
        binding = self._binding_from_transaction(transaction)
        with self._lock:
            # 查询和追加必须处于同一个实例锁区间；跨进程串行由外层维护写锁
            # 保证，真正的审计存储仍负责自己的文件写锁与 fallback。
            evidence = self._terminal_evidence_for(binding)
            if evidence.state != "none":
                return False
            # 审计 schema 的 status 没有 rolled_back；回滚事实使用固定
            # ``failed + outcome_code=rolled_back + rollback_status=succeeded``
            # 组合，避免写入层把未知 status 归一化后丢失终态语义。
            record = {
                "schema_version": 4,
                "record_type": "invocation_finished",
                "invocation_id": transaction.change_id,
                "source": "web",
                "operation": "maintenance.recover",
                "status": "succeeded" if outcome == "succeeded" else "failed",
                "outcome_code": outcome,
                "rollback_status": "not_applicable" if outcome == "succeeded" else "succeeded",
                **binding,
            }
            try:
                result = self._audit.write(record)
                if not (result is True or (isinstance(result, Mapping) and result.get("written") is True)):
                    raise MaintenanceRecoveryEvidenceError("终态审计写入未确认")
            except MaintenanceRecoveryEvidenceError:
                raise
            except Exception as error:
                raise MaintenanceRecoveryEvidenceError("终态审计写入失败") from error
            return True

    def _read_events(self) -> tuple[list[Any], list[Any]]:
        """读取真实审计存储的事件与损坏项；不自行解析路径或 fallback。"""
        try:
            result = self._audit.read_events()
            if isinstance(result, Mapping):
                # 真实 AuditStore 使用 events；items 仅保留给轻量测试桩兼容。
                if "events" in result:
                    events = result["events"]
                elif "items" in result:
                    events = result["items"]
                else:
                    raise MaintenanceRecoveryEvidenceError("审计读取结果无效")
                return list(events), list(result.get("damaged", ()))
            if isinstance(result, tuple) and len(result) == 2:
                return list(result[0]), list(result[1])
            return list(result), []
        except Exception as error:
            raise MaintenanceRecoveryEvidenceError("审计事实源不可读") from error

    def _terminal_evidence_for(self, binding: dict[str, str]) -> MaintenanceTerminalEvidence:
        """归并已绑定事务的终态事件；字典和原始 JSONL 使用同一判定。"""
        lines, damaged = self._read_events()
        if damaged:
            raise MaintenanceRecoveryEvidenceError("审计事实源损坏")
        matches: list[str] = []
        mismatch = False
        try:
            for raw in lines:
                if isinstance(raw, Mapping):
                    item = dict(raw)
                elif isinstance(raw, bytes):
                    if len(raw) > _MAX_LINE or not raw.strip():
                        raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                    item = json.loads(raw.decode("utf-8"), object_pairs_hook=self._no_duplicates)
                elif isinstance(raw, str):
                    if len(raw.encode("utf-8")) > _MAX_LINE or not raw.strip():
                        raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                    item = json.loads(raw, object_pairs_hook=self._no_duplicates)
                else:
                    raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                if not isinstance(item, dict):
                    raise MaintenanceRecoveryEvidenceError("审计事实行无效")
                if item.get("record_type") != "invocation_finished" or item.get("operation") != "maintenance.recover":
                    continue
                if item.get("change_id") != binding["change_id"]:
                    continue
                if any(item.get(key) != value for key, value in binding.items()):
                    mismatch = True
                    continue
                status = item.get("status")
                outcome = item.get("outcome_code")
                rollback_status = item.get("rollback_status")
                valid_success = outcome == "succeeded" and status == "succeeded" and rollback_status == "not_applicable"
                valid_rollback = outcome == "rolled_back" and status == "failed" and rollback_status == "succeeded"
                if not (valid_success or valid_rollback):
                    raise MaintenanceRecoveryEvidenceError("终态审计状态无效")
                matches.append(outcome)
        except (UnicodeError, json.JSONDecodeError, MaintenanceRecoveryEvidenceError) as error:
            raise MaintenanceRecoveryEvidenceError("审计事实源损坏") from error
        if mismatch:
            return MaintenanceTerminalEvidence("binding_mismatch")
        if not matches:
            return MaintenanceTerminalEvidence("none")
        if len(set(matches)) > 1:
            return MaintenanceTerminalEvidence("conflict")
        if len(matches) > 1:
            return MaintenanceTerminalEvidence("duplicate")
        return MaintenanceTerminalEvidence(
            f"unique_{matches[0]}",
            change_id=binding["change_id"],
            target_id=binding["maintenance_target_id"],
            before_fingerprint=binding["before_fingerprint"],
            after_fingerprint=binding["after_fingerprint"],
            task_id=binding["task_id"],
        )

    @staticmethod
    def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MaintenanceRecoveryEvidenceError("审计事实含重复字段")
            result[key] = value
        return result

    @staticmethod
    def _binding(change_id: str, target_id: str, before_fingerprint: str, after_fingerprint: str, task_id: str) -> dict[str, str]:
        if not all(isinstance(value, str) and _ID_RE.fullmatch(value) for value in (change_id, target_id, task_id)) or not all(isinstance(value, str) and _HASH_RE.fullmatch(value) for value in (before_fingerprint, after_fingerprint)):
            raise MaintenanceRecoveryEvidenceError("恢复证据绑定字段无效")
        return {"change_id": change_id, "maintenance_target_id": target_id, "before_fingerprint": before_fingerprint, "after_fingerprint": after_fingerprint, "task_id": task_id}

    @staticmethod
    def _binding_from_transaction(transaction: MaintenanceChange) -> dict[str, str]:
        """从严格事务模型提取审计绑定，禁止调用方自由传入关联字段。"""
        return MaintenanceRecoveryEvidenceAdapter._binding(transaction.change_id, transaction.target_id, transaction.before_fingerprint, transaction.after_fingerprint, transaction.task_id or "")


__all__ = ["MaintenanceRecoveryEvidenceAdapter", "MaintenanceRecoveryEvidenceError"]
