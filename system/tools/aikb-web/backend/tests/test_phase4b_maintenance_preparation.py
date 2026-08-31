"""environment 预览转 prepared 事务的最小安全测试。"""
import hashlib
import unittest
from types import SimpleNamespace
from aikb_web.core.actions import ActionError, ConfirmationTokenService
from aikb_web.core.maintenance_materials import MaintenanceEnvironmentMaterial, MaintenanceLeafMaterial
from aikb_web.core.maintenance_preparation import MaintenancePreparationError, MaintenancePreparationService

class _Tx:
    def __init__(self): self.items=[]
    def create(self, item): self.items.append(item)
class _Mat:
    def prepare(self, *args): return True
class _Provider:
    def __init__(self, stale=False): self.stale=stale
    def capture_environment(self, plan):
        leaves={}
        for name in ("user_environment.aikb_home", "user_environment.aikb_knowledge_home"):
            leaves[name]=MaintenanceLeafMaterial(name, "missing", None, __import__('hashlib').sha256(b'expected').hexdigest(), None, None, b'expected')
        env={"AIKB_HOME": MaintenanceEnvironmentMaterial("AIKB_HOME", "missing"), "AIKB_KNOWLEDGE_HOME": MaintenanceEnvironmentMaterial("AIKB_KNOWLEDGE_HOME", "missing")}
        status=SimpleNamespace(target_id="environment", base_fingerprint=plan.before_fingerprint)
        if self.stale: status=SimpleNamespace(target_id="environment", base_fingerprint="c"*64)
        return status, leaves, env

class PreparationTests(unittest.TestCase):
    def _plan(self):
        leaves = ("user_environment.aikb_home", "user_environment.aikb_knowledge_home")
        missing_hash = hashlib.sha256(b"<missing>").hexdigest()
        expected_hash = hashlib.sha256(b"expected").hexdigest()
        before = hashlib.sha256("\n".join(f"{leaf}:{missing_hash}" for leaf in leaves).encode()).hexdigest()
        after = hashlib.sha256("\n".join(f"{leaf}:{expected_hash}" for leaf in leaves).encode()).hexdigest()
        return SimpleNamespace(target_id="environment", before_fingerprint=before, after_fingerprint=after, preview_digest="f"*64, steps=tuple(SimpleNamespace(step_id=x) for x in ("preflight","backup","write_environment","verify")))
    def _status(self):
        return SimpleNamespace(target_id="environment", status="missing", base_fingerprint=self._plan().before_fingerprint)
    def test_success_and_token_single_use(self):
        tx = _Tx()
        tokens = ConfirmationTokenService()
        service = MaintenancePreparationService(tx, lambda _: _Mat(), tokens)
        result = service.prepare(self._plan(), self._status(), _Provider())
        self.assertEqual(result.change.status, "prepared")
        self.assertEqual(len(tx.items), 1)
        binding = {
            "action_id": result.change.action_id,
            "parameters": {"change_id": result.change.change_id},
            "risk_level": result.change.risk_level,
            "preview_digest": result.change.preview_digest,
        }
        tokens.consume(result.confirmation_token, **binding)
        with self.assertRaises(ActionError):
            tokens.consume(result.confirmation_token, **binding)
    def test_stale_fresh_status_rejected_before_create(self):
        tx=_Tx(); service=MaintenancePreparationService(tx, lambda _: _Mat(), ConfirmationTokenService())
        with self.assertRaises(MaintenancePreparationError): service.prepare(self._plan(), self._status(), _Provider(stale=True))
        self.assertEqual(tx.items,[])
if __name__ == "__main__": unittest.main()
