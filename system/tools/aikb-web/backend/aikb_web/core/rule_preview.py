"""阶段 4A 规则读取、候选预览和本机草稿服务。

本模块是 Web 规则中心的唯一服务端编排层：静态规则目标来自共享
``aikb.rules``，浏览器只能提供规则 ID、基线摘要和候选正文。预览会做只读
Git 前置检查，并把候选和不含正文的事务元数据保存到本机运行面；这里没有
正式文件替换、备份、任务、审计或任意 Shell 执行能力。
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

# 直接从 backend 启动时 sibling 核心不一定已加入 sys.path；只补充受控的共享
# 核心目录，仍不复制规则注册表，也不接受环境变量提供的任意导入路径。
try:
    from aikb.rules import VALIDATOR_VERSION, target_path, validate_content
except ModuleNotFoundError:
    _core_root = Path(__file__).resolve().parents[4] / "aikb-mcp"
    if str(_core_root) not in sys.path:
        sys.path.insert(0, str(_core_root))
    from aikb.rules import VALIDATOR_VERSION, target_path, validate_content

from .rule_changes import RuleChangeTransaction, RULE_UPDATE_ACTION_ID
from .rules import RuleError, RuleReadModel, RuleRegistry, normalize_rule_content, rule_content_hash


MAX_CANDIDATE_LINES = 2_000
MAX_DIFF_LINES = 4_000
MAX_DIFF_BYTES = 256 * 1024
_HEX_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SAFE_CHANGE_ID = re.compile(r"^change-[0-9a-f]{32}$")


class RuleServiceError(RuntimeError):
    """规则服务无法读取仓库或创建安全预览材料。"""


class RulePreviewRejected(ValueError):
    """候选或仓库前置条件不满足预览契约；details 仅包含安全摘要。"""

    def __init__(self, message: str, *, status_code: int = 409, code: str = "rule_preview_rejected", details: Any | None = None):
        """保存可公开的错误码和字段级详情，不携带正文、路径或底层异常。"""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


@dataclass(frozen=True)
class RepositoryState:
    """控制仓只读检查结果；Git 原始输出和物理路径不离开服务内部。"""

    revision: str
    branch: str
    clean: bool
    ordinary_branch: bool
    in_operation: bool

    @property
    def ready(self) -> bool:
        """判断是否满足预览要求的普通分支、无事务状态和全仓洁净。"""
        return bool(self.revision and self.branch and self.clean and self.ordinary_branch and not self.in_operation)


@dataclass(frozen=True)
class _PreviewTokenRecord:
    """仅保存在当前进程内的预览令牌绑定，服务重启后自然失效。"""

    rule_id: str
    change_id: str
    risk_level: str
    repository_revision: str
    before_hash: str
    after_hash: str
    diff_hash: str
    validator_version: str
    preview_digest: str
    expires_at: float


class RulePreviewTokenService:
    """签发五分钟、绑定完整预览摘要且未提供消费入口的进程内令牌。"""

    TTL_SECONDS = 300
    MAX_ACTIVE_TOKENS = 1024

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """生成进程随机密钥；不把密钥、令牌或候选写入运行面。"""
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._records: dict[str, _PreviewTokenRecord] = {}
        self._lock = threading.RLock()

    def issue(self, record: _PreviewTokenRecord) -> str:
        """为已经完成所有校验的预览生成一次性确认令牌。"""
        nonce = secrets.token_urlsafe(32)
        token = hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).hexdigest() + "." + nonce
        with self._lock:
            now = self._clock()
            self._records = {key: value for key, value in self._records.items() if value.expires_at > now}
            if len(self._records) >= self.MAX_ACTIVE_TOKENS:
                raise RuleServiceError("待确认预览过多")
            self._records[token] = record
        return token

    def consume(self, token: str, expected: Mapping[str, str]) -> None:
        """校验并消费令牌绑定；本批次没有 HTTP 消费入口，供后续 apply 复用。"""
        with self._lock:
            record = self._records.get(token)
            if record is None:
                raise RulePreviewRejected("确认令牌无效或已消费", status_code=409, code="preview_token_invalid")
            if self._clock() >= record.expires_at:
                self._records.pop(token, None)
                raise RulePreviewRejected("确认令牌已过期", status_code=409, code="preview_token_expired")
            fields = {
                "rule_id": record.rule_id, "change_id": record.change_id,
                "risk_level": record.risk_level, "repository_revision": record.repository_revision,
                "before_hash": record.before_hash, "after_hash": record.after_hash,
                "diff_hash": record.diff_hash, "validator_version": record.validator_version,
                "preview_digest": record.preview_digest,
            }
            if set(expected) != set(fields) or any(
                not isinstance(value, str) or not hmac.compare_digest(fields[key], value)
                for key, value in expected.items()
            ):
                raise RulePreviewRejected("确认令牌与预览不匹配", status_code=409, code="preview_token_mismatch")
            self._records.pop(token, None)


class RulePreviewService:
    """提供规则目录/正文和零副作用候选预览。"""

    def __init__(
        self,
        settings: Any,
        *,
        registry: RuleRegistry | None = None,
        token_service: RulePreviewTokenService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """绑定可信设置和静态注册表；不接受浏览器覆盖的文件路径。"""
        repo_root = getattr(settings, "repo_root", None)
        workspace_root = getattr(settings, "workspace_root", None)
        if not isinstance(repo_root, Path) or not isinstance(workspace_root, Path):
            raise RuleServiceError("规则服务设置不可用")
        self._repo_root = repo_root.resolve()
        self._workspace_root = workspace_root.resolve()
        self._registry = registry or RuleRegistry()
        self._tokens = token_service or RulePreviewTokenService(clock)
        self._clock = clock

    @property
    def registry(self) -> RuleRegistry:
        """返回固定注册表，供 API 进行安全能力查询。"""
        return self._registry

    def _path(self, rule_id: str) -> Path:
        """按共享核心静态 ID 解析目标；物理路径仅留在内部文件操作。"""
        try:
            self._registry.get(rule_id)
            return target_path(self._repo_root, rule_id)
        except (RuleError, ValueError) as error:
            raise RulePreviewRejected("规则不存在", status_code=404, code="rule_not_found") from error

    def _read(self, rule_id: str) -> RuleReadModel:
        """读取 UTF-8 无 BOM 正文并构造安全详情模型，不回显文件错误。"""
        try:
            spec = self._registry.get(rule_id)
        except RuleError as error:
            raise RulePreviewRejected("规则不存在", status_code=404, code="rule_not_found") from error
        path = self._path(rule_id)
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8", errors="strict")
            normalized = normalize_rule_content(content, max_chars=spec.max_chars)
        except (OSError, UnicodeError, RuleError) as error:
            raise RuleServiceError("规则正文不可读取") from error
        revision = self._repository_state().revision
        if not revision:
            raise RuleServiceError("控制仓 revision 不可读取")
        return RuleReadModel(spec, normalized, rule_content_hash(normalized, max_chars=spec.max_chars), revision)

    def _git(self, args: tuple[str, ...], *, timeout: float = 8) -> tuple[bool, str]:
        """执行固定、只读的 Git 查询；从不启用 shell，也不返回错误输出。"""
        try:
            result = subprocess.run(
                ["git", *args], cwd=self._repo_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False, ""
        return result.returncode == 0, result.stdout.strip()

    def _repository_state(self) -> RepositoryState:
        """读取 revision、分支、全仓状态和 Git 未完成操作标记。"""
        revision_ok, revision = self._git(("rev-parse", "HEAD"))
        branch_ok, branch = self._git(("symbolic-ref", "--quiet", "--short", "HEAD"))
        status_ok, status = self._git(("--no-optional-locks", "status", "--porcelain=v1"), timeout=10)
        git_dir_ok, git_dir_text = self._git(("rev-parse", "--git-dir"))
        git_dir = Path(git_dir_text)
        if git_dir_ok and not git_dir.is_absolute():
            git_dir = self._repo_root / git_dir
        markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
        in_operation = bool(git_dir_ok and any((git_dir / marker).exists() for marker in markers))
        # revision 只用于绑定和公开摘要，Git 输出即使被污染也不能进入响应。
        valid_revision = revision if revision_ok and _SAFE_REVISION.fullmatch(revision) else ""
        valid_branch = branch if branch_ok and _SAFE_BRANCH.fullmatch(branch) else ""
        return RepositoryState(valid_revision, valid_branch, status_ok and not bool(status), bool(valid_branch), in_operation)

    def list_rules(self) -> list[dict[str, Any]]:
        """列出四项静态规则及当前摘要；公共字段不包含物理路径。"""
        state = self._repository_state()
        hashes: dict[str, str] = {}
        for rule_id in ("entry", "user", "agent", "contributing"):
            hashes[rule_id] = self._read(rule_id).content_hash
        return self._registry.public_list(content_hashes=hashes, revision=state.revision or None)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        """读取指定规则当前正文和安全元数据。"""
        return self._read(rule_id).public_dict()

    @staticmethod
    def _diff(source: str, candidate: str) -> str:
        """生成完整 LF unified diff，文件名使用逻辑标签而非物理路径。"""
        source_lines = source.splitlines(keepends=True)
        candidate_lines = candidate.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                source_lines, candidate_lines,
                fromfile="USER_RULES.md (current)", tofile="USER_RULES.md (candidate)",
                lineterm="\n",
            )
        )

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        """按稳定 JSON 计算预览摘要，避免字段顺序影响令牌绑定。"""
        canonical = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _transaction_dir(self, change_id: str, now: datetime) -> Path:
        """创建仅当前用户可访问的年月事务目录，不接受外部目录名。"""
        root = self._workspace_root / "runtime" / "web" / "rule-changes" / now.strftime("%Y") / now.strftime("%m")
        directory = root / change_id
        try:
            directory.mkdir(parents=True, exist_ok=False)
            try:
                directory.chmod(stat.S_IRWXU)
            except OSError:
                pass
        except OSError as error:
            raise RuleServiceError("无法创建规则预览材料") from error
        return directory

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        """写入短期本机材料并尽量限制为当前用户读写权限。"""
        try:
            path.write_text(content, encoding="utf-8", newline="\n")
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        except OSError as error:
            raise RuleServiceError("无法保存规则预览材料") from error

    def preview(self, rule_id: str, *, base_content_hash: str, candidate_content: str) -> dict[str, Any]:
        """校验仓库和候选，生成完整 diff、事务草稿及五分钟进程内令牌。"""
        try:
            spec = self._registry.get(rule_id)
        except RuleError as error:
            raise RulePreviewRejected("规则不存在", status_code=404, code="rule_not_found") from error
        if not spec.writable or rule_id != "user":
            raise RulePreviewRejected("该规则只读", status_code=403, code="rule_read_only")
        if not isinstance(base_content_hash, str) or _HEX_HASH.fullmatch(base_content_hash) is None:
            raise RulePreviewRejected("基线摘要无效", status_code=400, code="invalid_base_hash")
        if not isinstance(candidate_content, str):
            raise RulePreviewRejected("候选正文无效", status_code=422, code="invalid_candidate")

        state = self._repository_state()
        if not state.ready:
            raise RulePreviewRejected("控制仓前置条件不满足", code="repository_not_ready")
        current = self._read(rule_id)
        # 读取正文期间若分支/revision 发生变化，即使正文摘要暂时相同也不能
        # 继续生成绑定旧版本的预览；调用方应重新读取详情再发起预览。
        if current.revision != state.revision:
            raise RulePreviewRejected("控制仓 revision 已变化", code="repository_revision_conflict")
        if not hmac.compare_digest(base_content_hash, current.content_hash):
            raise RulePreviewRejected("规则基线已变化", code="base_hash_conflict")

        validation = validate_content(rule_id, candidate_content)
        if not validation.valid:
            raise RulePreviewRejected(
                "候选规则校验失败", status_code=422, code="candidate_invalid",
                details={"validation": validation.as_dict()},
            )
        candidate = validation.normalized_content
        if hmac.compare_digest(validation.content_hash, current.content_hash):
            raise RulePreviewRejected("候选正文没有变化", status_code=422, code="no_change")
        if validation.line_count > MAX_CANDIDATE_LINES:
            raise RulePreviewRejected("候选正文超过行数预算", status_code=422, code="candidate_too_large")
        diff = self._diff(current.content, candidate)
        diff_bytes = len(diff.encode("utf-8"))
        diff_lines = diff.count("\n") + (1 if diff and not diff.endswith("\n") else 0)
        if diff_lines > MAX_DIFF_LINES or diff_bytes > MAX_DIFF_BYTES:
            raise RulePreviewRejected("差异超过预览预算", status_code=422, code="diff_too_large")

        now_timestamp = self._clock()
        now = datetime.fromtimestamp(now_timestamp, tz=timezone.utc)
        expires_at = now + timedelta(seconds=RulePreviewTokenService.TTL_SECONDS)
        change_id = "change-" + uuid.uuid4().hex
        diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        digest_payload = {
            "rule_id": rule_id, "change_id": change_id, "risk_level": spec.risk_level,
            "repository_revision": state.revision, "before_hash": current.content_hash,
            "after_hash": validation.content_hash, "diff_hash": diff_hash,
            "validator_version": VALIDATOR_VERSION,
        }
        preview_digest = self._digest(digest_payload)
        transaction = RuleChangeTransaction(
            change_id=change_id, rule_id=rule_id, action_id=RULE_UPDATE_ACTION_ID,
            risk_level="source_write", status="prepared", before_hash=current.content_hash,
            after_hash=validation.content_hash, diff_hash=diff_hash, preview_digest=preview_digest,
            validator_version=VALIDATOR_VERSION, repository_revision=state.revision,
            created_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
            updated_at=now.isoformat().replace("+00:00", "Z"),
        )
        # 在创建任何草稿目录前再次读取仓库和目标正文，缩小预览校验与落盘之间
        # 的 TOCTOU 窗口；任一 revision、工作区或正文摘要变化都要求重新预览。
        final_state = self._repository_state()
        final_current = self._read(rule_id)
        if (
            not final_state.ready
            or final_state.revision != state.revision
            or final_current.revision != final_state.revision
            or final_current.content_hash != current.content_hash
        ):
            raise RulePreviewRejected("控制仓在预览期间发生变化", code="repository_changed")
        directory = self._transaction_dir(change_id, now)
        try:
            # JSON 仅保存摘要和状态；candidate.md 是预览短期材料，绝不进入任务/审计。
            self._write_private(directory / "candidate.md", candidate)
            self._write_private(directory / "transaction.json", json.dumps(transaction.to_dict(), ensure_ascii=False, indent=2) + "\n")
            token = self._tokens.issue(
                _PreviewTokenRecord(
                    rule_id, change_id, spec.risk_level, state.revision, current.content_hash,
                    validation.content_hash, diff_hash, VALIDATOR_VERSION, preview_digest,
                    expires_at.timestamp(),
                )
            )
        except Exception as error:
            self._cleanup_new_draft(directory, change_id, now)
            raise RuleServiceError("无法创建规则预览材料") from error
        return {
            "rule_id": rule_id,
            "change_id": change_id,
            "source_content_hash": current.content_hash,
            "candidate_content_hash": validation.content_hash,
            "before_hash": current.content_hash,
            "after_hash": validation.content_hash,
            "diff_hash": diff_hash,
            "diff": diff,
            "repository_revision": state.revision,
            "validator_version": VALIDATOR_VERSION,
            "validation": validation.as_dict(),
            "preview_digest": preview_digest,
            "expires_at": transaction.expires_at,
            "expires_in_seconds": RulePreviewTokenService.TTL_SECONDS,
            "confirmation_token": token,
        }

    def _cleanup_new_draft(self, directory: Path, change_id: str, created_at: datetime) -> None:
        """仅删除本次新建且严格位于年月目录内的半成品草稿。"""
        if _SAFE_CHANGE_ID.fullmatch(change_id) is None:
            return
        expected_parent = (
            self._workspace_root / "runtime" / "web" / "rule-changes"
            / created_at.strftime("%Y") / created_at.strftime("%m")
        ).resolve()
        try:
            candidate = directory.resolve(strict=False)
            candidate.relative_to(expected_parent)
        except (OSError, ValueError):
            return
        if candidate.parent != expected_parent or candidate.name != change_id:
            return
        try:
            # 目录刚由本次调用以 exist_ok=False 创建，限定目标后递归清理不会触及
            # 同年月下其它变更；清理失败继续抛出通用服务错误而不泄漏路径。
            shutil.rmtree(candidate)
        except OSError as error:
            raise RuleServiceError("无法清理规则预览材料") from error
