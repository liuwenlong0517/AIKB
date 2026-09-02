"""阶段 4B 维护事务的私有材料存储。

本模块是事务执行器的本机材料边界：逐逻辑叶子保存事务前字节、期望字节、
存在语义和必要文件属性，同时保存两个固定用户环境值的缺失/空/具体值语义。
manifest 仅保存安全元数据和摘要，正文与环境值留在注入的事务目录中，永远不
进入 ``public_dict``、异常文本、任务或审计投影。本模块不解析或写入真实目标。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .maintenance_targets import (
    MAINTENANCE_LEAVES_BY_TARGET,
    MAINTENANCE_TARGET_REGISTRY,
    validate_logical_id,
)


class MaintenanceMaterialError(ValueError):
    """私有维护材料缺失、越界、损坏或完整性验证失败。"""


_SCHEMA_VERSION = 1
_ENV_NAMES = ("AIKB_HOME", "AIKB_KNOWLEDGE_HOME")
_ENV_STATES = ("missing", "empty", "value")
_LEAF_EXISTENCE = ("present", "missing")
_MAX_LEAF_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_ENV_BYTES = 64 * 1024
_LEAF_FILES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        leaf_id: (f"leaf-{index:02d}.before", f"leaf-{index:02d}.expected")
        for index, leaf_id in enumerate(
            leaf for leaves in MAINTENANCE_LEAVES_BY_TARGET.values() for leaf in leaves
        )
    }
)
_ENV_FILES: Mapping[str, str] = MappingProxyType(
    {name: f"environment-{index:02d}.old" for index, name in enumerate(_ENV_NAMES)}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(data: bytes) -> str:
    """计算材料摘要；原始字节不会进入异常或公开模型。"""

    return hashlib.sha256(data).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """生成稳定 manifest 字节，便于完整性校验且不包含正文。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_hash(value: str | None, field_name: str, *, required: bool = False) -> str | None:
    """校验摘要格式；错误文本只包含字段名，不回显用户输入。"""

    if value is None and not required:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MaintenanceMaterialError(f"{field_name} 摘要无效")
    return value


def _safe_bytes(value: bytes, field_name: str, limit: int) -> bytes:
    """校验字节类型和预算，避免材料目录被正文无限膨胀。"""

    if not isinstance(value, bytes) or len(value) > limit:
        raise MaintenanceMaterialError(f"{field_name} 材料超出预算")
    return value


def _validate_leaf(leaf_id: str) -> str:
    """只允许固定静态叶子 ID，不接受路径或任意新叶子。"""

    try:
        validate_logical_id(leaf_id, "leaf_id")
    except Exception as error:
        raise MaintenanceMaterialError("leaf_id 无效") from error
    if leaf_id not in _LEAF_FILES:
        raise MaintenanceMaterialError("leaf_id 不是固定维护叶子")
    return leaf_id


def _validate_change_id(change_id: str) -> str:
    """校验事务逻辑 ID，并把底层目标校验异常收敛为材料边界错误。"""

    try:
        validate_logical_id(change_id, "change_id")
    except Exception as error:
        raise MaintenanceMaterialError("change_id 无效") from error
    return change_id


@dataclass(frozen=True)
class MaintenanceLeafMaterial:
    """单叶子私有材料元数据与正文。

    ``before_bytes``/``expected_bytes`` 仅供事务执行器读取，``public_dict`` 永远
    省略它们；缺失叶子的 before 字节和摘要必须为 ``None``，不能伪装为空文件。
    """

    leaf_id: str
    existence: str
    before_hash: str | None
    expected_hash: str
    file_mode: int | None
    before_bytes: bytes | None
    expected_bytes: bytes

    def __post_init__(self) -> None:
        """校验存在语义、字节摘要和文件属性，不接受路径或正文伪装字段。"""

        _validate_leaf(self.leaf_id)
        if self.existence not in _LEAF_EXISTENCE:
            raise MaintenanceMaterialError("叶子存在语义无效")
        if self.existence == "missing" and (self.before_bytes is not None or self.before_hash is not None):
            raise MaintenanceMaterialError("缺失叶子不得保存事务前正文")
        if self.existence == "present":
            if self.before_bytes is None:
                raise MaintenanceMaterialError("存在叶子缺少事务前正文")
            _safe_bytes(self.before_bytes, "before", _MAX_LEAF_BYTES)
            if self.before_hash != _sha256(self.before_bytes):
                raise MaintenanceMaterialError("事务前摘要不匹配")
        _safe_bytes(self.expected_bytes, "expected", _MAX_LEAF_BYTES)
        if self.expected_hash != _sha256(self.expected_bytes):
            raise MaintenanceMaterialError("期望摘要不匹配")
        _safe_hash(self.before_hash, "before_hash", required=self.existence == "present")
        _safe_hash(self.expected_hash, "expected_hash", required=True)
        if self.file_mode is not None and (
            not isinstance(self.file_mode, int) or isinstance(self.file_mode, bool) or not 0 <= self.file_mode <= 0o7777
        ):
            raise MaintenanceMaterialError("文件属性无效")

    def public_dict(self) -> dict[str, Any]:
        """返回不含正文、路径和备份文件名的安全元数据。"""

        return {
            "leaf_id": self.leaf_id,
            "existence": self.existence,
            "before_hash": self.before_hash,
            "expected_hash": self.expected_hash,
            "file_mode": self.file_mode,
        }

    # 与其他维护模型保持同名安全投影入口；不复制或暴露私有字节。
    to_public_dict = public_dict


