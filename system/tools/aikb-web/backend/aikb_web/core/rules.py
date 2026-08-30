"""阶段 4A 的静态规则注册表和安全读模型契约。

规则目标由受版本控制的注册表决定。浏览器和上层 API 只应携带 ``rule_id``，
不能把物理路径、磁盘根目录或路径拼接结果作为输入；物理目标映射因此刻意
留在尚未实现的事务执行层，而不进入本模块的数据模型或公开投影。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from aikb.rules import list_rules as list_shared_rules


class RuleError(ValueError):
    """规则 ID、正文编码或规则读模型不符合静态契约。"""


RULE_IDS = ("entry", "user", "agent", "contributing")
RULE_RISK_LEVEL = "source_write"
MAX_RULE_BYTES = 64 * 1024
MAX_RULE_LINE_BYTES = 4 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class RuleSpec:
    """一个不可变规则能力描述，不包含可由调用者覆盖的路径字段。"""

    rule_id: str
    title: str
    description: str
    readable: bool
    writable: bool
    risk_level: str
    max_chars: int

    def public_dict(
        self,
        *,
        content_hash: str | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        """返回规则目录安全投影；可选哈希和 revision 也不暴露物理路径。"""
        if content_hash is not None:
            _validate_hash(content_hash, "content_hash")
        if revision is not None:
            _validate_revision(revision)
        result: dict[str, Any] = {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "readable": self.readable,
            "writable": self.writable,
            "risk_level": self.risk_level,
            "max_chars": self.max_chars,
        }
        if content_hash is not None:
            result["content_hash"] = content_hash
        if revision is not None:
            result["revision"] = revision
        return result


@dataclass(frozen=True)
class RuleReadModel:
    """规则详情读模型；正文是服务端读取的内容，不是浏览器提供的路径。"""

    spec: RuleSpec
    content: str
    content_hash: str
    revision: str

    def __post_init__(self) -> None:
        """校验详情模型的摘要字段，避免把任意文本当作安全元数据。"""
        if not isinstance(self.content, str):
            raise RuleError("规则正文必须是文本")
        _validate_hash(self.content_hash, "content_hash")
        _validate_revision(self.revision)

    def public_dict(self) -> dict[str, Any]:
        """生成详情投影；该投影没有路径、磁盘根目录或候选 diff。"""
        result = self.spec.public_dict(content_hash=self.content_hash, revision=self.revision)
        result["content"] = self.content
        return result


def _validate_hash(value: str, field_name: str) -> str:
    """校验 SHA-256 字段的固定格式，防止把正文或路径混入摘要。"""
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise RuleError(f"{field_name} 必须是 64 位小写十六进制摘要")
    return value


def _validate_revision(value: str) -> str:
    """校验 Git revision 的安全短/长格式；不接受任意描述文本。"""
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise RuleError("revision 必须是 Git 十六进制 revision")
    return value


def normalize_rule_content(content: str, *, max_chars: int) -> str:
    """规范化候选规则正文并执行通用编码预算检查。

    这里仅处理跨平台字节表示和通用安全边界；职责词、禁止词及可移植性等
    项目规则仍由共享规则验证器负责，避免 Web 层复制另一套验证事实。
    """
    if not isinstance(content, str):
        raise RuleError("规则正文必须是文本")
    if "\x00" in content or "\ufffd" in content:
        raise RuleError("规则正文包含禁止字符")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in content):
        raise RuleError("规则正文包含无效 Unicode 字符")
    if content.startswith("\ufeff"):
        raise RuleError("规则必须使用 UTF-8 无 BOM")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > max_chars:
        raise RuleError("规则正文超过字符预算")
    if len(normalized.encode("utf-8")) > MAX_RULE_BYTES:
        raise RuleError("规则正文超过字节预算")
    for line in normalized.split("\n"):
        if len(line.encode("utf-8")) > MAX_RULE_LINE_BYTES:
            raise RuleError("规则单行超过字节预算")
    return normalized


def rule_content_hash(content: str, *, max_chars: int) -> str:
    """按规则规范化文本计算稳定 SHA-256 摘要。"""
    normalized = normalize_rule_content(content, max_chars=max_chars)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_SHARED_RULES = {item.rule_id: item for item in list_shared_rules()}
if set(_SHARED_RULES) != set(RULE_IDS):
    # Web 规则能力不能在共享核心缺项时用本地副本继续运行，否则两层会形成
    # 不同的可写白名单。导入阶段直接失败关闭，交给应用健康状态报告能力缺失。
    raise RuntimeError("共享规则注册表与 Web 固定规则 ID 不一致")

# 公开读模型只抽取共享注册表的语义字段，刻意丢弃 relative_path。
_RULE_SPECS: tuple[RuleSpec, ...] = tuple(
    RuleSpec(
        item.rule_id,
        item.title,
        item.description,
        item.readable,
        item.writable,
        RULE_RISK_LEVEL if item.writable else "read_only",
        item.max_chars,
    )
    for item in (_SHARED_RULES[key] for key in RULE_IDS)
)
_RULE_REGISTRY: Mapping[str, RuleSpec] = MappingProxyType({item.rule_id: item for item in _RULE_SPECS})


class RuleRegistry:
    """提供固定规则能力查询和服务端正文构造的安全读模型。"""

    def __init__(self, specs: Mapping[str, RuleSpec] | None = None):
        """绑定静态注册表；自定义表仅用于隔离测试，生产不读配置文件。"""
        table = dict(specs or _RULE_REGISTRY)
        if set(table) != set(RULE_IDS):
            raise RuleError("规则注册表必须完整包含固定规则 ID")
        if any(key != value.rule_id for key, value in table.items()):
            raise RuleError("规则注册表键与 rule_id 不一致")
        if any(value.writable and value.rule_id != "user" for value in table.values()):
            raise RuleError("只有 user 规则允许写入")
        self._specs = MappingProxyType(table)

    def list(self, *, content_hashes: Mapping[str, str] | None = None, revision: str | None = None) -> list[dict[str, Any]]:
        """按 ID 稳定列出公开能力，可选附加服务端读取的摘要和 revision。"""
        hashes = content_hashes or {}
        unknown = set(hashes) - set(self._specs)
        if unknown:
            raise RuleError("内容摘要包含未知规则 ID")
        return [
            self._specs[key].public_dict(content_hash=hashes.get(key), revision=revision)
            for key in sorted(self._specs)
        ]

    def get(self, rule_id: str) -> RuleSpec:
        """按固定逻辑 ID 获取规则；未知值不回退到路径或默认目标。"""
        if not isinstance(rule_id, str) or rule_id not in self._specs:
            raise RuleError("未知规则 ID")
        return self._specs[rule_id]

    def read_model(self, rule_id: str, content: str, revision: str) -> RuleReadModel:
        """用服务端已读取正文构造详情模型，并重新计算规范化内容摘要。"""
        spec = self.get(rule_id)
        normalized = normalize_rule_content(content, max_chars=spec.max_chars)
        return RuleReadModel(spec, normalized, rule_content_hash(normalized, max_chars=spec.max_chars), revision)

    def public_list(self, **kwargs: Any) -> list[dict[str, Any]]:
        """``list`` 的语义别名，供 API 适配层明确表达公开投影意图。"""
        return self.list(**kwargs)
