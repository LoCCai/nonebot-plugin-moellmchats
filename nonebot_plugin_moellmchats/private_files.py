from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import stat
import sys
import tempfile

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
READONLY_DIRECTORY_MODE = 0o500
READONLY_FILE_MODE = 0o400

# Ownership and no-follow chmod checks in this module intentionally rely on
# POSIX uid/mode semantics. Failing explicitly is safer than pretending that
# Windows ACLs are equivalent to these bits.
_POSIX_PRIVATE_STORAGE_SUPPORTED = (
    os.name == "posix"
    and callable(getattr(os, "geteuid", None))
    and callable(getattr(os, "chmod", None))
)


class PrivateStorageError(ValueError):
    """A protected path cannot be validated or hardened safely."""


class UnsupportedPrivateStoragePlatformError(PrivateStorageError):
    """The host cannot provide the POSIX ownership and mode guarantees."""


def ensure_supported_private_storage_platform() -> None:
    """Require the POSIX uid/chmod semantics used by private plugin storage."""

    if not _POSIX_PRIVATE_STORAGE_SUPPORTED:
        raise UnsupportedPrivateStoragePlatformError(
            "私有配置存储仅支持具备 UID 与 chmod 语义的 POSIX 平台；"
            f"当前平台 os.name={os.name!r}, sys.platform={sys.platform!r}"
        )


def _os_error(action: str, path: Path, error: OSError) -> PrivateStorageError:
    detail = error.strerror or str(error)
    errno_detail = f" (errno={error.errno})" if error.errno is not None else ""
    return PrivateStorageError(
        f"私有存储{action}失败: {path}: {detail}{errno_detail}"
    )


def _current_uid() -> int:
    ensure_supported_private_storage_platform()
    try:
        return os.geteuid()
    except OSError as error:
        raise _os_error("读取当前 UID", Path("."), error) from error


def _owned_path(path: Path, *, allow_missing: bool) -> os.stat_result | None:
    """Return lstat data after rejecting links and foreign ownership."""

    ensure_supported_private_storage_platform()
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise PrivateStorageError(f"受保护路径不存在: {path}") from None
    except OSError as error:
        raise _os_error("检查路径元数据", path, error) from error

    if stat.S_ISLNK(info.st_mode):
        raise PrivateStorageError(f"受保护路径禁止符号链接: {path}")

    expected_uid = _current_uid()
    actual_uid = getattr(info, "st_uid", None)
    if actual_uid is None:
        raise UnsupportedPrivateStoragePlatformError(
            f"平台未提供受保护路径的所有者 UID: {path}"
        )
    if actual_uid != expected_uid:
        raise PrivateStorageError(
            "受保护路径所有者不匹配: "
            f"{path} (expected uid={expected_uid}, actual uid={actual_uid})"
        )
    return info


def _require_kind(path: Path, info: os.stat_result, *, directory: bool) -> None:
    matches = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not matches:
        expected = "真实目录" if directory else "普通文件"
        raise PrivateStorageError(f"受保护路径必须是{expected}: {path}")


def _chmod_owned(
    path: Path,
    mode: int,
    info: os.stat_result,
    *,
    directory: bool,
) -> None:
    """Apply a mode without accepting a link/replacement race silently."""

    _require_kind(path, info, directory=directory)
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, TypeError) as error:
        raise UnsupportedPrivateStoragePlatformError(
            "当前 POSIX 运行时不支持安全的 no-follow chmod: "
            f"{path}: {error}"
        ) from error
    except OSError as error:
        raise _os_error(f"设置权限 chmod(0o{mode:o})", path, error) from error

    after = _owned_path(path, allow_missing=False)
    assert after is not None
    _require_kind(path, after, directory=directory)
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        raise PrivateStorageError(f"受保护路径在 chmod 期间被替换: {path}")


def ensure_private_directory(path: Path) -> None:
    ensure_supported_private_storage_platform()
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as error:
        # If the leaf exists, inspect it so links, foreign owners, and wrong
        # object types get a precise diagnostic instead of FileExistsError.
        try:
            info = _owned_path(path, allow_missing=True)
        except PrivateStorageError:
            raise
        if info is not None:
            _require_kind(path, info, directory=True)
        raise _os_error("创建私有目录", path, error) from error

    info = _owned_path(path, allow_missing=False)
    assert info is not None
    _chmod_owned(path, PRIVATE_DIRECTORY_MODE, info, directory=True)


