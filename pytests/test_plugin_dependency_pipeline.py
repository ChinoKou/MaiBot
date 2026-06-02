"""插件依赖流水线测试。"""

from pathlib import Path
from types import SimpleNamespace

import json

import pytest

from src.common.process_launcher import PLUGIN_PIP_INSTALL_PROCESS_ARG
from src.plugin_runtime.dependency_pipeline import (
    DependencyInstallResult,
    PluginDependencyPipeline,
    PluginPackageRequirement,
)


def _build_manifest(
    plugin_id: str,
    *,
    dependencies: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """构造测试用的 Manifest v2 数据。

    Args:
        plugin_id: 插件 ID。
        dependencies: 依赖声明列表。

    Returns:
        dict[str, object]: 可直接写入 ``_manifest.json`` 的字典。
    """

    return {
        "manifest_version": 2,
        "version": "1.0.0",
        "name": plugin_id,
        "description": "测试插件",
        "author": {
            "name": "tester",
            "url": "https://example.com/tester",
        },
        "license": "MIT",
        "urls": {
            "repository": f"https://example.com/{plugin_id}",
        },
        "host_application": {
            "min_version": "1.0.0",
            "max_version": "1.0.0",
        },
        "sdk": {
            "min_version": "2.0.0",
            "max_version": "2.99.99",
        },
        "dependencies": dependencies or [],
        "capabilities": [],
        "i18n": {
            "default_locale": "zh-CN",
            "supported_locales": ["zh-CN"],
        },
        "id": plugin_id,
    }


def _write_plugin(
    plugin_root: Path,
    plugin_name: str,
    plugin_id: str,
    *,
    dependencies: list[dict[str, str]] | None = None,
) -> Path:
    """在临时目录中写入一个测试插件。

    Args:
        plugin_root: 插件根目录。
        plugin_name: 插件目录名。
        plugin_id: 插件 ID。
        dependencies: Python 依赖声明列表。

    Returns:
        Path: 插件目录路径。
    """

    plugin_dir = plugin_root / plugin_name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("def create_plugin():\n    return object()\n", encoding="utf-8")
    (plugin_dir / "_manifest.json").write_text(
        json.dumps(_build_manifest(plugin_id, dependencies=dependencies)),
        encoding="utf-8",
    )
    return plugin_dir


def test_build_plan_blocks_plugin_conflicting_with_host_requirement(tmp_path: Path) -> None:
    """与主程序依赖冲突的插件应被阻止加载。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "conflict_plugin",
        "test.conflict-plugin",
        dependencies=[
            {
                "type": "python_package",
                "name": "numpy",
                "version_spec": "<1.0.0",
            }
        ],
    )

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    plan = pipeline.build_plan([plugin_root])

    assert "test.conflict-plugin" in plan.blocked_plugin_reasons
    assert "主程序" in plan.blocked_plugin_reasons["test.conflict-plugin"]
    assert plan.install_requirements == ()


def test_build_plan_blocks_plugins_with_conflicting_python_dependencies(tmp_path: Path) -> None:
    """插件之间出现 Python 包版本冲突时应同时阻止双方加载。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "plugin_a",
        "test.plugin-a",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package",
                "version_spec": "<2.0.0",
            }
        ],
    )
    _write_plugin(
        plugin_root,
        "plugin_b",
        "test.plugin-b",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package",
                "version_spec": ">=3.0.0",
            }
        ],
    )

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    plan = pipeline.build_plan([plugin_root])

    assert "test.plugin-a" in plan.blocked_plugin_reasons
    assert "test.plugin-b" in plan.blocked_plugin_reasons
    assert "test.plugin-b" in plan.blocked_plugin_reasons["test.plugin-a"]
    assert "test.plugin-a" in plan.blocked_plugin_reasons["test.plugin-b"]


def test_build_plan_collects_install_requirements_for_missing_packages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未安装但无冲突的依赖应进入自动安装计划。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "plugin_a",
        "test.plugin-a",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package",
                "version_spec": ">=1.0.0,<2.0.0",
            }
        ],
    )

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    monkeypatch.setattr(
        pipeline._manifest_validator,
        "get_installed_package_version",
        lambda package_name: None if package_name == "demo-package" else "1.0.0",
    )

    plan = pipeline.build_plan([plugin_root])

    assert plan.blocked_plugin_reasons == {}
    assert len(plan.install_requirements) == 1
    assert plan.install_requirements[0].package_name == "demo-package"
    assert plan.install_requirements[0].plugin_id == "test.plugin-a"
    assert plan.install_requirements[0].requirement_text == "demo-package>=1.0.0,<2.0.0"
    assert plan.install_requirements[0].target_dir.name == "python_packages"


@pytest.mark.asyncio
async def test_execute_blocks_plugins_when_auto_install_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自动安装失败时，相关插件应被阻止加载。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "plugin_a",
        "test.plugin-a",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package",
                "version_spec": ">=1.0.0,<2.0.0",
            }
        ],
    )

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    monkeypatch.setattr(
        pipeline._manifest_validator,
        "get_installed_package_version",
        lambda package_name: None if package_name == "demo-package" else "1.0.0",
    )

    async def fake_install(_requirements) -> DependencyInstallResult:
        """模拟依赖安装失败。"""

        return DependencyInstallResult(
            succeeded=False,
            environment_changed=False,
            failed_plugin_ids=("test.plugin-a",),
            error_message="network error",
        )

    monkeypatch.setattr(pipeline, "_install_requirements", fake_install)

    result = await pipeline.execute([plugin_root])

    assert result.environment_changed is False
    assert "test.plugin-a" in result.blocked_plugin_reasons
    assert "自动安装 Python 依赖失败" in result.blocked_plugin_reasons["test.plugin-a"]