@dataclass(frozen=True)
class MaintenanceEnvironmentMaterial:
    """固定用户环境变量的私有旧值材料，保留缺失/空/具体值语义。"""

    name: str
    state: str
    value: str | None = None

    def __post_init__(self) -> None:
        """校验固定环境名和状态；具体值不进入公开投影或 manifest。"""

        if self.name not in _ENV_NAMES:
            raise MaintenanceMaterialError("环境名未声明")
        if self.state not in _ENV_STATES:
            raise MaintenanceMaterialError("环境存在语义无效")
        if self.state == "missing" and self.value is not None:
            raise MaintenanceMaterialError("缺失环境值不得携带正文")
        if self.state == "empty" and self.value != "":
            raise MaintenanceMaterialError("空环境值语义不一致")
        if self.state == "value":
            if not isinstance(self.value, str) or not self.value:
                raise MaintenanceMaterialError("具体环境值无效")
        if self.value is not None:
            encoded = self.value.encode("utf-8")
            if len(encoded) > _MAX_ENV_BYTES or "\x00" in self.value:
                raise MaintenanceMaterialError("环境值超出预算")

    def public_dict(self) -> dict[str, str]:
        """只公开固定环境名和存在语义，不返回环境值或其文件名。"""

        return {"name": self.name, "state": self.state}

    to_public_dict = public_dict


@dataclass(frozen=True)
class MaintenanceMaterialManifest:
    """材料目录的安全 manifest 视图，不包含物理路径或正文。"""

    change_id: str
    target_id: str
    leaves: tuple[MaintenanceLeafMaterial, ...]
    environments: tuple[MaintenanceEnvironmentMaterial, ...]
    manifest_digest: str

    def public_dict(self) -> dict[str, Any]:
        """生成 API/任务可用的安全投影，省略所有私有材料。"""

        return {
            "change_id": self.change_id,
            "target_id": self.target_id,
            "leaves": [item.public_dict() for item in self.leaves],
            "environments": [item.public_dict() for item in self.environments],
            "manifest_digest": self.manifest_digest,
        }

    to_public_dict = public_dict


