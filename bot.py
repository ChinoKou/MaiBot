# raise RuntimeError("System Not Ready")
from pathlib import Path
from rich.traceback import install

import asyncio
import contextlib
import hashlib
import os
import platform
# import shutil
import subprocess
import sys
import time
import traceback
import zipfile

from src.common.i18n import set_locale, t, tn
from src.common.logger import get_logger, initialize_logging, shutdown_logging
from src.common.process_launcher import (
    PLUGIN_PIP_INSTALL_PROCESS_ARG,
    PLUGIN_RUNNER_PROCESS_ARG,
    WORKER_PROCESS_ARG,
    build_self_launch_command,
    strip_internal_process_args,
)
from src.common.runtime_loop import set_main_loop
from src.common.runtime_paths import get_bundle_path, get_runtime_path, get_runtime_root
from src.config.legacy_upgrade_confirmation import require_legacy_upgrade_confirmation

RUNTIME_ROOT = get_runtime_root().resolve()
os.chdir(RUNTIME_ROOT)
set_locale(os.getenv("MAIBOT_LOCALE", "zh-CN"))

# 检查是否是 Worker 进程，只在 Worker 进程中输出详细的初始化信息
# Runner 进程只需要基本的日志功能，不需要详细的初始化日志
is_worker = os.environ.get("MAIBOT_WORKER_PROCESS") == "1" or WORKER_PROCESS_ARG in sys.argv[1:]
is_plugin_runner = PLUGIN_RUNNER_PROCESS_ARG in sys.argv[1:]
is_plugin_pip_install = PLUGIN_PIP_INSTALL_PROCESS_ARG in sys.argv[1:]
sys.argv = [sys.argv[0], *strip_internal_process_args(sys.argv[1:])]
initialize_logging(verbose=is_worker and not is_plugin_pip_install)
install(extra_lines=3)
logger = get_logger("main")

# 定义重启退出码
RESTART_EXIT_CODE = 42
# print("-----------------------------------------")
# print("\n\n\n\n\n")
# print(t("startup.dev_branch_warning"))
# print("\n\n\n\n\n")
# print("-----------------------------------------")


def _print_interrupt_exit_notice() -> None:
    """在日志系统不可用或正在退出时，用最小输出提示 Ctrl+C 退出。"""

    print("\n收到 Ctrl+C，中断退出。")


def _iter_bundled_pip_wheels() -> list[Path]:
    """查找 PyInstaller 包内随 ensurepip 携带的 pip wheel。"""

    candidate_roots: list[Path] = []
    meipass_root = getattr(sys, "_MEIPASS", "")
    if meipass_root:
        candidate_roots.append(Path(str(meipass_root)).resolve())
    candidate_roots.append(get_bundle_path().resolve())

    pip_wheels: list[Path] = []
    for root in candidate_roots:
        bundled_dir = root / "ensurepip" / "_bundled"
        pip_wheels.extend(sorted(bundled_dir.glob("pip-*.whl"), reverse=True))
    return pip_wheels


def _prepare_filesystem_pip_path(pip_wheel: Path) -> Path:
    """将 pip wheel 解压到外置运行目录，避免 frozen loader 影响 distlib 资源读取。"""

    target_dir = get_runtime_path("runtime", "pip", pip_wheel.stem).resolve()
    marker_path = target_dir / ".maibot_pip_wheel"
    marker_text = pip_wheel.name
    if marker_path.is_file() and marker_path.read_text(encoding="utf-8") == marker_text:
        return target_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pip_wheel) as wheel_zip:
        wheel_zip.extractall(target_dir)
    marker_path.write_text(marker_text, encoding="utf-8")
    return target_dir


def _load_bundled_pip_main():
    """加载 PyInstaller 包内的 pip 入口。"""

    for pip_wheel in _iter_bundled_pip_wheels():
        pip_path = _prepare_filesystem_pip_path(pip_wheel)
        sys.path.insert(0, str(pip_path))
        try:
            from pip._internal.cli.main import main as pip_main

            return pip_main
        except ModuleNotFoundError as exc:
            if exc.name != "pip":
                raise
            with contextlib.suppress(ValueError):
                sys.path.remove(str(pip_path))

    try:
        from pip._internal.cli.main import main as pip_main

        return pip_main
    except ModuleNotFoundError as exc:
        if exc.name != "pip":
            raise

    raise ModuleNotFoundError("No module named 'pip'")