def ensure_private_file(path: Path) -> None:
    info = _owned_path(path, allow_missing=True)
    if info is None:
        return
    _chmod_owned(path, PRIVATE_FILE_MODE, info, directory=False)


def atomic_write_private_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace an owner-private text file with fsync durability."""

    ensure_private_directory(path.parent)
    # Replacing a symlink is not equivalent to validating the protected path.
    # Reject it (and foreign/special targets) before creating any new content.
    ensure_private_file(path)
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    except OSError as error:
        raise _os_error("创建原子写入临时文件", path.parent, error) from error

    temporary = Path(temporary_name)
    operation_failed = False
    descriptor_open = True
    try:
        try:
            with os.fdopen(fd, "w", encoding=encoding) as file:
                descriptor_open = False
                file.write(value)
                file.flush()
                os.fsync(file.fileno())
        except OSError as error:
            raise _os_error("写入并同步临时文件", temporary, error) from error

        info = _owned_path(temporary, allow_missing=False)
        assert info is not None
        _chmod_owned(temporary, PRIVATE_FILE_MODE, info, directory=False)
        try:
            os.replace(temporary, path)
        except OSError as error:
            raise _os_error("原子替换文件", path, error) from error
        ensure_private_file(path)
    except BaseException:
        operation_failed = True
        raise
    finally:
        if descriptor_open:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            if not operation_failed:
                raise _os_error("清理临时文件", temporary, error) from error


def _absolute_lexical(path: Path) -> Path:
    """Normalize without resolving links, which protected paths forbid."""

    return Path(os.path.abspath(os.fspath(path)))


def _directory_children(path: Path) -> list[tuple[Path, os.stat_result, bool]]:
    try:
        with os.scandir(path) as entries:
            children = sorted((Path(entry.path) for entry in entries), key=os.fspath)
    except OSError as error:
        raise _os_error("遍历受保护目录", path, error) from error

    inspected: list[tuple[Path, os.stat_result, bool]] = []
    for child in children:
        info = _owned_path(child, allow_missing=False)
        assert info is not None
        if stat.S_ISDIR(info.st_mode):
            inspected.append((child, info, True))
        elif stat.S_ISREG(info.st_mode):
            inspected.append((child, info, False))
        else:
            raise PrivateStorageError(
                f"受保护树只允许普通文件和真实目录: {child}"
            )
    return inspected


def harden_private_tree(
    root: Path,
    *,
    skip_children: Iterable[Path] = (),
) -> None:
    """Validate and tighten an owner-owned tree without following symlinks.

    Skipped child directories themselves are made private. Their contents are
    still validated (links, foreign owners, and special files fail closed) but
    retain their separately managed immutable modes.
    """

    info = _owned_path(root, allow_missing=True)
    if info is None:
        return
    _chmod_owned(root, PRIVATE_DIRECTORY_MODE, info, directory=True)

    skipped = {_absolute_lexical(path) for path in skip_children}
    protect_root_children = _absolute_lexical(root) not in skipped
    pending: list[tuple[Path, bool]] = [(root, protect_root_children)]
    while pending:
        current, protect_children = pending.pop()
        children = _directory_children(current)
        for child, child_info, is_directory in children:
            if protect_children:
                mode = PRIVATE_DIRECTORY_MODE if is_directory else PRIVATE_FILE_MODE
                _chmod_owned(
                    child,
                    mode,
                    child_info,
                    directory=is_directory,
                )
            if is_directory:
                child_protected = (
                    protect_children and _absolute_lexical(child) not in skipped
                )
                pending.append((child, child_protected))


def harden_readonly_tree(root: Path) -> None:
    """Validate an immutable tree and remove all owner write bits."""

    info = _owned_path(root, allow_missing=False)
    assert info is not None
    _chmod_owned(root, READONLY_DIRECTORY_MODE, info, directory=True)

    pending = [root]
    while pending:
        current = pending.pop()
        children = _directory_children(current)
        for child, child_info, is_directory in children:
            mode = READONLY_DIRECTORY_MODE if is_directory else READONLY_FILE_MODE
            _chmod_owned(child, mode, child_info, directory=is_directory)
            if is_directory:
                pending.append(child)
