"""生成 PyInstaller 打包时使用的 Host 依赖元数据。"""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import argparse
import json
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "runtime" / "host_metadata.json"
SDK_PACKAGE_NAME = "maibot-plugin-sdk"


def _load_pyproject(path: Path) -> dict[str, Any]:
    with path.open("rb") as file_obj:
        data = tomllib.load(file_obj)
    return data if isinstance(data, dict) else {}


def _detect_sdk_version() -> str:
    try:
        return importlib_metadata.version(SDK_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        pass

    sdk_pyproject_path = PROJECT_ROOT / "packages" / SDK_PACKAGE_NAME / "pyproject.toml"
    try:
        sdk_project = _load_pyproject(sdk_pyproject_path).get("project", {})
    except Exception:
        return ""
    if not isinstance(sdk_project, dict):
        return ""
    return str(sdk_project.get("version", "") or "").strip()


def build_host_metadata() -> dict[str, Any]:
    pyproject = _load_pyproject(PROJECT_ROOT / "pyproject.toml")
    project = pyproject.get("project", {})
    if not isinstance(project, dict):
        project = {}

    raw_dependencies = project.get("dependencies", [])
    dependencies = [str(dependency or "").strip() for dependency in raw_dependencies if str(dependency or "").strip()] if isinstance(raw_dependencies, list) else []

    return {
        "schema_version": 1,
        "host_version": str(project.get("version", "") or "").strip(),
        "sdk_version": _detect_sdk_version(),
        "dependencies": dependencies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate MaiBot host metadata for frozen builds.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Output JSON path. Defaults to runtime/host_metadata.json.",
    )
    args = parser.parse_args()

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_host_metadata(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"host metadata written: {output_path}")


if __name__ == "__main__":
    main()
