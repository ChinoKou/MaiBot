from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.chat.utils.typo_generator import ChineseTypoGenerator
from src.common.runtime_paths import get_char_frequency_path


@pytest.fixture(autouse=True)
def clear_runtime_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIBOT_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("MAIBOT_BUNDLE_ROOT", raising=False)


def test_char_frequency_uses_bundle_path_in_source_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    bundled_path = bundle_root / "depends-data" / "char_frequency.json"
    bundled_path.parent.mkdir(parents=True, exist_ok=True)
    bundled_path.write_text(json.dumps({"你": 123}), encoding="utf-8")
    monkeypatch.setenv("MAIBOT_BUNDLE_ROOT", str(bundle_root))

    generator = ChineseTypoGenerator.__new__(ChineseTypoGenerator)
    result = generator._load_or_create_char_frequency()

    assert result == {"你": 123}
    assert get_char_frequency_path() == bundled_path


def test_char_frequency_raises_when_bundled_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    monkeypatch.setenv("MAIBOT_BUNDLE_ROOT", str(bundle_root))

    generator = ChineseTypoGenerator.__new__(ChineseTypoGenerator)

    with pytest.raises(FileNotFoundError):
        generator._load_or_create_char_frequency()