@pytest.mark.asyncio
async def test_execute_blocks_only_plugins_with_failed_auto_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """部分插件依赖安装失败时，不应阻止已成功同步的插件。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "plugin_a",
        "test.plugin-a",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package-a",
                "version_spec": ">=1.0.0,<2.0.0",
            }
        ],
    )
    _write_plugin(
        plugin_root,
        "plugin_b",
        "test.plugin-b",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package-b",
                "version_spec": ">=1.0.0,<2.0.0",
            }
        ],
    )

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    monkeypatch.setattr(pipeline._manifest_validator, "get_installed_package_version", lambda _package_name: None)

    async def fake_install(requirements) -> DependencyInstallResult:
        """模拟其中一个插件安装失败。"""

        assert {requirement.plugin_id for requirement in requirements} == {"test.plugin-a", "test.plugin-b"}
        return DependencyInstallResult(
            succeeded=False,
            environment_changed=True,
            failed_plugin_ids=("test.plugin-b",),
            error_message="network error",
        )

    monkeypatch.setattr(pipeline, "_install_requirements", fake_install)

    result = await pipeline.execute([plugin_root])

    assert result.environment_changed is True
    assert "test.plugin-a" not in result.blocked_plugin_reasons
    assert "test.plugin-b" in result.blocked_plugin_reasons
    assert "自动安装 Python 依赖失败" in result.blocked_plugin_reasons["test.plugin-b"]


def test_build_plan_reinstalls_when_plugin_dependency_dir_shadows_global_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """插件外置目录中旧版本依赖优先于全局版本，应触发重装。"""

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "plugin_a",
        "test.plugin-a",
        dependencies=[
            {
                "type": "python_package",
                "name": "demo-package",
                "version_spec": ">=1.0.0,<2.0.0",
            }
        ],
    )
    dependency_root = tmp_path / "state"
    (dependency_root / "test.plugin-a" / "python_packages").mkdir(parents=True)

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    monkeypatch.setattr(
        "src.plugin_runtime.dependency_pipeline.get_plugin_dependency_dir",
        lambda plugin_id: dependency_root / plugin_id / "python_packages",
    )
    monkeypatch.setattr(
        pipeline._manifest_validator,
        "get_installed_package_version",
        lambda package_name: "1.5.0" if package_name == "demo-package" else None,
    )
    monkeypatch.setattr(pipeline, "_get_distribution_version_from_path", lambda _package_name, _target_dir: "0.9.0")

    plan = pipeline.build_plan([plugin_root])

    assert plan.blocked_plugin_reasons == {}
    assert len(plan.install_requirements) == 1
    assert plan.install_requirements[0].plugin_id == "test.plugin-a"
    assert plan.install_requirements[0].requirement_text == "demo-package>=1.0.0,<2.0.0"


@pytest.mark.asyncio
async def test_install_requirements_keeps_successes_when_one_target_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安装多个目标目录时，一个失败不应抹掉其他成功目标的环境变更状态。"""

    pipeline = PluginDependencyPipeline(project_root=Path.cwd())
    target_a = tmp_path / "a_packages"
    target_b = tmp_path / "b_packages"

    def fake_run(command, **_kwargs) -> SimpleNamespace:
        """按目标目录模拟 pip 命令结果。"""

        if str(target_a) in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="network error")

    monkeypatch.setattr("src.plugin_runtime.dependency_pipeline.subprocess.run", fake_run)

    result = await pipeline._install_requirements(
        [
            PluginPackageRequirement(
                package_name="demo-package-a",
                plugin_id="test.plugin-a",
                requirement_text="demo-package-a>=1.0.0",
                version_spec=">=1.0.0",
                target_dir=target_a,
            ),
            PluginPackageRequirement(
                package_name="demo-package-b",
                plugin_id="test.plugin-b",
                requirement_text="demo-package-b>=1.0.0",
                version_spec=">=1.0.0",
                target_dir=target_b,
            ),
        ]
    )

    assert result.succeeded is False
    assert result.environment_changed is True
    assert result.failed_plugin_ids == ("test.plugin-b",)
    assert "test.plugin-b: network error" in result.error_message


def test_build_install_command_uses_internal_pip_mode_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """frozen 模式下应复用当前可执行文件执行内部 pip 子命令。"""

    monkeypatch.setattr("src.common.runtime_paths.sys.frozen", True, raising=False)
    monkeypatch.setattr("src.common.process_launcher.sys.frozen", True, raising=False)
    monkeypatch.setattr("src.plugin_runtime.dependency_pipeline.sys.frozen", True, raising=False)
    monkeypatch.setattr("src.common.process_launcher.sys.executable", str(tmp_path / "MaiBot.exe"))

    command = PluginDependencyPipeline._build_install_command(["demo-package>=1.0.0"], tmp_path / "packages")

    assert command == [
        str(tmp_path / "MaiBot.exe"),
        PLUGIN_PIP_INSTALL_PROCESS_ARG,
        "--disable-pip-version-check",
        "--no-input",
        "--no-warn-script-location",
        "--target",
        str(tmp_path / "packages"),
        "--upgrade",
        "demo-package>=1.0.0",
    ]