def run_plugin_pip_install_process(args: list[str]) -> None:
    """使用当前 frozen 运行时执行 pip 安装命令。"""

    pip_main = _load_bundled_pip_main()
    sys.exit(pip_main(["install", *args]))


def run_runner_process():
    """
    Runner 进程逻辑：作为守护进程运行，负责启动和监控 Worker 进程。
    处理重启请求 (退出码 42) 和 Ctrl+C 信号。
    """
    passthrough_args = strip_internal_process_args(sys.argv[1:])

    # 设置环境变量，标记子进程为 Worker 进程
    env = os.environ.copy()
    env["MAIBOT_WORKER_PROCESS"] = "1"

    while True:
        cmd = build_self_launch_command(WORKER_PROCESS_ARG, passthrough_args)
        logger.info(t("startup.launching_script", script_file=" ".join(cmd)))
        logger.info(t("startup.compiling_shaders"))

        process = subprocess.Popen(cmd, env=env)

        try:
            # 等待子进程结束
            return_code = process.wait()

            if return_code == RESTART_EXIT_CODE:
                logger.info(t("startup.restart_requested", exit_code=RESTART_EXIT_CODE))
                time.sleep(1)  # 稍作等待
                continue
            else:
                logger.info(t("startup.program_exited", return_code=return_code))
                sys.exit(return_code)

        except KeyboardInterrupt:
            # 向子进程发送终止信号
            if process.poll() is None:
                # 在 Windows 上，Ctrl+C 通常已经发送给了子进程（如果它们共享控制台）
                # 但为了保险，我们可以尝试 terminate
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(t("startup.child_process_force_kill"))
                    process.kill()
            sys.exit(0)


if is_plugin_pip_install:
    run_plugin_pip_install_process(sys.argv[1:])

if is_plugin_runner:
    from src.plugin_runtime.runner.runner_main import main as plugin_runner_main

    plugin_runner_main()
    sys.exit(0)

# 检查是否是 Worker 进程
# 如果没有设置 Worker 标记，说明是直接运行的脚本，此时应该作为 Runner 运行。
if not is_worker:
    if __name__ == "__main__":
        require_legacy_upgrade_confirmation(RUNTIME_ROOT)
        run_runner_process()
    # 如果作为模块导入，不执行 Runner 逻辑，但也不应该执行下面的 Worker 逻辑
    sys.exit(0)

# 以下是 Worker 进程的逻辑

# 最早期初始化日志系统，确保所有后续模块都使用正确的日志格式
# 注意：Runner 进程已经在第 37 行初始化了日志系统，但 Worker 进程是独立进程，需要重新初始化
# 由于 Runner 和 Worker 是不同进程，它们有独立的内存空间，所以都会初始化一次
# 这是正常的，但为了避免重复的初始化日志，我们在 initialize_logging() 中添加了防重复机制
# 不过由于是不同进程，每个进程仍会初始化一次，这是预期的行为

require_legacy_upgrade_confirmation(RUNTIME_ROOT)

logger.info(t("startup.worker_dir_set", script_dir=RUNTIME_ROOT))

from src.main import MainSystem  # noqa
from src.manager.async_task_manager import async_task_manager  # noqa


# logger = get_logger("main")


# install(extra_lines=3)

# 设置工作目录为脚本所在目录
# script_dir = os.path.dirname(os.path.abspath(__file__))
# os.chdir(script_dir)
confirm_logger = get_logger("confirm")
# 获取没有加载env时的环境变量
env_mask = {key: os.getenv(key) for key in os.environ}

uvicorn_server = None
driver = None
app = None
loop = None


def print_opensource_notice():
    """打印开源项目提示，防止倒卖"""
    from colorama import init, Fore, Style

    init()

    notice_lines = [
        "",
        f"{Fore.CYAN}{'═' * 70}{Style.RESET_ALL}",
        f"{Fore.GREEN}{t('startup.opensource_title')}{Style.RESET_ALL}",
        f"{Fore.CYAN}{'─' * 70}{Style.RESET_ALL}",
        f"{Fore.YELLOW}{t('startup.opensource_free_notice')}{Style.RESET_ALL}",
        f"{Fore.WHITE}{t('startup.opensource_scamming_notice')}{Style.RESET_ALL}",
        "",
        f"{Fore.WHITE}{t('startup.opensource_repo')}{Fore.BLUE}{t('startup.opensource_repo_value')} {Style.RESET_ALL}",
        f"{Fore.WHITE}{t('startup.opensource_docs')}{Fore.BLUE}{t('startup.opensource_docs_value')} {Style.RESET_ALL}",
        f"{Fore.WHITE}{t('startup.opensource_group')}{Fore.BLUE}{t('startup.opensource_group_value')}{Style.RESET_ALL}",
        f"{Fore.CYAN}{'─' * 70}{Style.RESET_ALL}",
        f"{Fore.RED}  ⚠ {t('startup.opensource_resale_warning').strip()}{Style.RESET_ALL}",
        f"{Fore.CYAN}{'═' * 70}{Style.RESET_ALL}",
        "",
    ]

    for line in notice_lines:
        print(line)


