"""阶段 4B 逐叶子恢复判定的纯逻辑测试。"""

from __future__ import annotations

import unittest

from aikb_web.core.maintenance_recovery import (
    CurrentLeafObservation,
    EnvironmentObservation,
    LeafRecoveryDecision,
    RecoveryContractError,
    RecoveryDecision,
    RecoveryLeaf,
    build_recovery_plan,
    decide_leaf,
)


class RecoveryTests(unittest.TestCase):
    """验证摘要匹配、第三方保护、环境缺失语义和逆序计划。"""

    def setUp(self) -> None:
        self.before = "b" * 64
        self.expected = "e" * 64
        self.leaf = RecoveryLeaf("agent.codex.mcp", "present", self.before, self.expected)

    def test_before_expected_and_third_party_decisions(self) -> None:
        self.assertEqual(decide_leaf(self.leaf, CurrentLeafObservation("present", self.before)).decision, RecoveryDecision.NOOP)
        self.assertEqual(decide_leaf(self.leaf, CurrentLeafObservation("present", self.expected)).decision, RecoveryDecision.RESTORE_BEFORE)
        self.assertEqual(decide_leaf(self.leaf, CurrentLeafObservation("present", "1" * 64)).decision, RecoveryDecision.THIRD_PARTY_CHANGED)
        pending = RecoveryLeaf("agent.codex.mcp", "present", self.before, self.expected, "pending")
        self.assertEqual(decide_leaf(pending, CurrentLeafObservation("present", self.expected)).decision, RecoveryDecision.THIRD_PARTY_CHANGED)
        rolled_back = RecoveryLeaf("agent.codex.mcp", "present", self.before, self.expected, "rolled_back")
        self.assertEqual(decide_leaf(rolled_back, CurrentLeafObservation("present", self.expected)).decision, RecoveryDecision.THIRD_PARTY_CHANGED)

    def test_missing_before_distinguishes_empty_environment(self) -> None:
        leaf = RecoveryLeaf("user_environment.aikb_home", "missing", None, self.expected)
        self.assertEqual(decide_leaf(leaf, EnvironmentObservation("missing", None)).decision, RecoveryDecision.NOOP)
        self.assertEqual(decide_leaf(leaf, EnvironmentObservation("empty", self.expected)).decision, RecoveryDecision.REMOVE_CREATED)
        self.assertEqual(decide_leaf(leaf, EnvironmentObservation("value", "9" * 64)).decision, RecoveryDecision.THIRD_PARTY_CHANGED)

    def test_invalid_observation_is_material_invalid_without_value_projection(self) -> None:
        with self.assertRaises(RecoveryContractError):
            CurrentLeafObservation("present", "not-a-hash")
        with self.assertRaises(RecoveryContractError):
            RecoveryLeaf(self.leaf.leaf_id, "missing", self.before, self.expected)

    def test_missing_observations_cannot_carry_hash(self) -> None:
        with self.assertRaises(RecoveryContractError):
            CurrentLeafObservation("missing", self.before)
        with self.assertRaises(RecoveryContractError):
            EnvironmentObservation("missing", self.before)

    def test_plan_uses_reverse_static_write_steps_only(self) -> None:
        leaves = (
            RecoveryLeaf("agent.codex.root_instructions", "present", "a" * 64, "d" * 64),
            RecoveryLeaf("agent.codex.mcp", "present", "b" * 64, "e" * 64),
            RecoveryLeaf("agent.codex.hooks", "present", "c" * 64, "f" * 64),
        )
        observations = {leaf.leaf_id: CurrentLeafObservation("present", leaf.expected_hash) for leaf in leaves}
        plan = build_recovery_plan("agent.codex", leaves, observations, ("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify"))
        self.assertEqual([item.step_id for item in plan], ["write_hooks", "write_mcp", "write_root_instructions"])
        self.assertTrue(all(item.decision == RecoveryDecision.RESTORE_BEFORE for item in plan))
        self.assertEqual(set(plan[0].to_dict()), {"step_id", "decision", "leaf_ids", "leaf_decisions"})

    def test_environment_plan_keeps_mixed_leaf_decisions(self) -> None:
        leaves = (
            RecoveryLeaf("user_environment.aikb_home", "present", "a" * 64, "b" * 64),
            RecoveryLeaf("user_environment.aikb_knowledge_home", "missing", None, "c" * 64),
        )
        observations = {
            leaves[0].leaf_id: EnvironmentObservation("value", "b" * 64),
            leaves[1].leaf_id: EnvironmentObservation("empty", "c" * 64),
        }
        plan = build_recovery_plan("environment", leaves, observations, ("preflight", "backup", "write_environment", "verify"))
        self.assertEqual(
            [item.decision for item in plan[0].leaf_decisions],
            [RecoveryDecision.RESTORE_BEFORE, RecoveryDecision.REMOVE_CREATED],
        )
        self.assertEqual(len(plan[0].to_dict()["leaf_decisions"]), 2)

    def test_target_and_order_are_strict(self) -> None:
        with self.assertRaises(RecoveryContractError):
            build_recovery_plan("../escape", (), {}, ())


if __name__ == "__main__":
    unittest.main()
