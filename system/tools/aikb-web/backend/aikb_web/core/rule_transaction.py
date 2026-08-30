"""阶段 4A ``rule.user.update`` 的原子事务与启动恢复核心。

本模块只接受服务端生成的 ``change_id`` 和预览令牌，不接受正文、路径、命令或
浏览器参数。它复用 :mod:`rule_preview` 的可信目标映射、令牌绑定与仓库状态，
复用共享 ``aikb.rules`` 验证器，并把每一步状态写回专用事务 JSON。正式规则
替换使用同目录临时文件、刷新和 ``os.replace``；失败时保留唯一备份并尝试
原子回滚。模块不启动 Shell、不创建任务或审计记录，也不执行 Git 写操作。
"""

from __future__ import annotations

import json
import io
import os
import re
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    from aikb.rules import (
        RuleValidationResult,
        RuleValidationError,
        list_rules,
        target_path,
        validate_auxiliary_file,
        validate_candidate_file,
        validate_rule_file,
    )
except ModuleNotFoundError:
    _core_root = Path(__file__).resolve().parents[4] / "aikb-mcp"
    if str(_core_root) not in sys.path:
        sys.path.insert(0, str(_core_root))
    from aikb.rules import (
        RuleValidationResult,
        RuleValidationError,
        list_rules,
        target_path,
        validate_auxiliary_file,
        validate_candidate_file,
        validate_rule_file,
    )

from .rule_changes import RuleChangeTransaction
from .rule_preview import RulePreviewRejected, RulePreviewService, RuleServiceError


_CHANGE_ID = re.compile(r"^change-[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_TERMINAL = frozenset({"succeeded", "expired", "rejected", "rolled_back", "recovery_required"})


class RuleTransactionError(RuleServiceError):
    """规则事务拒绝、原子写入失败或恢复需要人工介入。"""


class RuleTransactionUncertain(RuleTransactionError):
    """令牌已消费但事务边界无法确认；调用方必须进入全局恢复阻断。"""


@dataclass(frozen=True)
class RuleTransactionScanIssue:
    """启动扫描发现的安全问题；只包含逻辑 ID 和固定原因，不包含物理路径。"""

    change_id: str | None
    reason: str = "transaction_material_invalid"
    recovery_required: bool = True


class _CrossProcessLock:
    """使用固定 workspace 锁文件提供跨进程非阻塞排他锁。"""

    def __init__(self, path: Path) -> None:
        """绑定服务端生成的固定锁文件；路径不来自请求或事务 JSON。"""
        self._path = path
        self._handle: io.BufferedRandom | None = None

    def acquire(self) -> None:
        """创建/打开锁文件并尝试占用首字节；不支持的平台安全拒绝。"""
        handle: io.BufferedRandom | None = None
        try:
            if self._path.is_symlink():
                raise RuleTransactionError("控制仓锁材料不能是符号链接")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._path, "a+b")
            handle.seek(0)
            if self._path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                try:
                    import fcntl
                except ImportError as error:
                    handle.close()
                    raise RuleTransactionError("控制仓锁不可用") from error
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._handle = handle
        except (OSError, ValueError) as error:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise RuleTransactionError("控制仓正由其他规则事务占用") from error

    def release(self) -> None:
        """释放文件锁并关闭句柄；固定锁文件本身不删除以避免竞争窗口。"""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            handle.close()


