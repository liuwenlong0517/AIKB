"""Windows 维护事务私有材料的最小权限收紧器。

本模块只处理服务端已经解析出的单个 ``Path`` 和目录标志，不接受客户端路径、
ACL 文本或命令参数。Windows 上通过绝对路径调用系统 ``icacls.exe``，参数始终
是固定白名单；所有底层输出均被丢弃，失败只返回固定错误，避免泄露路径、SID
或 ACL 内容。非 Windows 明确拒绝执行，不能伪装为成功。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class MaintenancePermissionsUnsupported(RuntimeError):
    """当前平台没有实现维护材料 ACL 收紧。"""


class MaintenancePermissionsError(RuntimeError):
    """ACL 收紧或回读校验失败；异常消息不包含路径和系统输出。"""


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_FULL_CONTROL = 0x1F01FF
_OBJECT_INHERIT = 0x01
_CONTAINER_INHERIT = 0x02
_INHERITED_ACE = 0x10


class AclAce(NamedTuple):
    """回读 ACL 的最小结构化 ACE，不包含原始 SDDL 或路径。"""

    sid: str
    access_mask: int
    flags: int
    allow: bool


@dataclass(frozen=True)
class AclSnapshot:
    """Win32 回读的 DACL 保护状态和 ACE 摘要。"""

    protected: bool
    aces: tuple[AclAce, ...]


class _AclSizeInformation(ctypes.Structure):
    """对应 Win32 ACL_SIZE_INFORMATION 的三个 DWORD 字段。"""

    _fields_ = [
        ("ace_count", wintypes.DWORD),
        ("acl_bytes_in_use", wintypes.DWORD),
        ("acl_bytes_free", wintypes.DWORD),
    ]


class _SidAndAttributes(ctypes.Structure):
    """Win32 TOKEN_USER 内嵌的 SID_AND_ATTRIBUTES 结构。"""

    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    """Win32 TOKEN_USER 结构，首字段不是 SID 本身而是 SID 指针。"""

    _fields_ = [("User", _SidAndAttributes)]


def _current_user_sid() -> str:
    """从当前进程 token 获取真实用户 SID，不使用可伪造的用户名环境变量。"""
    if os.name != "nt":
        raise MaintenancePermissionsUnsupported("当前平台不支持维护材料权限收紧")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise MaintenancePermissionsError("当前用户主体不可用")
    try:
        needed = ctypes.c_ulong(0)
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise MaintenancePermissionsError("当前用户主体不可用")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise MaintenancePermissionsError("当前用户主体不可用")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_ptr = token_user.User.Sid
        if not sid_ptr:
            raise MaintenancePermissionsError("当前用户主体不可用")
        text_ptr = ctypes.c_wchar_p()
        if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text_ptr)):
            raise MaintenancePermissionsError("当前用户主体不可用")
        try:
            value = text_ptr.value
        finally:
            kernel.LocalFree(text_ptr)
        if not value or not value.startswith("S-"):
            raise MaintenancePermissionsError("当前用户主体不可用")
        return value
    finally:
        kernel.CloseHandle(token)


def _read_acl_snapshot(path: Path) -> AclSnapshot:
    """用 Win32 security API 读取保护位、ACE 主体、权限和继承标志。"""
    if os.name != "nt":
        raise MaintenancePermissionsUnsupported("当前平台不支持维护材料权限收紧")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security_descriptor = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    error = advapi.GetNamedSecurityInfoW(
        ctypes.c_wchar_p(str(path)), 1, 0x00000004, None, None,
        ctypes.byref(dacl), None, ctypes.byref(security_descriptor),
    )
    if error:
        raise MaintenancePermissionsError("维护材料权限读取失败")
    try:
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        if not advapi.GetSecurityDescriptorControl(
            security_descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise MaintenancePermissionsError("维护材料权限读取失败")
        if not dacl:
            # NULL DACL 意味着不受 ACL 限制，不能视作“没有额外 ACE”而放行。
            raise MaintenancePermissionsError("维护材料权限读取失败")
        size = _AclSizeInformation()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise MaintenancePermissionsError("维护材料权限读取失败")
        aces: list[AclAce] = []
        for index in range(size.ace_count):
            ace = wintypes.LPVOID()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise MaintenancePermissionsError("维护材料权限读取失败")
            address = ace.value
            if not address:
                raise MaintenancePermissionsError("维护材料权限读取失败")
            header = ctypes.string_at(address, 4)
            ace_type, flags = header[0], header[1]
            mask = ctypes.c_uint32.from_address(address + 4).value
            sid = wintypes.LPVOID(address + 8)
            text_ptr = ctypes.c_wchar_p()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text_ptr)):
                raise MaintenancePermissionsError("维护材料权限读取失败")
            try:
                sid_text = text_ptr.value or ""
            finally:
                kernel.LocalFree(text_ptr)
            aces.append(AclAce(sid_text, mask, flags, ace_type == 0))
        return AclSnapshot(bool(control.value & 0x1000), tuple(aces))
    finally:
        kernel.LocalFree(security_descriptor)


def _reject_reparse_components(path: Path) -> None:
    """逐级拒绝符号链接、junction 和 Windows reparse point。"""
    if not path.is_absolute():
        raise MaintenancePermissionsError("维护材料路径不可用")
    current = path
    components: list[Path] = []
    while True:
        components.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(components):
        try:
            if not component.exists() and component != path:
                continue
            attributes = getattr(component.stat(), "st_file_attributes", 0)
        except OSError as error:
            raise MaintenancePermissionsError("维护材料路径不可用") from error
        if component.is_symlink() or attributes & _REPARSE:
            raise MaintenancePermissionsError("维护材料路径不可用")


def _trusted_icacls() -> Path:
    """通过 kernel32 获取系统目录定位 icacls，不信任可伪造环境变量。"""
    if os.name != "nt":
        raise MaintenancePermissionsUnsupported("当前平台不支持维护材料权限收紧")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(260)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise MaintenancePermissionsError("系统权限工具不可用")
    candidate = Path(buffer.value) / "icacls.exe"
    try:
        if not candidate.is_absolute() or not candidate.is_file() or candidate.is_symlink():
            raise MaintenancePermissionsError("系统权限工具不可用")
    except OSError as error:
        raise MaintenancePermissionsError("系统权限工具不可用") from error
    return candidate


class MaintenancePermissionsHardener:
    """把私有事务材料 DACL 收紧到当前用户、SYSTEM 和 Administrators。"""

    def harden(self, path: Path, is_directory: bool) -> None:
        """固定 ACL、禁用继承并回读验证；任一边界失败均 fail-closed。"""
        if os.name != "nt":
            raise MaintenancePermissionsUnsupported("当前平台不支持维护材料权限收紧")
        if not isinstance(path, Path) or not isinstance(is_directory, bool):
            raise MaintenancePermissionsError("维护材料参数无效")
        _reject_reparse_components(path)
        try:
            if not path.exists() or path.is_dir() != is_directory:
                raise MaintenancePermissionsError("维护材料不可用")
        except OSError as error:
            raise MaintenancePermissionsError("维护材料不可用") from error
        try:
            user_sid = _current_user_sid()
            icacls = _trusted_icacls()
            inheritance = "/grant:r"
            permission = "(OI)(CI)F" if is_directory else "F"
            command = [
                str(icacls), str(path), "/inheritance:r", inheritance,
                f"*{user_sid}:{permission}", f"*{_SYSTEM_SID}:{permission}",
                f"*{_ADMINISTRATORS_SID}:{permission}",
            ]
            result = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                text=False,
                timeout=30,
            )
            if result.returncode != 0:
                raise MaintenancePermissionsError("维护材料权限设置失败")
            snapshot = _read_acl_snapshot(path)
            expected = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
            if not snapshot.protected or len(snapshot.aces) != len(expected):
                raise MaintenancePermissionsError("维护材料权限校验失败")
            inherit_flags = _OBJECT_INHERIT | _CONTAINER_INHERIT if is_directory else 0
            if any(
                not ace.allow or ace.sid not in expected
                or ace.access_mask != _FULL_CONTROL
                or ace.flags & _INHERITED_ACE
                or (ace.flags & (_OBJECT_INHERIT | _CONTAINER_INHERIT)) != inherit_flags
                for ace in snapshot.aces
            ) or {ace.sid for ace in snapshot.aces} != expected:
                raise MaintenancePermissionsError("维护材料权限校验失败")
        except MaintenancePermissionsError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
            raise MaintenancePermissionsError("维护材料权限操作失败") from error


__all__ = [
    "AclAce",
    "AclSnapshot",
    "MaintenancePermissionsError",
    "MaintenancePermissionsHardener",
    "MaintenancePermissionsUnsupported",
]
