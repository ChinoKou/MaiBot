from pathlib import Path

import os
import sys


_RUNTIME_ROOT_ENV = "MAIBOT_RUNTIME_ROOT"
_BUNDLE_ROOT_ENV = "MAIBOT_BUNDLE_ROOT"
PLUGIN_STATE_MIGRATION_MARKER_NAME = ".legacy_state_migrated"


def is_frozen_app() -> bool:
    """判断当前是否运行在冻结后的二进制环境中。"""

    return bool(getattr(sys, "frozen", False))


def get_source_root() -> Path:
    """返回源码模式下的项目根目录。"""

    return Path(__file__).resolve().parents[2]


def get_install_root() -> Path:
    """返回实际安装根目录。

    - 源码模式：项目根目录
    - frozen 模式：exe 所在目录
    """

    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return get_source_root()


def get_bundle_root() -> Path:
    """返回只读资源根目录。

    - 源码模式：项目根目录
    - frozen 模式：优先使用 ``MAIBOT_BUNDLE_ROOT``，否则使用 ``sys._MEIPASS``
    """

    configured_root = os.getenv(_BUNDLE_ROOT_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    if is_frozen_app():
        meipass_root = getattr(sys, "_MEIPASS", "")
        if meipass_root:
            return Path(str(meipass_root)).resolve()

    return get_source_root()


def get_runtime_root() -> Path:
    """返回运行期对外可见的根目录。

    默认与安装根目录一致，也允许通过环境变量覆盖。
    """

    configured_root = os.getenv(_RUNTIME_ROOT_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return get_install_root()


def get_runtime_path(*parts: str) -> Path:
    """基于运行期根目录拼接路径。"""

    return get_runtime_root().joinpath(*parts)


def get_bundle_path(*parts: str) -> Path:
    """基于资源根目录拼接路径。"""

    return get_bundle_root().joinpath(*parts)


def get_config_dir() -> Path:
    return get_runtime_path("config")


def get_bot_config_path() -> Path:
    return get_config_dir() / "bot_config.toml"


def get_model_config_path() -> Path:
    return get_config_dir() / "model_config.toml"


def get_data_dir() -> Path:
    return get_runtime_path("data")


def get_logs_dir() -> Path:
    return get_runtime_path("logs")


def get_plugins_dir() -> Path:
    return get_runtime_path("plugins")


def get_plugin_state_root() -> Path:
    return get_plugins_dir() / "data"


def get_plugin_state_dir(plugin_id: str) -> Path:
    normalized_plugin_id = str(plugin_id or "").strip()
    if not normalized_plugin_id:
        raise ValueError("plugin_id 不能为空")
    return get_plugin_state_root() / normalized_plugin_id


def get_plugin_dependency_dir(plugin_id: str) -> Path:
    return get_plugin_state_dir(plugin_id) / "python_packages"


def get_locales_dir() -> Path:
    return get_bundle_path("locales")


def _contains_prompt_templates(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    try:
        return any(path.is_file() for path in directory.rglob("*.prompt"))
    except OSError:
        return False


def get_prompts_dir() -> Path:
    runtime_prompts_dir = get_runtime_path("prompts")
    bundled_prompts_dir = get_bundle_path("prompts")

    if is_frozen_app():
        if bundled_prompts_dir.exists():
            return bundled_prompts_dir
        return runtime_prompts_dir

    if runtime_prompts_dir.exists():
        if _contains_prompt_templates(runtime_prompts_dir) or not bundled_prompts_dir.exists():
            return runtime_prompts_dir

    if bundled_prompts_dir.exists():
        return bundled_prompts_dir

    return runtime_prompts_dir


def get_custom_prompts_dir() -> Path:
    return get_data_dir() / "custom_prompts"


def get_runtime_dir() -> Path:
    return get_runtime_path("runtime")


def get_builtin_plugins_dir() -> Path:
    return get_bundle_path("src", "plugins", "built_in")


def get_dashboard_dist_dir() -> Path:
    return get_bundle_path("dashboard", "dist")


def get_host_metadata_path() -> Path:
    return get_bundle_path("runtime", "host_metadata.json")
