"""阶段 4B 维护事务的安全事实源。

本模块只保存 :mod:`maintenance_changes` 定义的逻辑状态和安全元数据，不接触
用户配置、备份正文、环境变量值或真实执行器。运行面由服务端注入，固定在
``workspace/runtime/web/maintenance-transactions``；任何符号链接、junction 或
Windows 重解析点都会 fail-closed，避免事务事实源被重定向到工作区外。
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

from .maintenance_changes import (
    MAINTENANCE_CHANGE_STATUSES,
    TERMINAL_MAINTENANCE_CHANGE_STATUSES,
    MaintenanceChange,
    MaintenanceChangeError,
)
from .maintenance_targets import validate_logical_id


class MaintenanceTransactionStoreError(ValueError):
    """事务事实源路径、JSON 材料或原子落盘失败。"""


_MAX_TRANSACTION_BYTES = 16 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，防止不同解析器对同一事实源产生不同结论。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MaintenanceTransactionStoreError("事务 JSON 含重复字段")
        result[key] = value
    return result


def _is_reparse_point(path: Path) -> bool:
    """识别链接和 Windows 重解析点；检查不跟随链接，避免边界检查失真。"""
    try:
        info = path.lstat()
    except OSError as error:
        raise MaintenanceTransactionStoreError("事务运行面无法读取") from error
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


class MaintenanceTransactionStore:
    """维护事务目录的最小持久化接口，所有写入都经过同目录原子替换。

    构造函数只绑定注入的 workspace，不创建目录；只有 ``create`` 才建立运行面
    和事务目录。``save`` 只接受已经存在且目录名与 change_id 一致的事务目录，
    这样损坏或越界路径不会被自动修复或静默迁移。
    """

    def __init__(self, workspace_root: Path | str) -> None:
        """绑定可信 workspace 根；调用方不得从请求参数传入事务子目录。"""
        self._workspace_root = Path(workspace_root).absolute()
        # 同一进程内串行化“检查旧文件→替换”窗口；跨进程仍由 os.replace 提供
        # 原子可见性，二者共同保证并发读取不会观察到半 JSON。
        self._save_lock = threading.Lock()

    def _runtime_root(self, *, create: bool = False) -> Path:
        """逐级检查固定运行面；按需创建普通目录但绝不创建重解析点。"""
        current = self._workspace_root
        if current.exists() and _is_reparse_point(current):
            raise MaintenanceTransactionStoreError("workspace 不能是重解析点")
        for component in ("runtime", "web", "maintenance-transactions"):
            current = current / component
            if current.exists():
                if _is_reparse_point(current) or not current.is_dir():
                    raise MaintenanceTransactionStoreError("维护事务运行面不是普通目录")
            elif create:
                try:
                    current.mkdir()
                except FileExistsError:
                    if _is_reparse_point(current) or not current.is_dir():
                        raise MaintenanceTransactionStoreError("维护事务运行面创建竞争异常")
                except OSError as error:
                    raise MaintenanceTransactionStoreError("无法创建维护事务运行面") from error
        return current

    @staticmethod
    def _validate_change_id(change_id: Any) -> str:
        """校验事务目录名只能是逻辑 ID，拒绝路径、空白和控制字符。"""
        try:
            return validate_logical_id(change_id, "change_id")
        except (TypeError, ValueError) as error:
            raise MaintenanceTransactionStoreError("change_id 无效") from error

    def _directory(self, change_id: str, *, create: bool = False) -> Path:
        """解析固定事务目录并逐段拒绝链接；不会 resolve 到边界外。"""
        safe_id = self._validate_change_id(change_id)
        root = self._runtime_root(create=create)
        directory = root / safe_id
        if directory.exists():
            if _is_reparse_point(directory) or not directory.is_dir():
                raise MaintenanceTransactionStoreError("事务目录不是普通目录")
        elif create:
            try:
                directory.mkdir()
            except OSError as error:
                raise MaintenanceTransactionStoreError("无法创建事务目录") from error
        return directory

    def create(self, transaction: MaintenanceChange) -> MaintenanceChange:
        """显式创建事务目录并首次原子落盘；构造 Store 本身不会产生副作用。"""
        if not isinstance(transaction, MaintenanceChange):
            raise MaintenanceTransactionStoreError("只能保存 MaintenanceChange")
        # 已存在的目录即使暂时没有 transaction.json 也视为残留材料，不能借创建
        # 接口覆盖或“修复”；这样崩溃现场只能由后续人工恢复流程处理。
        root = self._runtime_root(create=True)
        safe_id = self._validate_change_id(transaction.change_id)
        directory = root / safe_id
        if directory.exists():
            if _is_reparse_point(directory) or not directory.is_dir():
                raise MaintenanceTransactionStoreError("事务目录不是普通目录")
            raise MaintenanceTransactionStoreError("事务目录已存在")
        try:
            directory.mkdir()
        except OSError as error:
            raise MaintenanceTransactionStoreError("无法创建事务目录") from error
        destination = directory / "transaction.json"
        if destination.exists():
            if _is_reparse_point(destination) or not destination.is_file():
                raise MaintenanceTransactionStoreError("事务材料不是普通文件")
            raise MaintenanceTransactionStoreError("事务已存在")
        try:
            self._atomic_write(destination, transaction)
        except Exception:
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        return transaction

    def load(self, change_id: str) -> MaintenanceChange:
        """严格读取事务 JSON；缺字段、未知字段、重复键、超预算或损坏均拒绝。"""
        directory = self._directory(change_id)
        path = directory / "transaction.json"
        if not path.exists() or _is_reparse_point(path) or not path.is_file():
            raise MaintenanceTransactionStoreError("事务材料不存在或不是普通文件")
        try:
            if path.stat().st_size > _MAX_TRANSACTION_BYTES:
                raise MaintenanceTransactionStoreError("事务 JSON 超出大小预算")
            payload = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
            transaction = MaintenanceChange.from_dict(payload)
            if transaction.change_id != change_id:
                raise MaintenanceTransactionStoreError("事务目录名与 change_id 不一致")
            return transaction
        except MaintenanceTransactionStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, MaintenanceChangeError) as error:
            raise MaintenanceTransactionStoreError("事务 JSON 损坏或契约无效") from error

    def save(self, transaction: MaintenanceChange) -> None:
        """用同目录临时文件、flush、fsync 和 os.replace 原子更新事务事实源。"""
        if not isinstance(transaction, MaintenanceChange):
            raise MaintenanceTransactionStoreError("只能保存 MaintenanceChange")
        directory = self._directory(transaction.change_id)
        destination = directory / "transaction.json"
        if destination.exists() and _is_reparse_point(destination):
            raise MaintenanceTransactionStoreError("事务材料不能是重解析点")
        if not destination.exists() or not destination.is_file():
            raise MaintenanceTransactionStoreError("事务材料不存在")
        with self._save_lock:
            self._atomic_write(destination, transaction)

    @staticmethod
    def _atomic_write(destination: Path, transaction: MaintenanceChange) -> None:
        """写入受预算约束的 UTF-8 JSON，并在替换失败时保留旧事实源。"""
        payload = json.dumps(transaction.to_dict(), ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        if len(encoded) > _MAX_TRANSACTION_BYTES:
            raise MaintenanceTransactionStoreError("事务 JSON 超出大小预算")
        # 临时名包含线程标识，允许同一进程并发保存同一事务而不互相打开临时文件。
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}-{threading.get_ident()}-{id(transaction)}"
        )
        try:
            with open(temporary, "xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                directory_handle = os.open(destination.parent, os.O_RDONLY)
            except OSError:
                directory_handle = None
            if directory_handle is not None:
                try:
                    os.fsync(directory_handle)
                finally:
                    os.close(directory_handle)
        except (OSError, ValueError) as error:
            raise MaintenanceTransactionStoreError("事务 JSON 原子落盘失败") from error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def list_nonterminal(self) -> tuple[MaintenanceChange, ...]:
        """列出所有非终态事务；运行面缺失返回空，异常材料不会被跳过。"""
        root = self._runtime_root()
        if not root.exists():
            return ()
        try:
            directories = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise MaintenanceTransactionStoreError("无法扫描维护事务运行面") from error
        result: list[MaintenanceChange] = []
        for directory in directories:
            if _is_reparse_point(directory) or not directory.is_dir():
                raise MaintenanceTransactionStoreError("运行面含非法事务目录")
            self._validate_change_id(directory.name)
            transaction = self.load(directory.name)
            if transaction.status not in TERMINAL_MAINTENANCE_CHANGE_STATUSES:
                result.append(transaction)
        return tuple(result)


__all__ = ["MaintenanceTransactionStore", "MaintenanceTransactionStoreError"]
