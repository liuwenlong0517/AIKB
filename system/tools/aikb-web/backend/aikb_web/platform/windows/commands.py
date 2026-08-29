"""Windows 受控动作的静态命令和最小环境构造。

本模块只把已注册动作转换为参数数组；不接受客户端命令、脚本、工作目录或
环境覆盖。真正的进程创建由 ``executor`` 负责，并通过 Job Object 绑定进程树。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class CommandError(ValueError):
    """动作不存在、固定脚本缺失或受信任程序无法解析。"""


@dataclass(frozen=True)
class CommandSpec:
    """一个不可变的服务端命令描述；``argv`` 永远是独立参数，不经过 Shell。"""

    action_id: str
    program: Path
    argv: tuple[str, ...]
    cwd: Path

    def arguments(self) -> tuple[str, ...]:
        """返回包含绝对程序名的参数数组，供低层 CreateProcess 调用。"""
        return (str(self.program), *self.argv)


_ALLOWED_ACTIONS = {
    "validate.structure",
    "repository.status.control",
    "repository.status.knowledge",
}
_MINIMAL_WINDOWS_ENV = (
    "SystemRoot", "SystemDrive", "ProgramData", "ALLUSERSPROFILE",
    "TEMP", "TMP", "USERPROFILE",
)
_CONTRACT_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "NO_COLOR": "1",
}


def _absolute_directory(value: object, label: str) -> Path:
    """把服务端路径解析为绝对目录；拒绝空值和不存在的目录。"""
    if not isinstance(value, (str, os.PathLike)):
        raise CommandError(f"{label} 未配置")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise CommandError(f"{label} 不可用")
    return path


def _registered_program(name: str) -> Path | None:
    """从机器级安装注册表解析 Git/PowerShell，避免信任可被当前目录劫持的 PATH。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        if name == "git":
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GitForWindows") as key:
                install_root = Path(str(winreg.QueryValueEx(key, "InstallPath")[0]))
            return install_root / "cmd" / "git.exe"
        if name == "pwsh":
            base_path = r"SOFTWARE\Microsoft\PowerShellCore\InstalledVersions"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base:
                for index in range(winreg.QueryInfoKey(base)[0]):
                    with winreg.OpenKey(base, winreg.EnumKey(base, index)) as version:
                        for value_name in ("InstallLocation", "InstallDir"):
                            try:
                                install_root = Path(str(winreg.QueryValueEx(version, value_name)[0]))
                            except OSError:
                                continue
                            candidate = install_root / "pwsh.exe"
                            if candidate.is_file():
                                return candidate
    except (OSError, ImportError, ValueError, TypeError):
        return None
    return None


def _trusted_program(name: str) -> Path:
    """只接受机器级安装记录指向的受信任程序，不从当前 PATH 猜测。"""
    resolved = _registered_program(name)
    if resolved is None:
        raise CommandError(f"受信任程序不可用：{name}")
    path = resolved.resolve()
    if not path.is_absolute() or not path.is_file():
        raise CommandError(f"受信任程序路径无效：{name}")
    return path


def build_child_environment(settings: object, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """从空白环境构造白名单子环境，不继承 token、代理和 Agent 配置。"""
    source = environ if environ is not None else os.environ
    repo_root = _absolute_directory(getattr(settings, "repo_root", None), "控制仓")
    knowledge_root = _absolute_directory(getattr(settings, "knowledge_root", None), "知识仓")
    result = {
        "AIKB_HOME": str(repo_root),
        "AIKB_KNOWLEDGE_HOME": str(knowledge_root),
        **_CONTRACT_ENV,
    }
    for name in _MINIMAL_WINDOWS_ENV:
        value = source.get(name)
        if value:
            result[name] = str(value)
    # PowerShell 的 native command processor 即使接收绝对 Python 叶子文件，仍需要
    # 一个可控 PATH 来完成原生进程解析。这里只加入服务端 Python 与 System32，
    # 绝不继承用户 PATH，也不会让仓库目录参与程序查找。
    system_root = result.get("SystemRoot")
    safe_path = [str(Path(sys.executable).resolve().parent)]
    if system_root:
        safe_path.extend((str(Path(system_root) / "System32"), str(Path(system_root))))
    result["Path"] = os.pathsep.join(dict.fromkeys(safe_path))
    result["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return result


def _repository_spec(action_id: str, settings: object, repo_root: Path) -> tuple[CommandSpec, ...]:
    """构造仓库状态的固定 branch/revision/status 三个只读 Git 调用。"""
    git = _trusted_program("git")
    prefix = ("-C", str(repo_root))
    return (
        CommandSpec(action_id, git, prefix + ("branch", "--show-current"), repo_root),
        CommandSpec(action_id, git, prefix + ("rev-parse", "--short=12", "HEAD"), repo_root),
        # ``--no-optional-locks`` 防止只读状态探测刷新 index.lock。
        CommandSpec(action_id, git, prefix + ("--no-optional-locks", "status", "--porcelain=v1", "--branch"), repo_root),
    )


def build_action_commands(action_id: str, settings: object) -> tuple[CommandSpec, ...]:
    """按静态动作 ID 构造受控参数数组；未知动作和用户路径一律拒绝。"""
    if action_id not in _ALLOWED_ACTIONS:
        raise CommandError("动作不在 Windows 受控白名单中")
    repo_root = _absolute_directory(getattr(settings, "repo_root", None), "控制仓")
    if action_id == "validate.structure":
        pwsh = _trusted_program("pwsh")
        python_path = Path(sys.executable).resolve()
        if not python_path.is_absolute() or not python_path.is_file():
            raise CommandError("服务端 Python 路径不可用")
        script = repo_root / "system" / "tests" / "validate-structure.ps1"
        if not script.is_file():
            raise CommandError("结构校验脚本不可用")
        knowledge_root = _absolute_directory(getattr(settings, "knowledge_root", None), "知识仓")
        return (CommandSpec(
            action_id,
            pwsh,
            (
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-KnowledgePath", str(knowledge_root), "-PythonPath", str(python_path),
            ),
            repo_root,
        ),)
    if action_id == "repository.status.control":
        return _repository_spec(action_id, settings, repo_root)
    knowledge_root = _absolute_directory(getattr(settings, "knowledge_root", None), "知识仓")
    return _repository_spec(action_id, settings, knowledge_root)


def build_command(action_id: str, settings: object) -> tuple[CommandSpec, ...]:
    """兼容性别名：动作执行器统一消费静态命令计划。"""
    return build_action_commands(action_id, settings)


__all__ = ["CommandError", "CommandSpec", "build_action_commands", "build_child_environment", "build_command"]