def easter_egg():
    # 彩蛋
    from colorama import init, Fore

    init()
    text = t("startup.easter_egg")
    rainbow_colors = [Fore.RED, Fore.YELLOW, Fore.GREEN, Fore.CYAN, Fore.BLUE, Fore.MAGENTA]
    rainbow_text = ""
    for i, char in enumerate(text):
        rainbow_text += rainbow_colors[i % len(rainbow_colors)] + char
    print(rainbow_text)


async def graceful_shutdown():  # sourcery skip: use-named-expression
    try:
        logger.info(t("startup.shutdown_started"))

        # 关闭 WebUI 服务器
        # try:
        #     from src.webui.webui_server import get_webui_server

        #     webui_server = get_webui_server()
        #     if webui_server and webui_server._server:
        #         await webui_server.shutdown()
        # except Exception as e:
        #     logger.warning(f"关闭 WebUI 服务器时出错: {e}")

        from src.core.event_bus import event_bus
        from src.core.types import EventType

        # 触发 ON_STOP 事件
        await event_bus.emit(event_type=EventType.ON_STOP)

        # 停止新版本插件运行时
        from src.plugin_runtime.integration import get_plugin_runtime_manager

        await get_plugin_runtime_manager().stop()

        # 停止所有异步任务
        await async_task_manager.stop_and_wait_all_tasks()

        # 获取所有剩余任务，排除当前任务
        remaining_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

        if remaining_tasks:
            logger.info(tn("startup.remaining_tasks_cancelling", len(remaining_tasks)))

            # 取消所有剩余任务
            for task in remaining_tasks:
                if not task.done():
                    task.cancel()

            # 等待所有任务完成，设置超时
            try:
                await asyncio.wait_for(asyncio.gather(*remaining_tasks, return_exceptions=True), timeout=15.0)
                logger.info(t("startup.remaining_tasks_cancelled"))
            except asyncio.TimeoutError:
                logger.warning(t("startup.remaining_tasks_cancel_timeout"))
            except Exception as e:
                logger.error(t("startup.remaining_tasks_cancel_error", error=e))

        logger.info(t("startup.shutdown_completed"))

    except Exception as e:
        logger.error(t("startup.shutdown_failed", error=e), exc_info=True)


def _calculate_file_hash(file_path: Path, file_type: str) -> str:
    """计算文件的MD5哈希值"""
    if not file_path.exists():
        logger.error(t("startup.file_not_found", file_type=file_type))
        raise FileNotFoundError(t("startup.file_not_found", file_type=file_type))

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _check_agreement_status(file_hash: str, confirm_file: Path, env_var: str) -> tuple[bool, bool]:
    """检查协议确认状态

    Returns:
        tuple[bool, bool]: (已确认, 未更新)
    """
    # 检查环境变量确认
    if file_hash == os.getenv(env_var):
        return True, False

    # 检查确认文件
    if confirm_file.exists():
        with open(confirm_file, "r", encoding="utf-8") as f:
            confirmed_content = f.read()
        if file_hash == confirmed_content:
            return True, False

    return False, True


def _prompt_user_confirmation(eula_hash: str, privacy_hash: str) -> None:
    """提示用户确认协议"""
    confirm_logger.critical(t("startup.agreement_reconfirm"))
    confirm_logger.critical(
        t(
            "startup.agreement_confirm_prompt",
            eula_hash=eula_hash,
            privacy_hash=privacy_hash,
        )
    )

    while True:
        user_input = input().strip().lower()
        if user_input in ["同意", "confirmed"]:
            return
        confirm_logger.critical(t("startup.agreement_confirm_retry"))


