"""阶段 4B 的全局维护写锁与事务认领核心。

锁是规则写入和维护写入共同使用的语义组；本模块不执行任何配置写入，也不
创建任务、审计或备份。跨进程锁只依赖操作系统持有的文件句柄，进程异常退出
时由操作系统回收；锁的物理路径属于实现细节，绝不进入公开模型或异常文本。
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, ContextManager, Protocol

from .maintenance_changes import MaintenanceChange, MaintenanceChangeError


class MaintenanceLockError(RuntimeError):
    """维护写锁无法取得、超时或取消。"""


class MaintenanceClaimError(RuntimeError):
    """维护事务认领不满足固定状态和标识契约。"""


class MaintenanceChangeStore(Protocol):
    """认领器所需的最小存储协议；真实存储由后续执行器注入。"""

    def load(self, change_id: str) -> MaintenanceChange | None:
        """按固定逻辑 ID 读取事务，不得返回原始配置或备份材料。"""

    def save(self, change: MaintenanceChange) -> None:
        """原子保存事务摘要；失败时不得伪造认领成功。"""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _validate_id(value: Any, field_name: str) -> str:
    """只允许逻辑标识，拒绝路径、空白和控制字符，且不把输入放入错误文本。"""
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise MaintenanceClaimError(f"{field_name} 格式无效")
    return value


class MaintenanceWriteLock:
    """规则与维护共用的进程内线程锁和跨进程非阻塞 OS 锁。

    ``workspace_root`` 仅用于服务端内部定位锁文件；它不会出现在任何公开返回
    值。默认 acquire 为非阻塞，调用方可传 timeout 进行短暂重试，避免请求线程
    被无限期占用。实例必须成对调用 release，推荐使用 ``with``。
    """

    LOCK_FILE_NAME = ".aikb-write.lock"

    def __init__(self, workspace_root: str | os.PathLike[str]) -> None:
        root = Path(workspace_root)
        if not root.is_absolute():
            raise MaintenanceLockError("锁根目录必须是绝对路径")
        self._lock_path = root / "runtime" / "web" / self.LOCK_FILE_NAME
        self._thread_key = os.path.normcase(str(self._lock_path))
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(self._thread_key, threading.Lock())
        self._handle: Any = None
        self._held = False
        self._owner_thread_id: int | None = None

    @property
    def lock_name(self) -> str:
        """返回固定语义名称，不返回物理路径。"""
        return "aikb-maintenance-write"

    def acquire(
        self,
        *,
        timeout: float = 0,
        cancel_event: Any | None = None,
        owner_id: str | None = None,
    ) -> None:
        """取得锁；超时或取消抛出固定错误，并确保部分取得的锁已释放。"""
        if owner_id is not None:
            _validate_id(owner_id, "owner_id")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
            raise MaintenanceLockError("timeout 格式无效")
        deadline = time.monotonic() + float(timeout)
        self._validate_runtime_components()
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_runtime_components()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise MaintenanceLockError("维护锁认领已取消")
            if self._thread_lock.acquire(blocking=False):
                try:
                    self._try_os_lock()
                    self._held = True
                    self._owner_thread_id = threading.get_ident()
                    return
                except OSError:
                    self._thread_lock.release()
                except Exception:
                    self._thread_lock.release()
                    raise MaintenanceLockError("维护锁不可用")
            if time.monotonic() >= deadline:
                raise MaintenanceLockError("维护写入锁已占用")
            time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

    def _validate_runtime_components(self) -> None:
        """逐级确认固定运行面不是链接或非目录，避免 mkdir 跟随重定向。"""
        current = self._lock_path.parents[2]
        for component in ("runtime", "web"):
            current = current / component
            if current.exists():
                attributes = getattr(current.stat(), "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if current.is_symlink() or attributes & reparse or not current.is_dir():
                    raise MaintenanceLockError("维护锁运行面不可用")

    def _try_os_lock(self) -> None:
        """打开固定锁文件并尝试独占；文件句柄存活期间锁随进程回收。"""
        handle = open(self._lock_path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                if os.path.getsize(self._lock_path) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                if os.path.getsize(self._lock_path) == 0:
                    handle.write(b"0")
                    handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            # 抢锁失败也要关闭临时句柄，否则重复重试会泄漏句柄并延迟文件回收。
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        """释放 OS 句柄和线程锁；重复释放安全无副作用。"""
        if not self._held:
            return
        if self._owner_thread_id != threading.get_ident():
            raise MaintenanceLockError("维护锁只能由持有线程释放")
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                if os.name != "nt":
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        finally:
            self._held = False
            self._owner_thread_id = None
            self._thread_lock.release()

    @property
    def held_by_current_thread(self) -> bool:
        """只返回当前线程是否持有锁，不暴露锁文件路径或所有者标识。"""
        return self._held and self._owner_thread_id == threading.get_ident()

    def assert_held_by_current_thread(self) -> None:
        """校验调用方确实持有本锁，供锁内认领接口 fail-closed 使用。"""
        if not self.held_by_current_thread:
            raise MaintenanceLockError("维护锁必须由当前线程持有")

    def __enter__(self) -> "MaintenanceWriteLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    @contextlib.contextmanager
    def held(self, *, timeout: float = 0, cancel_event: Any | None = None) -> ContextManager["MaintenanceWriteLock"]:
        """以上下文管理锁，保证超时、取消和业务异常均不会遗留持锁状态。"""
        self.acquire(timeout=timeout, cancel_event=cancel_event)
        try:
            yield self
        finally:
            self.release()


class MaintenanceClaimCoordinator:
    """在全局维护锁内把唯一 prepared 事务原子认领为 applying。"""

    def __init__(self, store: MaintenanceChangeStore, lock: MaintenanceWriteLock) -> None:
        self._store = store
        self._lock = lock

    def claim(
        self,
        change_id: str,
        task_id: str,
        *,
        owner_id: str | None = None,
        timeout: float = 0,
        cancel_event: Any | None = None,
    ) -> MaintenanceChange:
        """锁内校验 ID 和 prepared 状态，保存唯一 task_id 的 applying 事务。"""
        change_id = _validate_id(change_id, "change_id")
        task_id = _validate_id(task_id, "task_id")
        if owner_id is not None:
            _validate_id(owner_id, "owner_id")
        try:
            with self._lock.held(timeout=timeout, cancel_event=cancel_event):
                return self.claim_held(change_id, task_id)
        except MaintenanceClaimError:
            raise
        except MaintenanceLockError:
            raise
        except Exception as error:
            raise MaintenanceClaimError("维护事务认领失败") from error

    def claim_held(self, change_id: str, task_id: str) -> MaintenanceChange:
        """在调用方已持有同一全局锁时认领，避免释放锁后再执行产生竞态。"""
        try:
            self._lock.assert_held_by_current_thread()
        except MaintenanceLockError as error:
            raise MaintenanceClaimError("维护事务认领必须在持锁线程内") from error
        change_id = _validate_id(change_id, "change_id")
        task_id = _validate_id(task_id, "task_id")
        current = self._store.load(change_id)
        if not isinstance(current, MaintenanceChange):
            raise MaintenanceClaimError("维护事务不存在")
        if current.status != "prepared" or current.task_id is not None:
            raise MaintenanceClaimError("维护事务不可认领")
        try:
            applying = current.transition("applying", task_id=task_id)
        except MaintenanceChangeError as error:
            raise MaintenanceClaimError("维护事务状态不允许认领") from error
        self._store.save(applying)
        return applying


MaintenanceTransactionClaimCoordinator = MaintenanceClaimCoordinator


__all__ = [
    "MaintenanceChangeStore",
    "MaintenanceClaimCoordinator",
    "MaintenanceClaimError",
    "MaintenanceLockError",
    "MaintenanceTransactionClaimCoordinator",
    "MaintenanceWriteLock",
]
