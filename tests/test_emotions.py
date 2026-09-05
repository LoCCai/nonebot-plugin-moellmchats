from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nonebot_plugin_moellmchats import utils
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots

if TYPE_CHECKING:
    import pytest


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_emotion_candidate_only_publishes_directories_with_valid_images(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "有效" / "smile.PNG", _PNG)
    _write(tmp_path / "仅系统文件" / "Thumbs.db", b"not an image")
    _write(tmp_path / "伪装图片" / "fake.jpg", b"not a jpeg")
    _write(tmp_path / "空文件" / "empty.gif", b"")
    oversized = _write(tmp_path / "过大" / "huge.webp", b"RIFF\x00\x00\x00\x00WEBP")
    with oversized.open("ab") as file:
        file.truncate(utils._MAX_EMOTION_IMAGE_BYTES + 1)
    (tmp_path / "空目录").mkdir()

    symlink_file_group = tmp_path / "文件符号链接"
    symlink_file_group.mkdir()
    (symlink_file_group / "linked.png").symlink_to(tmp_path / "有效" / "smile.PNG")
    (tmp_path / "目录符号链接").symlink_to(tmp_path / "有效", target_is_directory=True)

    assert utils.load_emotions_candidate({"emotions_enabled": True, "emotions_dir": str(tmp_path)}) == ("有效",)
    assert utils.load_emotions_candidate({"emotions_enabled": False, "emotions_dir": str(tmp_path)}) == ()


def test_legacy_emotion_cache_uses_the_same_filtered_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "有效" / "one.jpeg", _JPEG)
    _write(tmp_path / "奶龙" / "Thumbs.db", b"database")
    monkeypatch.setattr(runtime_snapshots, "active", lambda: None)
    monkeypatch.setattr(
        utils.config_parser,
        "get_config",
        lambda key, *_args: {
            "emotions_enabled": True,
            "emotions_dir": str(tmp_path),
        }[key],
    )
    utils.invalidate_resource_caches()

    assert utils.get_emotions_names() == ["有效"]


def test_get_emotion_revalidates_selected_file_and_rejects_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write(tmp_path / "有效" / "one.png", _PNG)
    monkeypatch.setattr(utils, "get_emotions_names", lambda: ["有效"])
    monkeypatch.setattr(
        utils.config_parser,
        "get_config",
        lambda key, *_args: str(tmp_path) if key == "emotions_dir" else True,
    )

    segment = utils.get_emotion("有效")
    assert segment is not None
    assert segment.type == "image"
    assert str(segment.data["file"]).startswith("base64://")
    assert utils.get_emotion("../有效") is None
    assert utils.get_emotion("有效", protocol="onebot_v12") is None

    image.write_bytes(b"corrupted after candidate publication")
    assert utils.get_emotion("有效") is None
