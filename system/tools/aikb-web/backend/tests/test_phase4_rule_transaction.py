"""阶段 4A 波次 2 规则事务原子写入与恢复测试。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aikb_web.core.rule_preview import RepositoryState, RulePreviewService
from aikb_web.core.rule_transaction import RuleChangeStore, RuleTransactionError, RuleTransactionExecutor


class RuleTransactionTests(unittest.TestCase):
    """在临时控制仓覆盖成功、冲突、失败回滚和启动恢复边界。"""

    def setUp(self) -> None:
        """复制规则事实源到临时目录，任何正式文件写入都只发生在副本。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-rule-transaction-")
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.workspace = root / "workspace"
        (self.repo / "system" / "rules").mkdir(parents=True)
        source_root = Path(__file__).resolve().parents[5]
        for relative in (
            "ENTRY_RULES.md", "INDEX.md", "system/rules/USER_RULES.md",
            "system/rules/AI_RULES.md", "system/rules/CONTRIBUTING.md",
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, target)
        settings = SimpleNamespace(repo_root=self.repo, workspace_root=self.workspace)
        self.service = RulePreviewService(settings)
        self.state = RepositoryState("a" * 40, "main", True, True, False)
        self.service_state = patch.object(self.service, "_repository_state", return_value=self.state)
        self.service_state.start()
        self.detail = self.service.get_rule("user")

    def tearDown(self) -> None:
        """结束状态桩并释放临时仓库。"""
        self.service_state.stop()
        self.temp.cleanup()

    def _preview(self) -> tuple[RuleTransactionExecutor, dict[str, object]]:
        """创建合法预览并返回未消费令牌和事务执行器。"""
        candidate = str(self.detail["content"]) + "\n# transaction-test\n"
        preview = self.service.preview(
            "user", base_content_hash=str(self.detail["content_hash"]), candidate_content=candidate,
        )
        return RuleTransactionExecutor(self.service), preview

    @staticmethod
    def _apply(executor: RuleTransactionExecutor, preview: dict[str, object], *, task_id: str = "task-transaction") -> dict[str, object]:
        """先原子认领事务，再以绑定 task_id 应用，模拟协调器的内部调用。"""
        change_id = str(preview["change_id"])
        executor.claim(change_id, task_id)
        return executor.apply(
            change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), task_id,
        )

    def test_success_keeps_material_until_finalize_success(self) -> None:
        """成功应用先保留正文材料，终态审计成功后才允许 finalize 清理。"""
        executor, preview = self._preview()
        result = self._apply(executor, preview)
        self.assertEqual(result["status"], "succeeded")
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        self.assertIn("transaction-test", target.read_text(encoding="utf-8"))
        store = RuleChangeStore(self.workspace)
        directory = store._directory(str(preview["change_id"]))
        self.assertTrue((directory / "transaction.json").is_file())
        self.assertTrue((directory / "candidate.md").is_file())
        self.assertTrue((directory / "backup.md").is_file())
        executor.finalize_success(str(preview["change_id"]), "task-transaction")
        self.assertFalse((directory / "candidate.md").exists())
        self.assertFalse((directory / "backup.md").exists())

    def test_lock_conflict_does_not_consume_token(self) -> None:
        """全仓锁冲突在令牌消费前返回，释放锁后同一预览仍可成功。"""
        executor, preview = self._preview()
        with executor._acquire_repository():
            with self.assertRaises(RuleTransactionError):
                self._apply(executor, preview)
        result = self._apply(executor, preview)
        self.assertEqual(result["status"], "succeeded")

    def test_two_executors_share_nonblocking_workspace_lock(self) -> None:
        """两个独立执行器实例共享固定 workspace 锁，第二者不能同时进入。"""
        executor, _preview = self._preview()
        other = RuleTransactionExecutor(self.service)
        with executor._acquire_repository():
            with self.assertRaises(RuleTransactionError):
                with other._acquire_repository():
                    pass

    def test_two_executors_have_one_atomic_claim(self) -> None:
        """两个 executor 只能让一个 task_id 认领同一 prepared 事务。"""
        executor, preview = self._preview()
        other = RuleTransactionExecutor(self.service)
        change_id = str(preview["change_id"])
        executor.claim(change_id, "task-one")
        with self.assertRaises(RuleTransactionError):
            other.claim(change_id, "task-two")
        with self.assertRaises(RuleTransactionError):
            executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-two")
        result = executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-one")
        self.assertEqual(result["status"], "succeeded")

    def test_release_claim_allows_safe_retry_before_apply(self) -> None:
        """任务创建失败时释放 prepared 认领，令牌仍可由新任务使用。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        executor.claim(change_id, "task-abandoned")
        executor.release_claim(change_id, "task-abandoned")
        self.assertEqual(executor.prepare(change_id, str(preview["confirmation_token"]))["task_id"], None)
        result = self._apply(executor, preview, task_id="task-retry")
        self.assertEqual(result["status"], "succeeded")

    def test_root_symlink_boundary_is_rejected_without_symlink_privilege(self) -> None:
        """固定运行面任一路径段被模拟为链接时，存储层立即拒绝。"""
        store = RuleChangeStore(self.workspace)
        original = Path.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path.name == "rule-changes" or original(path)

        with patch.object(Path, "is_symlink", fake_is_symlink):
            with self.assertRaises(RuleTransactionError):
                store._runtime_root()

    def test_recovery_reports_damaged_transaction_material(self) -> None:
        """损坏 transaction.json 不能从启动扫描中静默消失。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        directory = RuleChangeStore(self.workspace)._directory(change_id)
        (directory / "transaction.json").write_text("{not-json", encoding="utf-8")
        result = executor.recover()
        self.assertEqual(result[0]["status"], "recovery_required")
        self.assertEqual(result[0]["change_id"], change_id)

    def test_revision_or_hash_conflict_happens_before_token_consumption(self) -> None:
        """revision/正文冲突拒绝应用，且事务仍未进入 applying。"""
        executor, preview = self._preview()
        self.service_state.stop()
        self.service_state = patch.object(self.service, "_repository_state", return_value=RepositoryState("b" * 40, "main", True, True, False))
        self.service_state.start()
        with self.assertRaises(RuleTransactionError):
            self._apply(executor, preview)
        transaction = RuleChangeStore(self.workspace).load(str(preview["change_id"]))
        self.assertEqual(transaction.status, "prepared")

    def test_wrong_token_does_not_consume_valid_preview(self) -> None:
        """令牌绑定失败不消耗正确令牌，随后仍可完成同一预览。"""
        executor, preview = self._preview()
        with self.assertRaises(RuleTransactionError):
            executor.claim(str(preview["change_id"]), "task-transaction")
            executor.apply(str(preview["change_id"]), "invalid-token", str(preview["preview_digest"]), "task-transaction")
        transaction = RuleChangeStore(self.workspace).load(str(preview["change_id"]))
        self.assertEqual(transaction.status, "prepared")
        result = executor.apply(str(preview["change_id"]), str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-transaction")
        self.assertEqual(result["status"], "succeeded")

    def test_prepare_peeks_without_consuming_and_wrong_token_isolated(self) -> None:
        """prepare 只预检不消费；错误令牌不会影响后续正确预检和应用。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        with self.assertRaises(RuleTransactionError):
            executor.prepare(change_id, "wrong-token")
        prepared = executor.prepare(change_id, str(preview["confirmation_token"]))
        self.assertEqual(prepared["status"], "prepared")
        executor.claim(change_id, "task-transaction")
        result = executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-transaction")
        self.assertEqual(result["status"], "succeeded")

    def test_symlink_material_is_rejected(self) -> None:
        """事务目录或 candidate 符号链接不得被当作服务端材料读取。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        store = RuleChangeStore(self.workspace)
        directory = store._directory(change_id)
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        candidate = directory / "candidate.md"
        candidate.unlink()
        try:
            candidate.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 测试环境不允许创建符号链接")
        with self.assertRaises(RuleTransactionError):
            store.candidate_path(change_id)

    def test_replace_failure_rolls_back_and_keeps_backup(self) -> None:
        """os.replace 前后任一写入异常都回滚并保留 backup 供人工检查。"""
        executor, preview = self._preview()
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        original = target.read_bytes()
        with patch.object(executor, "_replace_candidate", side_effect=RuleTransactionError("injected replace failure")):
            result = self._apply(executor, preview)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(RuleChangeStore(self.workspace).backup_path(str(preview["change_id"])).is_file())

    def test_formal_validation_failure_rolls_back(self) -> None:
        """正式文件复核失败时恢复原始字节并进入 rolled_back。"""
        executor, preview = self._preview()
        with patch.object(executor, "_full_validation", side_effect=(True, False, True)):
            result = self._apply(executor, preview)
        self.assertEqual(result["status"], "rolled_back")
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        self.assertNotIn("transaction-test", target.read_text(encoding="utf-8"))

    def test_state_save_failure_after_token_consumption_is_uncertain(self) -> None:
        """令牌消费后 applying 落盘失败必须抛出不确定态，而非普通拒绝。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        executor.claim(change_id, "task-save-failure")
        original_save = executor._store.save

        def fail_applying(transaction):
            if transaction.status == "applying":
                raise OSError("injected state failure")
            return original_save(transaction)

        with patch.object(executor._store, "save", side_effect=fail_applying):
            with self.assertRaises(Exception) as context:
                executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-save-failure")
        self.assertEqual(context.exception.__class__.__name__, "RuleTransactionUncertain")
        self.assertEqual(RuleChangeStore(self.workspace).load(change_id).status, "prepared")

    def test_third_party_change_before_replace_enters_recovery_without_overwrite(self) -> None:
        """替换紧前发现第三方修改时保留第三方正文并进入恢复态。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        executor.claim(change_id, "task-race")
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        original_current = executor._verified_before_bytes
        calls = 0

        def race_check(path, expected_hash, *, after_claim=False):
            nonlocal calls
            calls += 1
            if after_claim and calls >= 2:
                path.write_bytes(b"# third-party\n")
            return original_current(path, expected_hash, after_claim=after_claim)

        with patch.object(executor, "_verified_before_bytes", side_effect=race_check):
            with self.assertRaises(Exception):
                executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-race")
        self.assertEqual(target.read_bytes(), b"# third-party\n")
        self.assertEqual(RuleChangeStore(self.workspace).load(change_id).status, "recovery_required")

    def test_audit_failure_keeps_backup_until_recovery_decision(self) -> None:
        """终态审计失败只转人工恢复，不能丢失唯一 backup。"""
        executor, preview = self._preview()
        change_id = str(preview["change_id"])
        executor.claim(change_id, "task-audit")
        self.assertEqual(
            executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), "task-audit")["status"],
            "succeeded",
        )
        result = executor.mark_audit_failure(change_id, "task-audit")
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(RuleChangeStore(self.workspace).backup_path(change_id).is_file())

    def test_recovery_rolls_back_interrupted_candidate(self) -> None:
        """启动扫描发现候选 hash 时使用 backup 恢复并标记 rolled_back。"""
        executor, preview = self._preview()
        store = RuleChangeStore(self.workspace)
        change_id = str(preview["change_id"])
        transaction = store.load(change_id)
        store.save(transaction.transition("applying"))
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        backup = store.backup_path(change_id)
        backup.write_bytes(target.read_bytes())
        candidate = store.candidate_path(change_id).read_bytes()
        target.write_bytes(candidate)
        result = executor.recover()
        self.assertEqual(result[0]["status"], "rolled_back")
        self.assertEqual(target.read_bytes(), backup.read_bytes())

    def test_recovery_never_overwrites_third_party_change(self) -> None:
        """中断事务目标已被第三方修改时转人工恢复，不能覆盖第三方正文。"""
        executor, preview = self._preview()
        store = RuleChangeStore(self.workspace)
        change_id = str(preview["change_id"])
        transaction = store.load(change_id)
        store.save(transaction.transition("applying"))
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        store.backup_path(change_id).write_bytes(target.read_bytes())
        third_party = b"third party change\n"
        target.write_bytes(third_party)
        result = executor.recover()
        self.assertEqual(result[0]["status"], "recovery_required")
        self.assertEqual(target.read_bytes(), third_party)


if __name__ == "__main__":
    unittest.main()
