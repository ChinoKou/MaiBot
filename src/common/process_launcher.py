"""当前程序子进程启动命令构造工具。"""

from collections.abc import Sequence

import sys

from .runtime_paths import get_source_root, is_frozen_app

WORKER_PROCESS_ARG = "--worker"
PLUGIN_RUNNER_PROCESS_ARG = "--plugin-runner"
PLUGIN_PIP_INSTALL_PROCESS_ARG = "--plugin-pip-install"
INTERNAL_PROCESS_ARGS = frozenset({WORKER_PROCESS_ARG, PLUGIN_RUNNER_PROCESS_ARG, PLUGIN_PIP_INSTALL_PROCESS_ARG})


def build_self_launch_command(
    process_arg: str | None = None,
    passthrough_args: Sequence[str] | None = None,
) -> list[str]:
    """构造重新拉起当前 MaiBot 程序的命令。"""

    command = [sys.executable]
    if not is_frozen_app():
        command.append(str((get_source_root() / "bot.py").resolve()))

    if process_arg:
        command.append(process_arg)
    if passthrough_args:
        command.extend(passthrough_args)
    return command


def strip_internal_process_args(args: Sequence[str]) -> list[str]:
    """移除仅供 MaiBot 内部分发使用的进程模式参数。"""

    return [arg for arg in args if arg not in INTERNAL_PROCESS_ARGS]
