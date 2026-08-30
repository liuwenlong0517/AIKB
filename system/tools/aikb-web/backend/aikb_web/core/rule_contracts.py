"""阶段 4A 规则契约的稳定聚合导出入口。"""

from .rule_changes import (
    RULE_CHANGE_STATUSES,
    RULE_UPDATE_ACTION_ID,
    RULE_UPDATE_EFFECT,
    RULE_USER_UPDATE_SPEC,
    RuleChangeTransaction,
    RuleUpdateActionSpec,
)
from .rules import (
    RULE_IDS,
    RuleError,
    RuleReadModel,
    RuleRegistry,
    RuleSpec,
    normalize_rule_content,
    rule_content_hash,
)

__all__ = [
    "RULE_CHANGE_STATUSES",
    "RULE_IDS",
    "RULE_UPDATE_ACTION_ID",
    "RULE_UPDATE_EFFECT",
    "RULE_USER_UPDATE_SPEC",
    "RuleChangeTransaction",
    "RuleError",
    "RuleReadModel",
    "RuleRegistry",
    "RuleSpec",
    "RuleUpdateActionSpec",
    "normalize_rule_content",
    "rule_content_hash",
]
