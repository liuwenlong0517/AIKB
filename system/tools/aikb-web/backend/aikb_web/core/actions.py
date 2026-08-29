"""阶段 3 波次 1 的静态动作准入、预览和确认令牌。

本模块刻意不执行动作，也不从用户可修改的配置加载程序。动作注册表是受
版本控制的静态 Python 数据，后续 API/任务服务只能消费这里产生的规范化预览。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


class ActionError(ValueError):
    """动作不存在、参数不符合静态 Schema 或确认令牌无效。"""


@dataclass(frozen=True)
class ActionSpec:
    """一个不可变动作准入描述；不包含可执行命令行或用户可覆盖路径。"""

    action_id: str
    title: str
    description: str
    supported_platforms: tuple[str, ...]
    risk_level: str
    effects: tuple[str, ...]
    executor_kind: str
    program_key: str
    working_directory: str
    timeout_seconds: int
    concurrency_group: str
    concurrency_limit: int
    parameter_schema: Mapping[str, Any]
    confirmation_required: bool = True

    def public_dict(self) -> dict[str, Any]:
        """返回可供预览/API 使用的能力字段，不公开命令数组或物理路径。"""
        return {
            "action_id": self.action_id,
            "title": self.title,
            "description": self.description,
            "supported_platforms": list(self.supported_platforms),
            "risk_level": self.risk_level,
            "effects": list(self.effects),
            "executor_kind": self.executor_kind,
            "program_key": self.program_key,
            "timeout_seconds": self.timeout_seconds,
            "concurrency_group": self.concurrency_group,
            "concurrency_limit": self.concurrency_limit,
            "parameter_schema": dict(self.parameter_schema),
            "confirmation_required": self.confirmation_required,
        }


def _empty_schema() -> Mapping[str, Any]:
    """构造首批无参数动作的严格 Schema，明确拒绝未知字段。"""
    return MappingProxyType({"type": "object", "properties": {}, "additionalProperties": False})


_ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        "validate.structure", "结构校验", "读取控制仓与知识仓并执行结构校验。", ("windows",),
        "read_only", ("read:control_repository", "read:knowledge_repository"), "trusted_executor", "pwsh",
        "control_repository", 120, "structure_validation", 1, _empty_schema(), False,
    ),
    ActionSpec(
        "repository.status.control", "控制仓状态", "读取控制仓 branch、revision 和工作区状态。", ("windows",),
        "read_only", ("read:control_repository",), "trusted_executor", "git", "control_repository", 15,
        "repository_status", 2, _empty_schema(), False,
    ),
    ActionSpec(
        "repository.status.knowledge", "知识仓状态", "读取知识仓 branch、revision 和工作区状态。", ("windows",),
        "read_only", ("read:knowledge_repository",), "trusted_executor", "git", "knowledge_repository", 15,
        "repository_status", 2, _empty_schema(), False,
    ),
)
_ACTION_REGISTRY: Mapping[str, ActionSpec] = MappingProxyType({item.action_id: item for item in _ACTION_SPECS})


def _canonical(value: Any) -> str:
    """将已校验参数转为稳定 JSON，供摘要和令牌绑定使用。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ActionError("参数无法规范化") from error