def _save_confirmations(eula_updated: bool, privacy_updated: bool, eula_hash: str, privacy_hash: str) -> None:
    """保存用户确认结果"""
    if eula_updated:
        logger.info(
            t(
                "startup.agreement_updated",
                agreement_name=t("startup.eula_name"),
                file_hash=eula_hash,
            )
        )
        get_runtime_path("eula.confirmed").write_text(eula_hash, encoding="utf-8")

    if privacy_updated:
        logger.info(
            t(
                "startup.agreement_updated",
                agreement_name=t("startup.privacy_name"),
                file_hash=privacy_hash,
            )
        )
        get_runtime_path("privacy.confirmed").write_text(privacy_hash, encoding="utf-8")


def check_eula():
    """检查EULA和隐私条款确认状态"""
    # 计算文件哈希值
    eula_hash = _calculate_file_hash(get_bundle_path("EULA.md"), "EULA.md")
    privacy_hash = _calculate_file_hash(get_bundle_path("PRIVACY.md"), "PRIVACY.md")

    # 检查确认状态
    eula_confirmed, eula_updated = _check_agreement_status(eula_hash, get_runtime_path("eula.confirmed"), "EULA_AGREE")
    privacy_confirmed, privacy_updated = _check_agreement_status(
        privacy_hash, get_runtime_path("privacy.confirmed"), "PRIVACY_AGREE"
    )

    # 早期返回：如果都已确认且未更新
    if eula_confirmed and privacy_confirmed:
        return

    # 如果有更新，需要重新确认
    if eula_updated or privacy_updated:
        _prompt_user_confirmation(eula_hash, privacy_hash)
        _save_confirmations(eula_updated, privacy_updated, eula_hash, privacy_hash)


def raw_main():
    # 利用 TZ 环境变量设定程序工作的时区
    if platform.system().lower() != "windows":
        time.tzset()  # type: ignore

    # 打印开源提示（防止倒卖）
    print_opensource_notice()

    check_eula()
    logger.info(t("startup.eula_privacy_checked"))

    easter_egg()

    # 返回MainSystem实例
    return MainSystem()


if __name__ == "__main__":
    exit_code = 0  # 用于记录程序最终的退出状态
    try:
        # 获取MainSystem实例
        main_system = raw_main()

        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        set_main_loop(loop)

        # 初始化 WebSocket 日志推送
        from src.common.logger import initialize_ws_handler

        initialize_ws_handler(loop)

        try:
            # 执行初始化和任务调度
            loop.run_until_complete(main_system.initialize())
            # Schedule tasks returns a future that runs forever.
            # We can run console_input_loop concurrently.
            main_tasks = loop.create_task(main_system.schedule_tasks())
            loop.run_until_complete(main_tasks)

        except KeyboardInterrupt:
            try:
                logger.warning(t("startup.interrupt_received"))
            except KeyboardInterrupt:
                raise

            # 取消主任务
            if "main_tasks" in locals() and main_tasks and not main_tasks.done():
                main_tasks.cancel()
                try:
                    loop.run_until_complete(main_tasks)
                except asyncio.CancelledError:
                    pass

            # 执行优雅关闭
            if loop and not loop.is_closed():
                try:
                    loop.run_until_complete(graceful_shutdown())
                except KeyboardInterrupt:
                    _print_interrupt_exit_notice()
                except Exception as ge:
                    logger.error(t("startup.graceful_shutdown_error", error=ge))
        # 新增：检测外部请求关闭

    except SystemExit as e:
        # 捕获 SystemExit (例如 sys.exit()) 并保留退出代码
        if isinstance(e.code, int):
            exit_code = e.code
        else:
            exit_code = 1 if e.code else 0
        if exit_code == RESTART_EXIT_CODE:
            logger.info(t("startup.restart_signal_received"))

    except KeyboardInterrupt:
        _print_interrupt_exit_notice()
    except Exception as e:
        try:
            logger.error(t("startup.main_error", error=f"{str(e)} {str(traceback.format_exc())}"))
        except KeyboardInterrupt:
            _print_interrupt_exit_notice()
        exit_code = 1  # 标记发生错误
    finally:
        try:
            # 确保 loop 在任何情况下都尝试关闭（如果存在且未关闭）
            if "loop" in locals() and loop and not loop.is_closed():
                set_main_loop(None)
                loop.close()
                print(t("startup.event_loop_closed"))

            # 关闭日志系统，释放文件句柄
            try:
                shutdown_logging()
            except Exception as e:
                print(t("startup.logging_shutdown_error", error=e))

            print(t("startup.prepare_exit"))
        except KeyboardInterrupt:
            _print_interrupt_exit_notice()

        # 使用 os._exit() 强制退出，避免被阻塞
        # 由于已经在 graceful_shutdown() 中完成了所有清理工作，这是安全的
        os._exit(exit_code)