def _utc_now() -> str:
    """生成事务状态使用的 UTC 时间，不携带本机时区或路径信息。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    """解析事务过期时间；无效材料视为损坏事务而不是猜测修复。"""
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError) as error:
        raise RuleTransactionError("事务时间无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RuleTransactionError("事务时间必须使用 UTC")
    return parsed.astimezone(timezone.utc)


class RuleChangeStore:
    """读写受控年月事务目录，并严格拒绝外部路径和正文元数据混入。"""

    def __init__(self, workspace_root: Path) -> None:
        """绑定可信 workspace 根目录；调用方不能通过 change_id 指定目录。"""
        self._workspace_root = Path(workspace_root).resolve()

    def _runtime_root(self) -> Path:
        """逐级解析固定运行面，并拒绝任何已存在的符号链接组件。"""
        current = self._workspace_root
        for component in ("runtime", "web", "rule-changes"):
            current = current / component
            if current.is_symlink():
                raise RuleTransactionError("规则事务运行面不能是符号链接")
        return current

    @staticmethod
    def _reject_link_components(root: Path, path: Path) -> None:
        """检查 root 下的每一个已存在路径段，避免年月目录重定向运行面。"""
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise RuleTransactionError("规则事务越出运行面边界") from error
        current = root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise RuleTransactionError("规则事务材料不能是符号链接")

    def _directory(self, change_id: str) -> Path:
        """按 change_id 定位事务目录，路径段由服务端固定生成。"""
        if not isinstance(change_id, str) or _CHANGE_ID.fullmatch(change_id) is None:
            raise RuleTransactionError("change_id 无效")
        root = self._runtime_root()
        matches = sorted(root.glob(f"????/??/{change_id}"))
        if len(matches) != 1 or not matches[0].is_dir():
            raise RuleTransactionError("规则事务不存在")
        self._reject_link_components(root, matches[0])
        directory = matches[0].resolve()
        try:
            directory.relative_to(root.resolve())
        except ValueError as error:
            raise RuleTransactionError("规则事务越出运行面边界") from error
        return directory

    def load(self, change_id: str) -> RuleChangeTransaction:
        """读取并严格解析事务 JSON；不读取或投影 candidate/backup 正文。"""
        directory = self._directory(change_id)
        try:
            transaction_path = directory / "transaction.json"
            if transaction_path.is_symlink() or not transaction_path.is_file():
                raise RuleTransactionError("规则事务材料无效")
            payload = json.loads(transaction_path.read_text(encoding="utf-8"))
            return RuleChangeTransaction.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, RuleValidationError, ValueError) as error:
            raise RuleTransactionError("规则事务材料无效") from error

    def save(self, transaction: RuleChangeTransaction) -> None:
        """以同目录临时文件刷新事务 JSON，确保恢复事实源不会半写。"""
        directory = self._directory(transaction.change_id)
        destination = directory / "transaction.json"
        if destination.is_symlink():
            raise RuleTransactionError("规则事务材料不能是符号链接")
        temporary = directory / f".transaction.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(transaction.to_dict(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        try:
            with open(temporary, "xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(temporary, destination)
            self._fsync_directory(directory)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuleTransactionError("无法保存规则事务状态") from error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """尽力刷新目录项；Windows 不支持时不改变已完成的原子替换结果。"""
        try:
            descriptor = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def candidate_path(self, change_id: str) -> Path:
        """返回事务目录内固定名称的候选文件，不接受用户路径。"""
        return self._material_path(change_id, "candidate.md")

    def backup_path(self, change_id: str) -> Path:
        """返回事务目录内固定名称的备份文件，不接受用户路径。"""
        return self._material_path(change_id, "backup.md")

    def _material_path(self, change_id: str, name: str) -> Path:
        """获取固定正文材料并拒绝符号链接，防止重定向到运行面之外。"""
        path = self._directory(change_id) / name
        if path.is_symlink():
            raise RuleTransactionError("规则事务材料不能是符号链接")
        return path

    def scan_transactions(self) -> tuple[list[RuleChangeTransaction], list[RuleTransactionScanIssue]]:
        """扫描事务并显式返回损坏/链接问题，禁止恢复层静默忽略风险。"""
        result: list[RuleChangeTransaction] = []
        issues: list[RuleTransactionScanIssue] = []
        try:
            root = self._runtime_root()
        except RuleTransactionError:
            return result, [RuleTransactionScanIssue(None)]
        if not root.is_dir():
            return result, issues
        for directory in sorted(root.glob("????/??/change-*")):
            change_id = directory.name if _CHANGE_ID.fullmatch(directory.name or "") else None
            try:
                self._reject_link_components(root, directory)
                if directory.is_symlink() or not directory.is_dir():
                    issues.append(RuleTransactionScanIssue(change_id))
                    continue
                path = directory / "transaction.json"
                if path.is_symlink() or not path.is_file():
                    issues.append(RuleTransactionScanIssue(change_id))
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                transaction = RuleChangeTransaction.from_dict(payload)
            except (OSError, UnicodeError, json.JSONDecodeError, RuleValidationError, RuleTransactionError, ValueError):
                issues.append(RuleTransactionScanIssue(change_id))
                continue
            if transaction.change_id == directory.name:
                try:
                    candidate = directory / "candidate.md"
                    backup = directory / "backup.md"
                    if candidate.is_symlink() or backup.is_symlink():
                        issues.append(RuleTransactionScanIssue(transaction.change_id))
                    else:
                        result.append(transaction)
                except OSError:
                    issues.append(RuleTransactionScanIssue(transaction.change_id))
            else:
                issues.append(RuleTransactionScanIssue(change_id))
        return result, issues

    def all_transactions(self) -> list[RuleChangeTransaction]:
        """兼容旧调用方；发现扫描问题时以安全异常阻止静默恢复。"""
        transactions, issues = self.scan_transactions()
        if issues:
            raise RuleTransactionError("规则事务扫描发现不可安全忽略的材料")
        return transactions


class RuleTransactionExecutor:
    """执行 user 规则事务、原子回滚并恢复中断的非终态事务。"""

    def __init__(self, preview_service: RulePreviewService, store: RuleChangeStore | None = None) -> None:
        """绑定既有预览服务；目标、令牌和候选位置均由服务端事实源决定。"""
        self._service = preview_service
        self._store = store or RuleChangeStore(preview_service._workspace_root)
        key = str(preview_service._repo_root)
        with _LOCKS_GUARD:
            self._repository_lock = _LOCKS.setdefault(key, threading.Lock())
        # 锁文件位于固定运行面目录，不与年月/change_id 绑定，避免请求影响锁目标。
        # 与事务材料使用同一逐级边界检查，锁文件不能因运行面链接而落到外部。
        runtime_root = self._store._runtime_root()
        self._process_lock = _CrossProcessLock(runtime_root / ".control-repository.lock")

    @contextmanager
    def _acquire_repository(self, *, blocking: bool = False) -> Iterator[None]:
        """取得全控制仓进程锁；锁冲突不消费令牌，也不改变事务。"""
        # 每次进入临界区都重新检查固定运行面，防止进程启动后目录被替换为链接。
        self._store._runtime_root()
        acquired = self._repository_lock.acquire(blocking=blocking)
        if not acquired:
            raise RuleTransactionError("控制仓正由其他规则事务占用")
        try:
            self._process_lock.acquire()
            yield
        finally:
            self._process_lock.release()
            self._repository_lock.release()

    def _target(self) -> Path:
        """通过共享静态注册表取得 user 目标，拒绝符号链接和路径注入。"""
        try:
            return self._service._path("user")
        except (RulePreviewRejected, RuleValidationError, ValueError) as error:
            raise RuleTransactionError("规则目标不可用") from error

    def _full_validation(self, candidate: RuleValidationResult | None = None) -> bool:
        """以候选覆盖 user 规则执行四项规则及 INDEX 的完整共享校验。"""
        candidate_result = candidate
        if candidate_result is None:
            candidate_result = validate_rule_file("user", self._target())
        if not candidate_result.valid:
            return False
        for spec in list_rules():
            if spec.rule_id == "user":
                continue
            try:
                if not validate_rule_file(spec.rule_id, target_path(self._service._repo_root, spec.rule_id)).valid:
                    return False
            except (OSError, RuleValidationError, ValueError):
                return False
        try:
            return validate_auxiliary_file("INDEX.md", self._service._repo_root / "INDEX.md").valid
        except (OSError, RuleValidationError, ValueError):
            return False

    @staticmethod
    def _hash_matches(result: RuleValidationResult, expected: str) -> bool:
        """比较共享验证器产生的摘要，避免直接以原始字节猜测规范化结果。"""
        return bool(_HASH.fullmatch(expected)) and result.valid and result.content_hash == expected

    def _transition(self, transaction: RuleChangeTransaction, status: str) -> RuleChangeTransaction:
        """生成并立即保存状态迁移，保存失败保持异常而不伪报成功。"""
        updated = transaction.transition(status, updated_at=_utc_now())
        self._store.save(updated)
        return updated

    def _consume_token(self, transaction: RuleChangeTransaction, token: str, preview_digest: str) -> None:
        """在所有仓库/正文前置检查后原子消费预览令牌。"""
        if not isinstance(token, str) or not isinstance(preview_digest, str):
            raise RuleTransactionError("确认参数无效")
        if preview_digest != transaction.preview_digest:
            raise RuleTransactionError("预览摘要不匹配")
        try:
            self._service._tokens.consume(
                token,
                {
                    "rule_id": transaction.rule_id,
                    "change_id": transaction.change_id,
                    "risk_level": transaction.risk_level,
                    "repository_revision": transaction.repository_revision,
                    "before_hash": transaction.before_hash,
                    "after_hash": transaction.after_hash,
                    "diff_hash": transaction.diff_hash,
                    "validator_version": transaction.validator_version,
                    "preview_digest": transaction.preview_digest,
                },
            )
        except (RulePreviewRejected, ValueError) as error:
            raise RuleTransactionError("确认令牌无效或与预览不匹配") from error

    @staticmethod
    def _token_binding(transaction: RuleChangeTransaction) -> dict[str, str]:
        """生成令牌绑定字段；只包含安全摘要、逻辑 ID 和 revision。"""
        return {
            "rule_id": transaction.rule_id,
            "change_id": transaction.change_id,
            "risk_level": transaction.risk_level,
            "repository_revision": transaction.repository_revision,
            "before_hash": transaction.before_hash,
            "after_hash": transaction.after_hash,
            "diff_hash": transaction.diff_hash,
            "validator_version": transaction.validator_version,
            "preview_digest": transaction.preview_digest,
        }

    def _precheck(self, transaction: RuleChangeTransaction, confirmation_token: str) -> tuple[Path, RuleValidationResult]:
        """执行不写入、不消费令牌的完整应用预检，并返回可信目标/候选摘要。"""
        if transaction.status != "prepared":
            raise RuleTransactionError("规则事务当前不可应用")
        if _parse_utc(transaction.expires_at) <= datetime.now(timezone.utc):
            raise RuleTransactionError("规则预览已过期")
        target = self._target()
        state = self._service._repository_state()
        if not state.ready or state.revision != transaction.repository_revision:
            raise RuleTransactionError("控制仓前置条件已变化")
        current = self._current(target)
        if not self._hash_matches(current, transaction.before_hash):
            raise RuleTransactionError("规则基线已变化")
        candidate = validate_candidate_file("user", self._store.candidate_path(transaction.change_id))
        if not self._hash_matches(candidate, transaction.after_hash) or not self._full_validation(candidate):
            raise RuleTransactionError("候选规则复核失败")
        # backup 也是服务端固定材料；提前检查其链接边界，避免令牌消费后才发现
        # 备份目标被重定向。实际写入/回滚时仍会再次检查，以应对外部并发替换。
        self._store.backup_path(transaction.change_id)
        # 候选复核可能耗时；令牌验证前再次读取状态和目标摘要，缩小 TOCTOU 窗口。
        latest_state = self._service._repository_state()
        latest = self._current(target)
        if (
            not latest_state.ready
            or latest_state.revision != transaction.repository_revision
            or not self._hash_matches(latest, transaction.before_hash)
        ):
            raise RuleTransactionError("控制仓或规则基线已变化")
        try:
            self._service._tokens.validate(confirmation_token, self._token_binding(transaction))
        except (RulePreviewRejected, ValueError) as error:
            raise RuleTransactionError("确认令牌无效或与预览不匹配") from error
        return target, candidate

    def claim(self, change_id: str, task_id: str) -> dict[str, Any]:
        """在全仓锁内为 prepared 事务原子绑定唯一任务 ID，不消费令牌。"""
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise RuleTransactionError("规则任务标识无效")
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            if transaction.status != "prepared":
                raise RuleTransactionError("规则事务当前不可认领")
            if transaction.task_id is not None and transaction.task_id != task_id:
                raise RuleTransactionError("规则事务已被其他任务认领")
            if transaction.task_id == task_id:
                return self._public(transaction)
            claimed = replace(transaction, task_id=task_id, updated_at=_utc_now())
            self._store.save(claimed)
            return self._public(claimed)

    def release_claim(self, change_id: str, task_id: str) -> dict[str, Any]:
        """释放尚未开始应用的同一任务认领，供任务创建失败安全重试。"""
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise RuleTransactionError("规则任务标识无效")
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            if transaction.task_id != task_id:
                raise RuleTransactionError("规则事务认领不匹配")
            if transaction.status != "prepared":
                raise RuleTransactionError("已开始的规则事务不能释放认领")
            released = replace(transaction, task_id=None, updated_at=_utc_now())
            self._store.save(released)
            return self._public(released)

    def prepare(self, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """在全仓锁内检查事务、仓库、候选和令牌但不消费令牌或写入文件。"""
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            self._precheck(transaction, confirmation_token)
            return self._public(transaction)

    def prepare_apply(self, *, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """兼容应用协调层的安全预检别名；正式写入仍须调用 ``apply``。"""
        return self.prepare(change_id, confirmation_token)

    def _current(self, target: Path) -> RuleValidationResult:
        """读取正式目标并通过共享验证器返回当前安全摘要。"""
        return validate_rule_file("user", target)

    def _verified_before_bytes(self, target: Path, expected_hash: str, *, after_claim: bool = False) -> bytes:
        """读取正式目标原始字节，并确认共享验证器摘要仍等于基线。"""
        error_type = RuleTransactionUncertain if after_claim else RuleTransactionError
        try:
            raw = target.read_bytes()
        except OSError as error:
            raise error_type("正式规则无法安全读取") from error
        current = validate_rule_file("user", target)
        if not self._hash_matches(current, expected_hash):
            raise error_type("正式规则在应用期间发生第三方修改")
        return raw

    @staticmethod
    def _write_synced(path: Path, data: bytes) -> None:
        """写入备份或目标临时文件并 fsync；调用方负责同目录原子替换。"""
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(temporary, path)
            RuleChangeStore._fsync_directory(path.parent)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuleTransactionError("规则文件原子写入失败") from error

    def _replace_candidate(self, target: Path, candidate: bytes) -> None:
        """把 UTF-8 无 BOM/LF 候选经同目录临时文件原子替换到正式目标。"""
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as stream:
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(stat.S_IMODE(target.stat().st_mode))
            except OSError:
                pass
            os.replace(temporary, target)
            RuleChangeStore._fsync_directory(target.parent)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuleTransactionError("规则文件原子写入失败") from error

    def _rollback(self, transaction: RuleChangeTransaction, target: Path) -> RuleChangeTransaction:
        """用唯一 backup 原子恢复并复核；失败转入 recovery_required。"""
        rollback = transaction if transaction.status == "rolling_back" else self._transition(transaction, "rolling_back")
        try:
            backup = self._store.backup_path(transaction.change_id)
            if not backup.is_file() or backup.is_symlink():
                raise RuleTransactionError("规则备份不可用")
            current = self._current(target)
            if not current.valid or current.content_hash not in {transaction.before_hash, transaction.after_hash}:
                # 正式目标已被第三方改写；回滚不能覆盖未知正文，只能人工恢复。
                raise RuleTransactionError("正式规则已被第三方修改")
            if current.content_hash == transaction.after_hash:
                self._write_synced(target, backup.read_bytes())
            if not self._full_validation():
                raise RuleTransactionError("规则回滚复核失败")
            return self._transition(rollback, "rolled_back")
        except Exception as error:
            try:
                return self._transition(rollback, "recovery_required")
            except Exception as persist_error:
                raise RuleTransactionError("规则事务需要人工恢复") from persist_error

    def apply(self, change_id: str, confirmation_token: str, preview_digest: str, task_id: str) -> dict[str, Any]:
        """在全仓锁内校验认领、消费令牌并原子应用；不创建任务或审计记录。"""
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise RuleTransactionError("规则任务标识无效")
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            if transaction.status == "prepared" and _parse_utc(transaction.expires_at) <= datetime.now(timezone.utc):
                self._transition(transaction, "expired")
                raise RuleTransactionError("规则预览已过期")
            if transaction.task_id != task_id:
                raise RuleTransactionError("规则事务认领不匹配")
            target, candidate = self._precheck(transaction, confirmation_token)
            # 在消费令牌前再取得一次与 before_hash 匹配的原始字节；后续 backup
            # 只能使用这份经验证的快照，不能把并发第三方修改当作回滚基线。
            before_bytes = self._verified_before_bytes(target, transaction.before_hash)
            self._consume_token(transaction, confirmation_token, preview_digest)
            try:
                applying = self._transition(transaction, "applying")
            except Exception as error:
                # 令牌已经不可重放，而 applying 未必已经落盘；明确抛出不确定态，
                # 由协调器全局阻断，绝不能伪装成普通候选校验失败。
                raise RuleTransactionUncertain("规则事务状态无法确认") from error
            backup = self._store.backup_path(change_id)
            try:
                self._write_synced(backup, before_bytes)
                # 这是替换前最后一次正式目标检查；发现第三方修改时只记录人工
                # 恢复态，绝不使用 backup 覆盖第三方正文。
                self._verified_before_bytes(target, transaction.before_hash, after_claim=True)
                self._replace_candidate(target, candidate.normalized_content.encode("utf-8"))
                validating = self._transition(applying, "validating")
                if not self._full_validation():
                    return self._public(self._rollback(validating, target))
                succeeded = self._transition(validating, "succeeded")
                return self._public(succeeded)
            except RuleTransactionUncertain:
                try:
                    latest = self._store.load(change_id)
                    if latest.status == "applying":
                        rolling = self._transition(latest, "rolling_back")
                        self._transition(rolling, "recovery_required")
                except Exception:
                    pass
                raise
            except Exception:
                # backup 一旦成功写入便不得删除；回滚失败由 recovery_required 保留现场。
                try:
                    latest = self._store.load(change_id)
                    if latest.status in {"succeeded", "recovery_required"}:
                        raise RuleTransactionUncertain("规则事务终态无法安全回滚")
                    rolled_back = self._rollback(latest, target)
                    return self._public(rolled_back)
                except RuleTransactionUncertain:
                    raise
                except Exception as error:
                    raise RuleTransactionUncertain("规则事务回滚状态无法确认") from error

    def mark_audit_failure(self, change_id: str, task_id: str | None = None) -> dict[str, Any]:
        """终态审计失败时保留 backup 并转人工恢复，供协调器安全阻断。"""
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            if task_id is not None and transaction.task_id != task_id:
                raise RuleTransactionError("规则事务认领不匹配")
            if transaction.status == "recovery_required":
                return self._public(transaction)
            if transaction.status not in {"succeeded", "rolled_back"}:
                raise RuleTransactionError("规则事务尚未到达可恢复终态")
            # 不清理 backup；恢复人员仍可据此确认或人工回滚正式正文。
            return self._public(self._transition(transaction, "recovery_required"))

    def finalize_success(self, change_id: str, task_id: str) -> dict[str, Any]:
        """仅在终态审计成功后清理正文材料，安全 JSON 始终保留。"""
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise RuleTransactionError("规则任务标识无效")
        with self._acquire_repository():
            transaction = self._store.load(change_id)
            if transaction.status != "succeeded" or transaction.task_id != task_id:
                raise RuleTransactionError("规则事务尚未完成终态审计")
            for path in (self._store.candidate_path(change_id), self._store.backup_path(change_id)):
                if path.is_symlink():
                    raise RuleTransactionError("规则事务材料不能是符号链接")
                if path.exists() and not path.is_file():
                    raise RuleTransactionError("规则事务正文材料无效")
            for path in (self._store.candidate_path(change_id), self._store.backup_path(change_id)):
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    raise RuleTransactionError("规则事务正文清理失败") from error
            return self._public(transaction)

    @staticmethod
    def _public(transaction: RuleChangeTransaction) -> dict[str, Any]:
        """返回不含正文、备份和路径的事务安全投影。"""
        return transaction.public_dict()

    def recover(self) -> list[dict[str, Any]]:
        """扫描非终态事务并按摘要决定回滚或人工恢复，绝不覆盖第三方修改。"""
        recovered: list[dict[str, Any]] = []
        with self._acquire_repository():
            transactions, issues = self._store.scan_transactions()
            # issue 投影不包含路径；上层可据此设置全局 recovery_required，不能
            # 因 transaction.json 损坏或材料链接而误报系统完全健康。
            recovered.extend(
                {
                    "change_id": issue.change_id,
                    "status": "recovery_required",
                    "reason": issue.reason,
                    "recovery_required": True,
                }
                for issue in issues
            )
            for transaction in transactions:
                if transaction.status in _TERMINAL:
                    continue
                if transaction.status == "prepared":
                    if _parse_utc(transaction.expires_at) <= datetime.now(timezone.utc):
                        try:
                            transaction = self._transition(transaction, "expired")
                        except RuleTransactionError:
                            continue
                    recovered.append(self._public(transaction))
                    continue
                try:
                    target = self._target()
                    current = self._current(target)
                    if not current.valid:
                        raise RuleTransactionError("正式规则当前状态不可识别")
                    if current.content_hash == transaction.before_hash:
                        transaction = self._transition(transaction, "rolling_back") if transaction.status != "rolling_back" else transaction
                        transaction = self._transition(transaction, "rolled_back")
                    elif current.content_hash == transaction.after_hash:
                        transaction = self._rollback(transaction, target)
                    else:
                        transaction = self._transition(transaction, "rolling_back") if transaction.status != "rolling_back" else transaction
                        transaction = self._transition(transaction, "recovery_required")
                except Exception:
                    try:
                        if transaction.status in {"applying", "validating"}:
                            transaction = self._transition(transaction, "rolling_back")
                        if transaction.status == "rolling_back":
                            transaction = self._transition(transaction, "recovery_required")
                    except Exception:
                        pass
                recovered.append(self._public(transaction))
        return recovered