class MaintenanceMaterialStore:
    """将维护私有材料限制在注入事务目录并提供原子写入/完整性读取。

    构造函数不创建目录、不读取材料；事务目录及 ``transaction.json`` 必须先由
    事务存储创建，``prepare`` 只会在其中新建 ``private`` 子目录并写入材料。
    ``permission_hardener`` 可由 Windows 宿主注入当前用户 ACL 收紧实现；默认
    仅收紧文件 mode，不能替代 Windows ACL，测试可替换以验证调用。
    """

    def __init__(
        self,
        root: Path,
        *,
        permission_hardener: Callable[[Path, bool], None] | None = None,
    ) -> None:
        """绑定已存在的隔离事务根；不解析、不扫描、不创建根目录。"""

        if (
            not isinstance(root, Path)
            or root.name != "maintenance-transactions"
            or root.is_symlink()
            or not root.is_dir()
            or self._has_reparse_component(root)
        ):
            raise MaintenanceMaterialError("事务材料根不可用")
        self._root = root
        self._permission_hardener = permission_hardener or self._default_harden_permissions

    def prepare(
        self,
        change_id: str,
        target_id: str,
        leaves: Mapping[str, MaintenanceLeafMaterial],
        environments: Mapping[str, MaintenanceEnvironmentMaterial],
    ) -> MaintenanceMaterialManifest:
        """一次性保存完整叶子/环境材料，并以 manifest 摘要证明写入完整。

        失败时不会向调用方返回任何正文；目录可能保留不完整材料，后续恢复扫描
        必须把它视为损坏而非继续使用，调用方可安全清理或人工保留。
        """

        _validate_change_id(change_id)
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        if target is None:
            raise MaintenanceMaterialError("target_id 不是固定维护目标")
        if tuple(leaves) != target.logical_leaves:
            raise MaintenanceMaterialError("材料叶子必须精确匹配目标")
        if target_id == "environment" and tuple(environments) != _ENV_NAMES:
            raise MaintenanceMaterialError("环境材料必须包含两个固定变量")
        if target_id != "environment" and environments:
            raise MaintenanceMaterialError("非环境目标不得携带环境材料")
        normalized_leaves = tuple(leaves[leaf_id] for leaf_id in target.logical_leaves)
        if any(item.leaf_id != leaf_id for item, leaf_id in zip(normalized_leaves, target.logical_leaves)):
            raise MaintenanceMaterialError("材料叶子标识不一致")
        normalized_env = tuple(environments[name] for name in _ENV_NAMES) if target_id == "environment" else ()
        if any(item.name != name for item, name in zip(normalized_env, _ENV_NAMES)):
            raise MaintenanceMaterialError("环境材料标识不一致")
        total_bytes = sum((len(item.before_bytes or b"") + len(item.expected_bytes)) for item in normalized_leaves)
        total_bytes += sum(len((item.value or "").encode("utf-8")) for item in normalized_env)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise MaintenanceMaterialError("事务材料超出总预算")
        directory = self._directory(change_id)
        private = self._private_directory(change_id)
        if private.exists() or private.is_symlink():
            raise MaintenanceMaterialError("事务私有材料已存在")
        try:
            private.mkdir(mode=0o700)
            self._harden(private, True)
            for item in normalized_leaves:
                before_name, expected_name = _LEAF_FILES[item.leaf_id]
                if item.before_bytes is not None:
                    self._atomic_write(private / before_name, item.before_bytes)
                self._atomic_write(private / expected_name, item.expected_bytes)
            for item in normalized_env:
                if item.value is not None:
                    self._atomic_write(private / _ENV_FILES[item.name], item.value.encode("utf-8"))
            payload = self._manifest_payload(change_id, target_id, normalized_leaves, normalized_env)
            digest = _sha256(_canonical_json(payload))
            self._atomic_write(private / "manifest.json", _canonical_json({**payload, "manifest_digest": digest}) + b"\n")
            return MaintenanceMaterialManifest(change_id, target_id, normalized_leaves, normalized_env, digest)
        except Exception:
            # 不将底层路径/正文包装进错误；不自动删除不完整材料，交给恢复策略审阅。
            raise MaintenanceMaterialError("维护材料写入失败")

    def load(self, change_id: str) -> MaintenanceMaterialManifest:
        """读取并验证 manifest 及全部材料摘要，失败时 fail-closed。"""

        directory = self._directory(change_id)
        private = self._private_directory(change_id)
        try:
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or self._has_reparse_component(directory)
                or not (directory / "transaction.json").is_file()
                or (directory / "transaction.json").is_symlink()
                or self._has_reparse_component(directory / "transaction.json")
                or not private.is_dir()
                or private.is_symlink()
                or self._has_reparse_component(private)
            ):
                raise MaintenanceMaterialError("事务材料目录无效")
            payload = json.loads((private / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
                raise MaintenanceMaterialError("事务材料 manifest 无效")
            digest = payload.pop("manifest_digest", None)
            if not isinstance(digest, str) or digest != _sha256(_canonical_json(payload)):
                raise MaintenanceMaterialError("事务材料完整性校验失败")
            if set(payload) != {"schema_version", "change_id", "target_id", "leaves", "environments"}:
                raise MaintenanceMaterialError("事务材料 manifest 字段无效")
            target = MAINTENANCE_TARGET_REGISTRY.get(payload.get("target_id"))
            if target is None:
                raise MaintenanceMaterialError("事务材料目标无效")
            if payload.get("change_id") != change_id:
                raise MaintenanceMaterialError("事务材料标识不一致")
            leaves = tuple(self._load_leaf(private, item) for item in payload.get("leaves", []))
            if tuple(item.leaf_id for item in leaves) != target.logical_leaves:
                raise MaintenanceMaterialError("事务材料叶子不完整")
            environments = tuple(self._load_environment(private, item) for item in payload.get("environments", []))
            if target.target_id == "environment" and tuple(item.name for item in environments) != _ENV_NAMES:
                raise MaintenanceMaterialError("环境材料不完整")
            return MaintenanceMaterialManifest(change_id, target.target_id, leaves, environments, digest)
        except MaintenanceMaterialError:
            raise
        except Exception as error:
            raise MaintenanceMaterialError("事务材料读取失败") from error

    def cleanup(self, change_id: str) -> None:
        """删除已安全终态事务的私有材料，保留 transaction.json 摘要。

        调用方必须先原子持久化 ``expired`` 等终态，再调用本方法；本方法只处理
        固定材料文件并拒绝重解析点，不删除事务目录或未知文件。清理失败会抛出
        ``MaintenanceMaterialError``，以便调用方记录/重试而不会误报清理完成。
        """

        directory = self._directory(change_id)
        private = self._private_directory(change_id)
        if not private.exists():
            # materialize 可能在创建 transaction.json 后、创建 private 前崩溃；
            # 此时没有私有材料需要清理，保持幂等并保留事务摘要即可。
            return
        if not private.is_dir() or private.is_symlink() or self._has_reparse_component(private):
            raise MaintenanceMaterialError("事务私有材料目录无效")
        known_files = {
            "manifest.json",
            *(
                file_name
                for names in _LEAF_FILES.values()
                for file_name in names
            ),
            *_ENV_FILES.values(),
        }
        try:
            # 先完整验证目录，再删除任何文件；否则未知项恰好排在已知项之后时，
            # 会留下“部分材料已删、未知材料仍在”的不可审计中间状态。
            entries = list(private.iterdir())
            for entry in entries:
                if entry.name not in known_files:
                    raise MaintenanceMaterialError("事务私有材料含未声明文件")
                if self._has_reparse_component(entry) or not entry.is_file():
                    raise MaintenanceMaterialError("事务私有材料文件无效")
            for entry in entries:
                entry.unlink()
            private.rmdir()
        except MaintenanceMaterialError:
            raise
        except (OSError, ValueError) as error:
            raise MaintenanceMaterialError("事务私有材料清理失败") from error

    def read_leaf(self, change_id: str, leaf_id: str) -> MaintenanceLeafMaterial:
        """读取并再次验证一个固定叶子的私有字节材料。"""

        manifest = self.load(change_id)
        _validate_leaf(leaf_id)
        for leaf in manifest.leaves:
            if leaf.leaf_id == leaf_id:
                return leaf
        raise MaintenanceMaterialError("事务材料叶子不存在")

    def read_environment(self, change_id: str, name: str) -> MaintenanceEnvironmentMaterial:
        """读取固定用户环境旧值；调用方不得将返回值放入公开投影。"""

        manifest = self.load(change_id)
        if name not in _ENV_NAMES:
            raise MaintenanceMaterialError("环境名未声明")
        for item in manifest.environments:
            if item.name == name:
                return item
        raise MaintenanceMaterialError("事务环境材料不存在")

    def _directory(self, change_id: str) -> Path:
        """将逻辑变更 ID 映射到事务根下固定子目录，不接受路径型 ID。"""

        _validate_change_id(change_id)
        directory = self._root / change_id
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or self._has_reparse_component(directory)
            or not (directory / "transaction.json").is_file()
            or (directory / "transaction.json").is_symlink()
            or self._has_reparse_component(directory / "transaction.json")
        ):
            raise MaintenanceMaterialError("事务目录或事实源不可用")
        return directory

    def _private_directory(self, change_id: str) -> Path:
        """返回既有事务目录下的唯一私有材料目录，不创建或解析边界外路径。"""

        return self._directory(change_id) / "private"

    @staticmethod
    def _manifest_payload(
        change_id: str,
        target_id: str,
        leaves: tuple[MaintenanceLeafMaterial, ...],
        environments: tuple[MaintenanceEnvironmentMaterial, ...],
    ) -> dict[str, Any]:
        """生成不含正文、环境值或物理路径的 manifest 载荷。"""

        return {
            "schema_version": _SCHEMA_VERSION,
            "change_id": change_id,
            "target_id": target_id,
            "leaves": [
                {
                    **item.public_dict(),
                    "before_file": _LEAF_FILES[item.leaf_id][0] if item.before_bytes is not None else None,
                    "expected_file": _LEAF_FILES[item.leaf_id][1],
                }
                for item in leaves
            ],
            "environments": [
                {
                    "name": item.name,
                    "state": item.state,
                    "value_file": _ENV_FILES[item.name] if item.value is not None else None,
                    "value_hash": _sha256(item.value.encode("utf-8")) if item.value is not None else None,
                }
                for item in environments
            ],
        }

    def _load_leaf(self, directory: Path, item: object) -> MaintenanceLeafMaterial:
        """根据固定文件名读取单叶子并验证正文摘要。"""

        if not isinstance(item, dict) or set(item) != {
            "leaf_id", "existence", "before_hash", "expected_hash", "file_mode", "before_file", "expected_file"
        }:
            raise MaintenanceMaterialError("叶子 manifest 无效")
        leaf_id = _validate_leaf(item["leaf_id"])
        before_name, expected_name = _LEAF_FILES[leaf_id]
        if item["before_file"] not in {None, before_name} or item["expected_file"] != expected_name:
            raise MaintenanceMaterialError("叶子材料文件映射无效")
        before = None if item["before_file"] is None else self._read_material(directory / before_name, item["before_hash"])
        expected = self._read_material(directory / expected_name, item["expected_hash"])
        return MaintenanceLeafMaterial(leaf_id, item["existence"], item["before_hash"], item["expected_hash"], item["file_mode"], before, expected)

    def _load_environment(self, directory: Path, item: object) -> MaintenanceEnvironmentMaterial:
        """根据固定文件名读取环境旧值并校验其摘要，manifest 不含具体值。"""

        if not isinstance(item, dict) or set(item) != {"name", "state", "value_file", "value_hash"}:
            raise MaintenanceMaterialError("环境 manifest 无效")
        name = item["name"]
        if name not in _ENV_NAMES:
            raise MaintenanceMaterialError("环境 manifest 名称无效")
        expected_file = _ENV_FILES[name] if item["state"] != "missing" else None
        if item["value_file"] != expected_file:
            raise MaintenanceMaterialError("环境材料文件映射无效")
        if item["state"] == "missing":
            value = None
        else:
            raw = self._read_material(directory / expected_file, item["value_hash"], limit=_MAX_ENV_BYTES)
            value = raw.decode("utf-8")
        material = MaintenanceEnvironmentMaterial(name, item["state"], value)
        if item["value_hash"] != (_sha256(value.encode("utf-8")) if value is not None else None):
            raise MaintenanceMaterialError("环境材料摘要不匹配")
        return material

    def _read_material(self, path: Path, expected_hash: object, *, limit: int = _MAX_LEAF_BYTES) -> bytes:
        """拒绝重解析点并读取固定材料文件，校验长度和 SHA-256。"""

        if self._has_reparse_component(path) or path.is_symlink() or not path.is_file():
            raise MaintenanceMaterialError("事务材料文件无效")
        data = path.read_bytes()
        _safe_bytes(data, "private", limit)
        _safe_hash(expected_hash, "material_hash", required=True)
        if _sha256(data) != expected_hash:
            raise MaintenanceMaterialError("事务材料摘要不匹配")
        return data

    def _atomic_write(self, destination: Path, data: bytes) -> None:
        """同目录临时文件写入、flush/fsync 后原子替换并刷新目录。"""

        # 固定材料文件只能首次创建；拒绝替换既有文件或链接，避免链接攻击
        # 把权限收紧和原子替换导向事务目录之外。
        if destination.exists() or destination.is_symlink() or self._has_reparse_component(destination):
            raise MaintenanceMaterialError("事务材料目标已存在或越界")
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(16)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._harden(temporary, False)
            os.replace(temporary, destination)
            self._harden(destination, False)
            try:
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                # Windows 目录句柄可能不支持 fsync；文件自身已 fsync，继续由上层验证。
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _harden(self, path: Path, directory: bool) -> None:
        """调用注入的权限收紧器；异常不带路径正文。"""

        try:
            self._permission_hardener(path, directory)
        except Exception as error:
            raise MaintenanceMaterialError("事务材料权限收紧失败") from error

    @staticmethod
    def _default_harden_permissions(path: Path, directory: bool) -> None:
        """最低限度收紧 mode；Windows ACL 必须由后续宿主实现显式注入。"""

        path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if directory else 0))

    @staticmethod
    def _has_reparse_component(path: Path) -> bool:
        """逐级检查符号链接/Windows 重解析点；缺失中间层继续检查父级。"""

        current = path
        while current != current.parent:
            try:
                if current.is_symlink():
                    return True
                attributes = getattr(current.stat(), "st_file_attributes", 0)
                if attributes & 0x400:
                    return True
            except (FileNotFoundError, NotADirectoryError):
                current = current.parent
                continue
            except OSError as error:
                raise MaintenanceMaterialError("事务材料边界无法判定") from error
            current = current.parent
        return False


__all__ = [
    "MaintenanceEnvironmentMaterial",
    "MaintenanceLeafMaterial",
    "MaintenanceMaterialError",
    "MaintenanceMaterialManifest",
    "MaintenanceMaterialStore",
]