class ActionRegistry:
    """提供静态动作查询、严格参数校验和不可伪造的预览摘要。"""

    def __init__(self, specs: Mapping[str, ActionSpec] | None = None):
        """绑定静态注册表；自定义表仅供隔离测试，生产默认使用内置注册表。"""
        self._specs = MappingProxyType(dict(specs or _ACTION_REGISTRY))

    def list(self) -> list[dict[str, Any]]:
        """按 action_id 稳定列出公开能力。"""
        return [self._specs[key].public_dict() for key in sorted(self._specs)]

    def get(self, action_id: str) -> ActionSpec:
        """获取动作规格；未知动作不会回退到任意命令或默认执行器。"""
        spec = self._specs.get(action_id)
        if spec is None:
            raise ActionError("未知动作")
        return spec

    def normalize_parameters(self, action_id: str, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
        """按动作 Schema 生成规范化参数；首批动作严格只接受空对象。"""
        spec = self.get(action_id)
        if parameters is None:
            candidate: Mapping[str, Any] = {}
        elif isinstance(parameters, Mapping):
            candidate = parameters
        else:
            raise ActionError("动作参数必须是 JSON 对象")
        schema = spec.parameter_schema
        if schema.get("additionalProperties") is False:
            properties = schema.get("properties", {})
            unknown = [str(key) for key in candidate if key not in properties]
            if unknown:
                raise ActionError("动作包含未允许参数")
        required = schema.get("required", ())
        missing = [str(key) for key in required if key not in candidate]
        if missing:
            raise ActionError("动作缺少必填参数")
        # 首批参数全部为空 Schema；这里仍保留基本类型检查，防止未来扩展绕过边界。
        for key, value in candidate.items():
            expected = schema.get("properties", {}).get(key, {}).get("type")
            if expected == "string" and not isinstance(value, str):
                raise ActionError("动作参数类型无效")
            if expected == "array" and not isinstance(value, list):
                raise ActionError("动作参数类型无效")
        return json.loads(_canonical(dict(candidate)))

    def preview(self, action_id: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """生成服务端规范化预览及摘要哈希；不触发执行或写入。"""
        spec = self.get(action_id)
        normalized = self.normalize_parameters(action_id, parameters)
        digest_input = {
            "action_id": spec.action_id, "parameters": normalized, "risk_level": spec.risk_level,
            "effects": list(spec.effects), "timeout_seconds": spec.timeout_seconds,
            "concurrency_group": spec.concurrency_group,
        }
        digest = hashlib.sha256(_canonical(digest_input).encode("utf-8")).hexdigest()
        return {
            "action_id": spec.action_id,
            "parameters": normalized,
            "steps": [f"使用受信任 {spec.program_key} 执行器读取指定仓库"],
            "risk_level": spec.risk_level,
            "effects": list(spec.effects),
            "timeout_seconds": spec.timeout_seconds,
            "concurrency_group": spec.concurrency_group,
            "confirmation_required": spec.confirmation_required,
            "preview_digest": digest,
        }


@dataclass(frozen=True)
class _TokenRecord:
    """仅存在进程内的确认令牌绑定信息。"""

    action_id: str
    parameters_digest: str
    risk_level: str
    preview_digest: str
    expires_at: float


class ConfirmationTokenService:
    """签发五分钟、单次消费且绑定预览内容的进程内确认令牌。"""

    TTL_SECONDS = 300
    MAX_ACTIVE_TOKENS = 1024

    def __init__(self, clock: Any = time.time):
        """生成本进程随机密钥；密钥与令牌均不写入 workspace、日志或前端。"""
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._records: dict[str, _TokenRecord] = {}
        # 令牌消费是跨线程的一次性事务；RLock 也允许未来的清理逻辑复用内部方法。
        self._lock = threading.RLock()

    def issue(self, *, action_id: str, parameters: Mapping[str, Any], risk_level: str, preview_digest: str) -> str:
        """为已生成预览的规范化参数签发短期令牌。"""
        parameters_digest = hashlib.sha256(_canonical(dict(parameters)).encode("utf-8")).hexdigest()
        nonce = secrets.token_urlsafe(32)
        token = hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).hexdigest() + "." + nonce
        with self._lock:
            now = self._clock()
            self._records = {key: record for key, record in self._records.items() if record.expires_at > now}
            if len(self._records) >= self.MAX_ACTIVE_TOKENS:
                raise ActionError("待确认预览过多，请稍后重试")
            self._records[token] = _TokenRecord(
                action_id, parameters_digest, risk_level, preview_digest,
                now + self.TTL_SECONDS,
            )
        return token

    def consume(
        self, token: str, *, action_id: str, parameters: Mapping[str, Any], risk_level: str, preview_digest: str,
    ) -> None:
        """校验并一次性消费令牌；错误绑定不会消耗仍可用于正确请求的令牌。"""
        with self._lock:
            record = self._records.get(token)
            if record is None:
                raise ActionError("确认令牌无效或已消费")
            if self._clock() >= record.expires_at:
                # 在锁内删除，避免两个并发消费者一个成功、另一个泄漏 KeyError。
                self._records.pop(token, None)
                raise ActionError("确认令牌已过期")
            parameters_digest = hashlib.sha256(_canonical(dict(parameters)).encode("utf-8")).hexdigest()
            valid = (
                hmac.compare_digest(record.action_id, action_id)
                and hmac.compare_digest(record.parameters_digest, parameters_digest)
                and hmac.compare_digest(record.risk_level, risk_level)
                and hmac.compare_digest(record.preview_digest, preview_digest)
            )
            if not valid:
                raise ActionError("确认令牌与预览不匹配")
            self._records.pop(token, None)
