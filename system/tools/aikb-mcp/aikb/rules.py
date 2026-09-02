"""控制仓核心规则的静态注册与共享验证。

规则正文仍以控制仓 Markdown 为事实源；本模块只集中保存规则 ID、逻辑目标、
能力和结构约束，供 MCP/后续 Web 预览以及 ``validate-structure.ps1`` 共同调用。
这里不提供任意路径解析或写入能力，候选文件只会被读取并在内存中验证。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MAX_LINE_BYTES = 4 * 1024
MAX_CONTENT_BYTES = 64 * 1024
# 预算字段和 warning 语义在 v2 中固定；预览令牌也会绑定该版本，避免旧校验
# 结果在规则约束升级后被误用于正式应用。
VALIDATOR_VERSION = "phase4-rules-v2"


class RuleValidationError(ValueError):
    """表示规则 ID、规则目标或候选正文违反了固定安全契约。"""


@dataclass(frozen=True)
class RuleSpec:
    """描述一个受控规则目标的稳定能力边界，不携带物理根目录。"""

    rule_id: str
    relative_path: str
    title: str
    description: str
    readable: bool
    writable: bool
    risk: str
    recommended_chars: int
    max_chars: int
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleValidationResult:
    """返回规则验证的安全摘要和规范化正文；不会写回任何文件。"""

    rule_id: str
    valid: bool
    normalized_content: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    char_count: int
    byte_count: int
    line_count: int
    content_hash: str

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        """转换为服务端可投影的结果；默认不公开候选正文。"""
        result: dict[str, object] = {
            "rule_id": self.rule_id,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "char_count": self.char_count,
            "byte_count": self.byte_count,
            "line_count": self.line_count,
            "content_hash": self.content_hash,
            "validator_version": VALIDATOR_VERSION,
        }
        if include_content:
            result["normalized_content"] = self.normalized_content
        return result


_ROLE_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "entry": (
        "AIKB_HOME",
        "system/rules/AI_RULES.md",
        "system/rules/USER_RULES.md",
        "用户明确要求跳过时不接入",
        "根目录 `README.md` 是人类维护手册",
        "不属于 Agent 默认上下文",
    ),
    "user": (
        "当前任务及更高优先级指令优先",
        "未经用户明确要求不得修改",
        "Java",
        "使用中文",
        "对用户前提和事实保持独立判断",
        "无法可靠确认",
        "实际执行任何文件或目录删除前",
        "类/模块",
        "方法/函数",
        "关键代码",
        "无需通读全部代码",
        "不得逐行复述",
        "理解相关现有实现",
        "最小必要范围原则",
        "无法验证时明确说明",
        "依据项目环境或可靠文档确认",
    ),
    "agent": (
        "ENTRY_RULES.md",
        "INDEX.md",
        "read_knowledge",
        "search_knowledge",
        "content_hash",
        "workspace/",
        "work_id",
        "system/rules/CONTRIBUTING.md",
        "CATALOG.md",
        "Markdown 是知识事实源",
        "首次接入不默认读取控制仓或知识仓的 `INDEX.md`",
        "MCP 不可用",
        "用户要求跳过 AIKB",
        "不在每次任务后自动写库",
        "必须先确认",
    ),
    "contributing": (
        "content/inbox/",
        "system/templates/",
        "system/schemas/knowledge-entry.schema.json",
        "CATALOG.md",
        "INDEX.md",
        "`id`",
        "`relations`",
        "system/tests/validate-structure.ps1",
        "无需逐次确认",
        "请求用户决定",
        "不发布未通过校验的正式知识",
    ),
}

_FORBIDDEN_TERMS: Mapping[str, tuple[str, ...]] = {
    "entry": ("search_knowledge", "read_knowledge", "work_id", "CATALOG.md"),
    "user": ("search_knowledge", "read_knowledge", "work_id"),
    "agent": (),
    "contributing": (),
}

# INDEX.md 仍是结构入口而非可由 Web 规则 ID 访问的治理对象，因此保留为内部
# 结构约束，不把它加入对外注册表，避免误将索引开放为可写规则。
_AUXILIARY_CONSTRAINTS: Mapping[str, tuple[int, tuple[str, ...], tuple[str, ...]]] = {
    "INDEX.md": (
        800,
        (
            "稳定降级入口", "根目录 `README.md` 是人类维护手册", "AIKB_KNOWLEDGE_HOME",
            "%AIKB_HOME%\\content", "system/rules/CONTRIBUTING.md", "知识仓 `CATALOG.md`",
        ),
        ("work_id", "session_id", "relation_limit"),
    ),
}

# 该映射是唯一静态规则注册表。新增可写规则必须同时更新验证器、文档和回归测试。
_RULES: Mapping[str, RuleSpec] = {
    "entry": RuleSpec(
        "entry", "ENTRY_RULES.md", "入口规则", "Agent 接入引导，只提供审阅", True, False, "high", 800, 1200,
        _ROLE_REQUIREMENTS["entry"], _FORBIDDEN_TERMS["entry"],
    ),
    "user": RuleSpec(
        "user", "system/rules/USER_RULES.md", "个人使用规则", "跨 Agent 的个人偏好，首批唯一可修改规则", True, True, "high", 1000, 1600,
        _ROLE_REQUIREMENTS["user"], _FORBIDDEN_TERMS["user"],
    ),
    "agent": RuleSpec(
        "agent", "system/rules/AI_RULES.md", "AIKB Agent 规则", "AIKB 工作协议，只提供审阅", True, False, "high", 6000, 10000,
        _ROLE_REQUIREMENTS["agent"], _FORBIDDEN_TERMS["agent"],
    ),
    "contributing": RuleSpec(
        "contributing", "system/rules/CONTRIBUTING.md", "贡献规则", "正式知识贡献规范，只提供审阅", True, False, "high", 9000, 14000,
        _ROLE_REQUIREMENTS["contributing"], _FORBIDDEN_TERMS["contributing"],
    ),
}

# 覆盖 Windows 盘符、UNC、Unix 绝对路径；只检查规则正文，不把逻辑相对路径误判为绝对路径。
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
    re.compile(r"(?<!\w)\\\\[^\s\r\n]+"),
    # Unix 路径限定为 ASCII 路径段，避免把规则中的“类/模块”等普通中文斜杠短语误报；
    # 同时覆盖 /etc、/tmp 等单段根路径，并跳过 URL 中的协议分隔符。
    re.compile(r"(?<![A-Za-z0-9_\u4e00-\u9fff:/])/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+"),
)


def get_rule(rule_id: str) -> RuleSpec:
    """按稳定 ID 获取规则定义；未知 ID 统一抛出 ``RuleValidationError``。"""
    try:
        return _RULES[rule_id]
    except (KeyError, TypeError) as exc:
        raise RuleValidationError("未知规则 ID") from exc


def list_rules() -> tuple[RuleSpec, ...]:
    """返回固定注册顺序的规则定义，调用方不能通过结果动态注册规则。"""
    return tuple(_RULES.values())


def target_path(repo_root: Path, rule_id: str) -> Path:
    """解析静态规则目标；不接受调用方传入的物理路径或相对路径。"""
    spec = get_rule(rule_id)
    root = Path(repo_root).resolve()
    lexical_target = root / spec.relative_path
    if lexical_target.is_symlink():
        raise RuleValidationError("正式规则目标不能是符号链接")
    target = lexical_target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:  # 静态注册表变更出错时也不能逃逸仓库根目录。
        raise RuleValidationError("规则目标越出控制仓边界") from exc
    return target


def _decode_and_normalize(raw: bytes) -> str:
    """严格解码并规范换行；拒绝 BOM、NUL、替代字符和不可移植的超长行。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuleValidationError("规则必须使用 UTF-8 无 BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuleValidationError("规则不是合法 UTF-8") from exc
    if "\x00" in text:
        raise RuleValidationError("规则不得包含 NUL")
    if "\ufffd" in text:
        raise RuleValidationError("规则不得包含 U+FFFD")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line_number, line in enumerate(normalized.split("\n"), start=1):
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise RuleValidationError(f"规则单行超过 4 KiB：第 {line_number} 行")
    if len(normalized.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise RuleValidationError("规则超过 64 KiB 总预算")
    return normalized


def validate_content(rule_id: str, raw: str | bytes) -> RuleValidationResult:
    """验证规则正文的编码、预算、职责词、禁止词和绝对路径。

    ``raw`` 只在内存中处理；返回的规范化正文用于预览哈希，不会写回正式文件。
    """
    spec = get_rule(rule_id)
    data = b""
    try:
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        normalized = _decode_and_normalize(data)
    except (TypeError, ValueError, RuleValidationError) as exc:
        message = str(exc) or "规则正文无效"
        return RuleValidationResult(
            rule_id, False, "", (message,), (), 0, 0, 0, hashlib.sha256(data).hexdigest()
        )

    errors: list[str] = []
    warnings: list[str] = []
    char_count = len(normalized)
    byte_count = len(normalized.encode("utf-8"))
    if char_count > spec.max_chars:
        errors.append(f"核心规则超过字符预算：{spec.relative_path} = {char_count} > {spec.max_chars}")
    elif char_count > spec.recommended_chars:
        warnings.append(f"核心规则超过推荐字符预算：{spec.relative_path} = {char_count} > {spec.recommended_chars}")
    for required in spec.required_terms:
        if required not in normalized:
            errors.append(f"{spec.relative_path} 缺少职责闭环：{required}")
    for forbidden in spec.forbidden_terms:
        if forbidden in normalized:
            errors.append(f"{spec.relative_path} 混入其他层职责：{forbidden}")
    for pattern in _ABSOLUTE_PATH_PATTERNS:
        if pattern.search(normalized):
            errors.append(f"{spec.relative_path} 不得包含机器绝对路径")
            break
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return RuleValidationResult(
        rule_id, not errors, normalized, tuple(errors), tuple(warnings), char_count, byte_count,
        normalized.count("\n") + 1, digest,
    )


def validate_rule_file(rule_id: str, file_path: Path) -> RuleValidationResult:
    """读取并验证一个服务端指定的规则/候选文件；拒绝符号链接且不产生写入。"""
    get_rule(rule_id)
    path = Path(file_path)
    try:
        if path.is_symlink() or not path.is_file():
            raise RuleValidationError("候选文件必须是普通文件")
        raw = path.read_bytes()
    except (OSError, RuleValidationError) as exc:
        return RuleValidationResult(
            rule_id, False, "", (str(exc),), (), 0, 0, 0, hashlib.sha256(b"").hexdigest()
        )
    return validate_content(rule_id, raw)


def validate_registered_rules(repo_root: Path) -> dict[str, object]:
    """验证四个静态正式规则，作为结构脚本和服务启动门禁的共享入口。"""
    results: list[RuleValidationResult] = []
    for spec in list_rules():
        results.append(validate_rule_file(spec.rule_id, target_path(repo_root, spec.rule_id)))
    return {
        "valid": all(result.valid for result in results),
        "validator_version": VALIDATOR_VERSION,
        "rules": [result.as_dict() for result in results],
    }


def validate_auxiliary_file(relative_path: str, file_path: Path) -> RuleValidationResult:
    """验证不对外注册的结构入口（目前仅 ``INDEX.md``），不接受动态规则 ID。"""
    try:
        max_chars, required_terms, forbidden_terms = _AUXILIARY_CONSTRAINTS[relative_path]
    except KeyError as exc:
        raise RuleValidationError("未知结构入口") from exc
    try:
        path = Path(file_path)
        if path.is_symlink() or not path.is_file():
            raise RuleValidationError("结构入口必须是普通文件")
        raw = path.read_bytes()
        normalized = _decode_and_normalize(raw)
    except (OSError, RuleValidationError) as exc:
        return RuleValidationResult(
            relative_path, False, "", (str(exc),), (), 0, 0, 0, hashlib.sha256(b"").hexdigest()
        )
    errors: list[str] = []
    if len(normalized) > max_chars:
        errors.append(f"核心规则超过字符预算：{relative_path} = {len(normalized)} > {max_chars}")
    for required in required_terms:
        if required not in normalized:
            errors.append(f"{relative_path} 缺少职责闭环：{required}")
    for forbidden in forbidden_terms:
        if forbidden in normalized:
            errors.append(f"{relative_path} 混入其他层职责：{forbidden}")
    if any(pattern.search(normalized) for pattern in _ABSOLUTE_PATH_PATTERNS):
        errors.append(f"{relative_path} 不得包含机器绝对路径")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return RuleValidationResult(
        relative_path, not errors, normalized, tuple(errors), (), len(normalized),
        len(normalized.encode("utf-8")), normalized.count("\n") + 1, digest,
    )


def validate_structure_rules(repo_root: Path) -> dict[str, object]:
    """验证四个注册规则与 INDEX.md 的全部共享核心约束。"""
    root = Path(repo_root).resolve()
    results = [validate_rule_file(spec.rule_id, target_path(root, spec.rule_id)) for spec in list_rules()]
    index = validate_auxiliary_file("INDEX.md", root / "INDEX.md")
    return {
        "valid": all(result.valid for result in (*results, index)),
        "validator_version": VALIDATOR_VERSION,
        "rules": [result.as_dict() for result in (*results, index)],
    }


def validate_candidate_file(rule_id: str, candidate_file: Path) -> RuleValidationResult:
    """执行候选覆盖校验；候选只读，正式规则目标不会被触碰。"""
    spec = get_rule(rule_id)
    if not spec.writable:
        return RuleValidationResult(rule_id, False, "", ("该规则只读，不允许候选覆盖",), (), 0, 0, 0, hashlib.sha256(b"").hexdigest())
    return validate_rule_file(rule_id, candidate_file)
