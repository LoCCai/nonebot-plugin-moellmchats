from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats import config as config_module
from nonebot_plugin_moellmchats import private_files


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_config_storage_is_tightened_and_atomic_rewrites_stay_private(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o777)
    config_dir.chmod(0o777)
    providers = config_dir / "providers.toml"
    providers.write_text("[providers]\n", encoding="utf-8")
    providers.chmod(0o666)

    immutable = config_dir / "generated_tools" / "versions" / "bundle" / ("a" * 64)
    immutable.mkdir(parents=True)
    immutable_file = immutable / "tool.py"
    immutable_file.write_text("pass\n", encoding="utf-8")
    immutable_file.chmod(0o444)
    immutable.chmod(0o555)

    monkeypatch.setattr(config_module, "config_path", config_dir)
    parser = config_module.ConfigParser()

    assert _mode(config_dir) == 0o700
    assert _mode(parser.filepath) == 0o600
    assert _mode(providers) == 0o600
    assert immutable.stat().st_mode & 0o222 == 0
    assert immutable_file.stat().st_mode & 0o222 == 0

    parser._write(dict(parser.config))
    assert _mode(parser.filepath) == 0o600


@pytest.mark.parametrize("value", [0, 1, config_module.MAX_CD_SECONDS])
def test_config_accepts_bounded_nonnegative_cooldown(value: int) -> None:
    candidate = dict(config_module.DEFAULT_CONFIG)
    candidate["cd_seconds"] = value
    config_module.ConfigParser._validate(candidate)


@pytest.mark.parametrize(
    "value",
    [-1, config_module.MAX_CD_SECONDS + 1, True, 1.5, "0", None],
)
def test_config_rejects_invalid_cooldown(value: object) -> None:
    candidate = dict(config_module.DEFAULT_CONFIG)
    candidate["cd_seconds"] = value
    with pytest.raises(ValueError, match="cd_seconds"):
        config_module.ConfigParser._validate(candidate)


def test_protected_tree_rejects_symlink_with_path_diagnostic(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = root / "linked.txt"
    link.symlink_to(target)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"禁止符号链接: .*linked\.txt",
    ):
        private_files.harden_private_tree(root)


def test_protected_tree_rejects_symlink_inside_skipped_immutable_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    versions = root / "versions"
    versions.mkdir(parents=True)
    target = tmp_path / "outside.py"
    target.write_text("pass\n", encoding="utf-8")
    (versions / "tool.py").symlink_to(target)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"禁止符号链接: .*tool\.py",
    ):
        private_files.harden_private_tree(root, skip_children=(versions,))


def test_protected_path_rejects_foreign_owner_with_uid_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    protected = tmp_path / "config.json"
    protected.write_text("{}", encoding="utf-8")
    actual_uid = os.geteuid()
    monkeypatch.setattr(private_files.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=rf"所有者不匹配: .*expected uid={actual_uid + 1}, actual uid={actual_uid}",
    ):
        private_files.ensure_private_file(protected)


def test_chmod_readonly_mount_error_is_wrapped_with_path_and_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    protected = tmp_path / "config.json"
    protected.write_text("{}", encoding="utf-8")

    def reject_chmod(*_args, **_kwargs) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(private_files.os, "chmod", reject_chmod)
    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"chmod\(0o600\).*config\.json.*Read-only file system.*errno=30",
    ):
        private_files.ensure_private_file(protected)


def test_non_posix_platform_fails_explicitly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(private_files, "_POSIX_PRIVATE_STORAGE_SUPPORTED", False)

    with pytest.raises(
        private_files.UnsupportedPrivateStoragePlatformError,
        match=r"仅支持.*POSIX",
    ):
        private_files.ensure_private_directory(tmp_path / "config")


def test_config_parser_rejects_symlink_instead_of_replacing_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"fastai_enabled": true}', encoding="utf-8")
    (config_dir / "config.json").symlink_to(outside)
    monkeypatch.setattr(config_module, "config_path", config_dir)

    with pytest.raises(
        private_files.PrivateStorageError,
        match=r"禁止符号链接: .*config\.json",
    ):
        config_module.ConfigParser()

    assert outside.read_text(encoding="utf-8") == '{"fastai_enabled": true}'
