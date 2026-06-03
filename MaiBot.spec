# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir build spec for MaiBot."""

from __future__ import annotations

import ensurepip
import json
import sys
from pathlib import Path

from PyInstaller.building import build_main as _pyi_build_main
from PyInstaller.utils.hooks import collect_data_files, copy_metadata


# -----------------------------------------------------------------------------
# Paths and collection configuration
# -----------------------------------------------------------------------------

ROOT = Path(SPECPATH).resolve()
METADATA_PATH = ROOT / "build" / "pyinstaller" / "host_metadata.json"

ROOT_DATA_FILES = (
    ("EULA.md", "."),
    ("PRIVACY.md", "."),
    ("scripts/replay_llm_request.py", "scripts"),
)
DATA_TREES = (
    (ROOT / "src" / "config", "src/config"),
    (ROOT / "src" / "plugins" / "built_in", "src/plugins/built_in"),
    (ROOT / "locales", "locales"),
    (ROOT / "prompts", "prompts"),
    (Path(ensurepip.__file__).resolve().parent / "_bundled", "ensurepip/_bundled"),
)
DATA_PACKAGES = ("ensurepip", "maibot_dashboard")
METADATA_PACKAGES = ("maibot-plugin-sdk",)
HIDDEN_IMPORTS = (
    "maibot_dashboard",
    "src.llm_models.model_client.openai_client",
    "src.llm_models.model_client.gemini_client",
)
PROJECT_PACKAGES_EXCLUDED_FROM_DLL_PROBE = ("src",)
UPX_EXCLUDES = ("_uuid.pyd", "python3.dll")

sys.path.insert(0, str(ROOT))
from scripts.generate_host_metadata import build_host_metadata  # noqa: E402


# -----------------------------------------------------------------------------
# PyInstaller compatibility adjustments
# -----------------------------------------------------------------------------

_original_find_binary_dependencies = _pyi_build_main.find_binary_dependencies


def _is_project_package(package_name: str) -> bool:
    return any(
        package_name == package_root or package_name.startswith(f"{package_root}.")
        for package_root in PROJECT_PACKAGES_EXCLUDED_FROM_DLL_PROBE
    )


def _find_binary_dependencies_without_project_imports(binaries, import_packages, symlink_suppression_patterns):
    """Skip importing MaiBot business packages during Windows DLL probing.

    PyInstaller imports every collected package in an isolated Windows probe to
    discover extra DLL search paths. Importing ``src`` executes runtime singletons
    such as config, emoji manager, and local storage, so only third-party packages
    should participate in that probe.
    """

    filtered_import_packages = [package for package in import_packages if not _is_project_package(package)]
    return _original_find_binary_dependencies(binaries, filtered_import_packages, symlink_suppression_patterns)


def _install_pyinstaller_dll_probe_filter() -> None:
    _pyi_build_main.find_binary_dependencies = _find_binary_dependencies_without_project_imports


# -----------------------------------------------------------------------------
# Data collection helpers
# -----------------------------------------------------------------------------


def _write_host_metadata() -> None:
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(
        json.dumps(build_host_metadata(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _add_data_tree(datas: list[tuple[str, str]], source_dir: Path, dest_dir: str) -> None:
    if not source_dir.is_dir():
        return

    for source_file in source_dir.rglob("*"):
        if not source_file.is_file():
            continue
        if "__pycache__" in source_file.parts or source_file.suffix == ".pyc":
            continue
        relative_parent = source_file.relative_to(source_dir).parent
        datas.append((str(source_file), str(Path(dest_dir) / relative_parent)))


def _safe_collect_data_files(package_name: str) -> list[tuple[str, str]]:
    try:
        return collect_data_files(package_name)
    except Exception:
        return []


def _safe_copy_metadata(package_name: str) -> list[tuple[str, str]]:
    try:
        return copy_metadata(package_name)
    except Exception:
        return []


def _collect_datas() -> list[tuple[str, str]]:
    datas: list[tuple[str, str]] = []

    for resource_name, dest_dir in ROOT_DATA_FILES:
        resource_path = ROOT / resource_name
        if resource_path.is_file():
            datas.append((str(resource_path), dest_dir))

    datas.append((str(METADATA_PATH), "runtime"))

    for source_dir, dest_dir in DATA_TREES:
        _add_data_tree(datas, source_dir, dest_dir)

    for package_name in DATA_PACKAGES:
        datas.extend(_safe_collect_data_files(package_name))

    for package_name in METADATA_PACKAGES:
        datas.extend(_safe_copy_metadata(package_name))

    return datas


_install_pyinstaller_dll_probe_filter()
_write_host_metadata()


# -----------------------------------------------------------------------------
# Build definition
# -----------------------------------------------------------------------------


a = Analysis(
    [str(ROOT / "bot.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_collect_datas(),
    hiddenimports=list(HIDDEN_IMPORTS),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytests", "tests"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MaiBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=list(UPX_EXCLUDES),
    name="MaiBot",
)
