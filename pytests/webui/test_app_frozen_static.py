"""WebUI frozen 静态资源路径测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.webui import app as webui_app


def test_resolve_static_path_prefers_bundled_dashboard_in_frozen_mode(monkeypatch, tmp_path: Path) -> None:
    """frozen 模式应优先使用 bundle root 下的 dashboard/dist。"""

    bundled_dist = tmp_path / "dashboard" / "dist"
    bundled_dist.mkdir(parents=True)
    (bundled_dist / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setenv("MAIBOT_BUNDLE_ROOT", str(tmp_path))
    monkeypatch.setattr("src.common.runtime_paths.sys.frozen", True, raising=False)

    with patch.object(webui_app, "import_module") as import_module_mock:
        resolved_path = webui_app._resolve_static_path()

    assert resolved_path == bundled_dist.resolve()
    import_module_mock.assert_not_called()
