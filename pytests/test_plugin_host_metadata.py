"""插件 Host 元数据读取测试。"""

from __future__ import annotations

from pathlib import Path

import json

from packaging.utils import canonicalize_name

from src.plugin_runtime.runner.manifest_validator import ManifestValidator


def test_manifest_validator_uses_frozen_host_metadata(monkeypatch, tmp_path: Path) -> None:
    """frozen 模式应从 bundle 元数据读取 Host 版本与依赖。"""

    metadata_path = tmp_path / "runtime" / "host_metadata.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host_version": "1.2.3",
                "sdk_version": "2.5.2",
                "dependencies": ["numpy>=2.2.6", "openai>=1.95.0"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MAIBOT_BUNDLE_ROOT", str(tmp_path))
    monkeypatch.setattr("src.common.runtime_paths.sys.frozen", True, raising=False)
    ManifestValidator._detect_default_host_version.cache_clear()
    ManifestValidator._detect_default_sdk_version.cache_clear()
    ManifestValidator._load_host_dependency_requirements.cache_clear()
    ManifestValidator._load_host_metadata.cache_clear()

    validator = ManifestValidator(project_root=tmp_path / "missing-project")

    try:
        assert validator._host_version == "1.2.3"
        assert validator._sdk_version == "2.5.2"
        requirements = validator.load_host_dependency_requirements()
        assert str(requirements[canonicalize_name("numpy")].specifier) == ">=2.2.6"
        assert str(requirements[canonicalize_name("openai")].specifier) == ">=1.95.0"
    finally:
        ManifestValidator._detect_default_host_version.cache_clear()
        ManifestValidator._detect_default_sdk_version.cache_clear()
        ManifestValidator._load_host_dependency_requirements.cache_clear()
        ManifestValidator._load_host_metadata.cache_clear()
