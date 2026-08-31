"""阶段 4B 私有事务材料 ACL 收紧器的隔离安全测试。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import ctypes
from pathlib import Path
from unittest.mock import patch

from aikb_web.platform.windows import maintenance_permissions as permissions


class MaintenancePermissionsTests(unittest.TestCase):
    """不触碰真实用户配置，验证平台边界、固定参数和 fail-closed。"""

    def test_non_windows_is_explicitly_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("{}", encoding="utf-8")
            with patch.object(permissions.os, "name", "posix"):
                with self.assertRaises(permissions.MaintenancePermissionsUnsupported):
                    permissions.MaintenancePermissionsHardener().harden(target, False)

    def test_invalid_path_and_directory_shape_fail_closed_without_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("{}", encoding="utf-8")
            hardener = permissions.MaintenancePermissionsHardener()
            with patch.object(permissions.os, "name", "nt"), patch.object(
                permissions.subprocess, "run"
            ) as run:
                with self.assertRaises(permissions.MaintenancePermissionsError):
                    hardener.harden(target, True)
                with self.assertRaises(permissions.MaintenancePermissionsError):
                    hardener.harden(Path(directory) / "missing.json", False)
                run.assert_not_called()

    def test_reparse_point_is_rejected_before_icacls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("{}", encoding="utf-8")
            hardener = permissions.MaintenancePermissionsHardener()
            with patch.object(permissions.os, "name", "nt"), patch.object(
                permissions.Path, "is_symlink", return_value=True
            ), patch.object(permissions.subprocess, "run") as run:
                with self.assertRaises(permissions.MaintenancePermissionsError):
                    hardener.harden(target, False)
                run.assert_not_called()

    def test_fixed_icacls_arguments_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("private", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **kwargs: object) -> object:
                calls.append(command)
                self.assertFalse(kwargs["shell"])
                self.assertTrue(kwargs["capture_output"])
                return subprocess.CompletedProcess(command, 0, b"ACL summary", b"")

            with patch.object(permissions.os, "name", "nt"), patch.object(
                permissions, "_trusted_icacls", return_value=Path("C:/Windows/System32/icacls.exe")
            ), patch.object(permissions, "_current_user_sid", return_value="S-1-5-21-1"), patch.object(
                permissions, "_read_acl_snapshot", return_value=permissions.AclSnapshot(True, (
                    permissions.AclAce("S-1-5-21-1", permissions._FULL_CONTROL, 0, True),
                    permissions.AclAce(permissions._SYSTEM_SID, permissions._FULL_CONTROL, 0, True),
                    permissions.AclAce(permissions._ADMINISTRATORS_SID, permissions._FULL_CONTROL, 0, True),
                ))
            ), patch.object(permissions.subprocess, "run", side_effect=fake_run):
                permissions.MaintenancePermissionsHardener().harden(target, False)
            self.assertEqual(len(calls), 1)
            self.assertIn("/inheritance:r", calls[0])
            self.assertIn("*S-1-5-18:F", calls[0])
            self.assertIn("*S-1-5-32-544:F", calls[0])
            self.assertEqual(calls[0][1], str(target))

    def test_environment_username_does_not_change_token_sid_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("private", encoding="utf-8")
            with patch.object(permissions.os, "name", "nt"), patch.dict(
                permissions.os.environ, {"USERNAME": "attacker"}
            ), patch.object(permissions, "_current_user_sid", return_value="S-1-5-21-real"), patch.object(
                permissions, "_trusted_icacls", return_value=Path("C:/Windows/System32/icacls.exe")
            ), patch.object(permissions, "_read_acl_snapshot", return_value=permissions.AclSnapshot(True, (
                permissions.AclAce("S-1-5-21-real", permissions._FULL_CONTROL, 0, True),
                permissions.AclAce(permissions._SYSTEM_SID, permissions._FULL_CONTROL, 0, True),
                permissions.AclAce(permissions._ADMINISTRATORS_SID, permissions._FULL_CONTROL, 0, True),
            ))), patch.object(
                permissions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")
            ) as run:
                permissions.MaintenancePermissionsHardener().harden(target, False)
            self.assertEqual(run.call_args.args[0][4], "*S-1-5-21-real:F")

    def test_acl_snapshot_extra_or_inherited_or_missing_ace_fails_closed(self) -> None:
        cases = (
            (permissions.AclAce("S-1-1-0", permissions._FULL_CONTROL, 0, True),),
            (permissions.AclAce("S-1-5-21-real", permissions._FULL_CONTROL, permissions._INHERITED_ACE, True),),
            (permissions.AclAce("S-1-5-18", permissions._FULL_CONTROL, 0, True),),
            (permissions.AclAce("S-1-5-21-real", 0, 0, True),),
        )
        for aces in cases:
            with self.subTest(aces=aces), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "material.json"
                target.write_text("private", encoding="utf-8")
                snapshot = permissions.AclSnapshot(True, aces)
                with patch.object(permissions.os, "name", "nt"), patch.object(
                    permissions, "_current_user_sid", return_value="S-1-5-21-real"
                ), patch.object(permissions, "_trusted_icacls", return_value=Path("C:/Windows/System32/icacls.exe")), patch.object(
                    permissions, "_read_acl_snapshot", return_value=snapshot
                ), patch.object(permissions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")):
                    with self.assertRaises(permissions.MaintenancePermissionsError):
                        permissions.MaintenancePermissionsHardener().harden(target, False)

    def test_acl_size_information_has_three_dwords(self) -> None:
        self.assertEqual(ctypes.sizeof(permissions._AclSizeInformation), 12)
        self.assertEqual(len(permissions._AclSizeInformation._fields_), 3)

    def test_real_temporary_roundtrip_when_windows_is_available(self) -> None:
        """尝试真实临时材料往返；权限策略不允许时仅记录为环境限制。"""
        if permissions.os.name != "nt":
            self.skipTest("非 Windows 环境")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "aikb-private.tmp"
            target.write_bytes(b"private")
            try:
                permissions.MaintenancePermissionsHardener().harden(target, False)
            except permissions.MaintenancePermissionsUnsupported:
                self.skipTest("Windows 权限能力不可用")

    def test_icacls_failure_is_fixed_and_does_not_leak_system_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "material.json"
            target.write_text("private", encoding="utf-8")
            failure = subprocess.CompletedProcess([], 1, b"C:\\secret\\material", b"SID-PRIVATE")
            with patch.object(permissions.os, "name", "nt"), patch.object(
                permissions, "_trusted_icacls", return_value=Path("C:/Windows/System32/icacls.exe")
            ), patch.object(permissions, "_current_user_sid", return_value="S-1-5-21-real"), patch.object(
                permissions.subprocess, "run", return_value=failure
            ):
                with self.assertRaises(permissions.MaintenancePermissionsError) as caught:
                    permissions.MaintenancePermissionsHardener().harden(target, False)
            self.assertNotIn("secret", str(caught.exception).lower())
            self.assertNotIn("sid-private", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
