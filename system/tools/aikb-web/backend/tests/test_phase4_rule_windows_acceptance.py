"""阶段 4A 波次 3 Windows 隔离仓与独立进程验收。

测试只操作临时复制的最小控制 Git 仓；常规测试门禁默认跳过，必须显式设置
``AIKB_RUN_WINDOWS_ACCEPTANCE=1`` 才执行，避免 Windows 文件锁/进程时序影响普通回归。
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aikb_web.core.rule_changes import RuleChangeTransaction
from aikb_web.core.rule_changes import RULE_USER_UPDATE_SPEC
from aikb_web.core.rule_preview import RulePreviewService
from aikb_web.core.rule_task import RuleChangeTaskCoordinator
from aikb_web.core.rule_transaction import RuleChangeStore, RuleTransactionError, RuleTransactionExecutor
from aikb_web.core.tasks import TaskStore


def _claim_worker(workspace: str, repo: str, change_id: str, task_id: str, result_queue) -> None:
    """独立 Python 进程只执行 claim，验证固定 workspace 锁和原子事务认领。"""
    try:
        service = SimpleNamespace(_workspace_root=Path(workspace), _repo_root=Path(repo))
        outcome = RuleTransactionExecutor(service).claim(change_id, task_id)
        result_queue.put(("ok", outcome.get("task_id")))
    except Exception as error:  # 子进程只回传固定类别，不能泄露临时路径。
        result_queue.put(("error", type(error).__name__))


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("AIKB_RUN_WINDOWS_ACCEPTANCE") == "1",
    "设置 AIKB_RUN_WINDOWS_ACCEPTANCE=1 后运行 Windows 隔离仓验收",
)
class Phase4RuleWindowsAcceptanceTests(unittest.TestCase):
    """使用真实临时 Git 仓覆盖 Windows 写入、恢复和进程竞争边界。"""

    def setUp(self) -> None:
        """复制规则事实源并初始化隔离控制仓，绝不触碰真实 USER_RULES.md。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-phase4-windows-")
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.workspace = root / "workspace"
        source_root = Path(__file__).resolve().parents[5]
        for relative in (
            "ENTRY_RULES.md", "INDEX.md", "system/rules/USER_RULES.md",
            "system/rules/AI_RULES.md", "system/rules/CONTRIBUTING.md",
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, target)
        self._git("init", "--quiet")
        self._git("config", "user.email", "aikb-acceptance@example.invalid")
        self._git("config", "user.name", "AIKB Windows acceptance")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "isolated acceptance baseline")
        self.service = RulePreviewService(SimpleNamespace(repo_root=self.repo, workspace_root=self.workspace))

    def tearDown(self) -> None:
        """清理临时仓库和运行面。"""
        self.temp.cleanup()

    def _git(self, *args: str) -> str:
        """执行临时仓只读/初始化 Git 命令，失败时返回测试错误而非隐藏 stderr。"""
        result = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def _preview(self, suffix: str = "# Windows acceptance") -> tuple[RuleTransactionExecutor, dict[str, object]]:
        """生成合法候选预览并返回未消费令牌。"""
        detail = self.service.get_rule("user")
        candidate = str(detail["content"]) + "\n" + suffix + "\n"
        preview = self.service.preview(
            "user", base_content_hash=str(detail["content_hash"]), candidate_content=candidate,
        )
        return RuleTransactionExecutor(self.service), preview

    @staticmethod
    def _apply(executor: RuleTransactionExecutor, preview: dict[str, object], task_id: str) -> dict[str, object]:
        """绑定服务端任务后应用事务。"""
        change_id = str(preview["change_id"])
        executor.claim(change_id, task_id)
        return executor.apply(change_id, str(preview["confirmation_token"]), str(preview["preview_digest"]), task_id)

    def test_real_apply_hash_dirty_scope_and_utf8_normalization(self) -> None:
        """中文/CRLF 候选无 BOM 写入，Git 只出现目标文件 dirty。"""
        detail = self.service.get_rule("user")
        candidate = str(detail["content"]).replace("\n", "\r\n") + "\r\n# 中文 Windows 验收\r\n"
        preview = self.service.preview(
            "user", base_content_hash=str(detail["content_hash"]), candidate_content=candidate,
        )
        executor = RuleTransactionExecutor(self.service)
        result = self._apply(executor, preview, "task-windows-success")
        self.assertEqual(result["status"], "succeeded")
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("中文 Windows 验收", raw.decode("utf-8"))
        status = self._git("status", "--porcelain=v1")
        self.assertIn(status[:2], {" M", "M "})
        self.assertEqual(status[2:], "system/rules/USER_RULES.md")
        self.assertEqual(self.service.get_rule("user")["content_hash"], preview["candidate_content_hash"])
        transaction_dir = RuleChangeStore(self.workspace)._directory(str(preview["change_id"]))
        self.assertTrue((transaction_dir / "candidate.md").is_file())
        self.assertTrue((transaction_dir / "backup.md").is_file())
        self.assertNotIn(str(preview["confirmation_token"]), (transaction_dir / "transaction.json").read_text(encoding="utf-8"))
        executor.finalize_success(str(preview["change_id"]), "task-windows-success")
        self.assertFalse((transaction_dir / "candidate.md").exists())
        self.assertFalse((transaction_dir / "backup.md").exists())

    def test_shared_validator_and_fault_injection_roll_back(self) -> None:
        """共享验证器拒绝坏候选，replace/正式复核失败均恢复原始字节。"""
        detail = self.service.get_rule("user")
        with self.assertRaises(Exception):
            self.service.preview(
                "user", base_content_hash=str(detail["content_hash"]), candidate_content="\x00invalid\n",
            )
        with self.assertRaises(Exception):
            self.service.preview(
                "user", base_content_hash=str(detail["content_hash"]),
                candidate_content=str(detail["content"]) + "\n" + ("x" * 4097),
            )
        original = (self.repo / "system" / "rules" / "USER_RULES.md").read_bytes()
        executor, preview = self._preview("# replace fault")
        with patch.object(executor, "_replace_candidate", side_effect=RuleTransactionError("replace fault")):
            result = self._apply(executor, preview, "task-replace-fault")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((self.repo / "system" / "rules" / "USER_RULES.md").read_bytes(), original)

        executor, preview = self._preview("# validation fault")
        with patch.object(executor, "_full_validation", side_effect=(True, False, True)):
            result = self._apply(executor, preview, "task-validation-fault")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((self.repo / "system" / "rules" / "USER_RULES.md").read_bytes(), original)

    def test_two_independent_processes_have_one_claim(self) -> None:
        """两个独立 Python 进程竞争同一 change 时最多一个 task_id 成功。"""
        executor, preview = self._preview("# process claim")
        change_id = str(preview["change_id"])
        transaction_dir = RuleChangeStore(self.workspace)._directory(change_id)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [
            context.Process(target=_claim_worker, args=(str(self.workspace), str(self.repo), change_id, f"task-process-{index}", queue))
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(item[0] == "ok" for item in outcomes), 1)
        self.assertEqual(RuleChangeStore(self.workspace).load(change_id).task_id, next(item[1] for item in outcomes if item[0] == "ok"))
        self.assertTrue(transaction_dir.joinpath("transaction.json").is_file())

    def test_recover_applying_and_validating_without_overwriting_third_party(self) -> None:
        """模拟 applying/validating 崩溃并恢复；第三方正文不会被覆盖。"""
        target = self.repo / "system" / "rules" / "USER_RULES.md"
        original = target.read_bytes()
        for index, status in enumerate(("applying", "validating")):
            executor, preview = self._preview(f"# crash {status}")
            change_id = str(preview["change_id"])
            store = RuleChangeStore(self.workspace)
            transaction = store.load(change_id).transition("applying")
            if status == "validating":
                transaction = transaction.transition("validating")
            store.save(transaction)
            store.backup_path(change_id).write_bytes(original)
            target.write_bytes(store.candidate_path(change_id).read_bytes())
            self.assertEqual(executor.recover()[0]["status"], "rolled_back")
            self.assertEqual(target.read_bytes(), original)

        executor, preview = self._preview("# third party")
        change_id = str(preview["change_id"])
        transaction = RuleChangeStore(self.workspace).load(change_id)
        store = RuleChangeStore(self.workspace)
        store.save(transaction.transition("applying"))
        store.backup_path(change_id).write_bytes(original)
        third_party = "# 第三方修改\n".encode("utf-8")
        target.write_bytes(third_party)
        recovered = executor.recover()
        self.assertEqual(recovered[0]["status"], "recovery_required")
        self.assertEqual(target.read_bytes(), third_party)

    def test_restart_reconciles_succeeded_transaction_with_queued_task(self) -> None:
        """模拟事务已成功但任务尚未终态的崩溃窗口，重启时补齐安全事实。"""
        executor, preview = self._preview("# succeeded before task finish")
        change_id = str(preview["change_id"])
        task_id = "c" * 32
        result = self._apply(executor, preview, task_id)
        self.assertEqual(result["status"], "succeeded")
        task_store = TaskStore(self.workspace, recover=False)
        task_store.create_task(
            action_id=RULE_USER_UPDATE_SPEC.action_id,
            parameters={"change_id": change_id},
            risk_level=RULE_USER_UPDATE_SPEC.risk_level,
            effects=list(RULE_USER_UPDATE_SPEC.effects), timeout_seconds=120,
            concurrency_group=RULE_USER_UPDATE_SPEC.action_id,
            preview_digest=str(preview["preview_digest"]), invocation_id=change_id, task_id=task_id,
        )
        audit: list[dict[str, object]] = []
        coordinator = RuleChangeTaskCoordinator(executor, task_store=task_store, audit_sink=audit.append)
        try:
            coordinator.recover()
            self.assertEqual(task_store.get_task(task_id)["status"], "succeeded")
            self.assertTrue(any(item.get("status") == "succeeded" for item in audit))
        finally:
            coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
