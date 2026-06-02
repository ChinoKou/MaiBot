"""插件 Python 依赖流水线。

负责在 Host 侧统一完成以下工作：
1. 扫描插件 Manifest；
2. 检测插件与主程序、插件与插件之间的 Python 依赖冲突；
3. 为可加载插件自动安装缺失的 Python 依赖；
4. 产出最终的拒绝加载列表，供运行时使用。
"""

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import asyncio
import shutil
import subprocess
import sys

from packaging.utils import canonicalize_name

from src.common.logger import get_logger
from src.common.process_launcher import PLUGIN_PIP_INSTALL_PROCESS_ARG, build_self_launch_command
from src.common.runtime_paths import get_plugin_dependency_dir, is_frozen_app
from src.plugin_runtime.runner.manifest_validator import ManifestValidator, PluginManifest


logger = get_logger("plugin_runtime.dependency_pipeline")


@dataclass(frozen=True)
class PackageDependencyUsage:
    """记录单个插件对某个 Python 包的依赖声明。"""

    package_name: str
    plugin_id: str
    version_spec: str


@dataclass(frozen=True)
class PluginPackageRequirement:
    """表示一个需要安装到插件外置依赖目录的 Python 包需求。"""

    package_name: str
    plugin_id: str
    requirement_text: str
    version_spec: str
    target_dir: Path


@dataclass(frozen=True)
class DependencyPipelinePlan:
    """表示一次依赖分析后得到的计划。"""

    blocked_plugin_reasons: Dict[str, str]
    install_requirements: Tuple[PluginPackageRequirement, ...]


@dataclass(frozen=True)
class DependencyPipelineResult:
    """表示一次依赖流水线执行后的结果。"""

    blocked_plugin_reasons: Dict[str, str]
    environment_changed: bool
    install_requirements: Tuple[PluginPackageRequirement, ...]


@dataclass(frozen=True)
class DependencyInstallResult:
    """表示自动安装插件 Python 依赖后的结果。"""

    succeeded: bool
    environment_changed: bool
    failed_plugin_ids: Tuple[str, ...] = ()
    error_message: str = ""


class PluginDependencyPipeline:
    """插件依赖流水线。

    该类不负责插件启停，只负责对插件目录进行依赖分析，并在必要时
    使用 ``uv`` 为可加载插件补齐缺失的 Python 依赖。
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        """初始化依赖流水线。

        Args:
            project_root: 项目根目录；留空时自动推断。
        """

        self._project_root: Path = project_root or Path(__file__).resolve().parents[2]
        self._manifest_validator: ManifestValidator = ManifestValidator(
            project_root=self._project_root,
            validate_python_package_dependencies=False,
        )

    async def execute(self, plugin_dirs: Iterable[Path]) -> DependencyPipelineResult:
        """执行完整的依赖分析与自动安装流程。

        Args:
            plugin_dirs: 需要扫描的插件根目录集合。

        Returns:
            DependencyPipelineResult: 最终的阻止加载结果与环境变更状态。
        """

        plan = self.build_plan(plugin_dirs)
        if not plan.install_requirements:
            return DependencyPipelineResult(
                blocked_plugin_reasons=dict(plan.blocked_plugin_reasons),
                environment_changed=False,
                install_requirements=plan.install_requirements,
            )

        install_result = await self._install_requirements(plan.install_requirements)
        if install_result.succeeded:
            return DependencyPipelineResult(
                blocked_plugin_reasons=dict(plan.blocked_plugin_reasons),
                environment_changed=install_result.environment_changed,
                install_requirements=plan.install_requirements,
            )

        blocked_plugin_reasons = dict(plan.blocked_plugin_reasons)
        for plugin_id in install_result.failed_plugin_ids:
            self._append_block_reason(
                blocked_plugin_reasons,
                plugin_id,
                f"自动安装 Python 依赖失败: {install_result.error_message}",
            )

        return DependencyPipelineResult(
            blocked_plugin_reasons=blocked_plugin_reasons,
            environment_changed=install_result.environment_changed,
            install_requirements=plan.install_requirements,
        )

    def build_plan(self, plugin_dirs: Iterable[Path]) -> DependencyPipelinePlan:
        """构建依赖分析计划。

        Args:
            plugin_dirs: 需要扫描的插件根目录集合。

        Returns:
            DependencyPipelinePlan: 分析后的阻止加载列表与安装计划。
        """

        manifests = self._collect_manifests(plugin_dirs)
        blocked_plugin_reasons = self._detect_host_conflicts(manifests)
        plugin_conflict_reasons = self._detect_plugin_conflicts(manifests, blocked_plugin_reasons)
        for plugin_id, reason in plugin_conflict_reasons.items():
            self._append_block_reason(blocked_plugin_reasons, plugin_id, reason)

        install_requirements = self._build_install_requirements(manifests, blocked_plugin_reasons)
        return DependencyPipelinePlan(
            blocked_plugin_reasons=blocked_plugin_reasons,
            install_requirements=install_requirements,
        )

    def _collect_manifests(self, plugin_dirs: Iterable[Path]) -> Dict[str, PluginManifest]:
        """收集所有可成功解析的插件 Manifest。

        Args:
            plugin_dirs: 需要扫描的插件根目录集合。

        Returns:
            Dict[str, PluginManifest]: 以插件 ID 为键的 Manifest 映射。
        """

        manifests: Dict[str, PluginManifest] = {}
        for _plugin_path, manifest in self._manifest_validator.iter_plugin_manifests(plugin_dirs):
            manifests[manifest.id] = manifest
        return manifests

    def _detect_host_conflicts(self, manifests: Dict[str, PluginManifest]) -> Dict[str, str]:
        """检测插件与主程序依赖之间的冲突。

        Args:
            manifests: 当前已解析到的插件 Manifest 映射。

        Returns:
            Dict[str, str]: 需要被阻止加载的插件及原因。
        """

        host_requirements = self._manifest_validator.load_host_dependency_requirements()
        blocked_plugin_reasons: Dict[str, str] = {}

        for manifest in manifests.values():
            for dependency in manifest.python_package_dependencies:
                package_specifier = self._manifest_validator.build_specifier_set(dependency.version_spec)
                if package_specifier is None:
                    self._append_block_reason(
                        blocked_plugin_reasons,
                        manifest.id,
                        f"Python 包依赖声明无效: {dependency.name}{dependency.version_spec}",
                    )
                    continue

                normalized_package_name = canonicalize_name(dependency.name)
                host_requirement = host_requirements.get(normalized_package_name)
                if host_requirement is None:
                    continue

                if self._manifest_validator.requirements_may_overlap(
                    host_requirement.specifier,
                    package_specifier,
                ):
                    continue

                host_specifier_text = str(host_requirement.specifier or "") or "任意版本"
                self._append_block_reason(
                    blocked_plugin_reasons,
                    manifest.id,
                    (
                        f"Python 包依赖与主程序冲突: {dependency.name} 需要 "
                        f"{dependency.version_spec}，主程序约束为 {host_specifier_text}"
                    ),
                )

        return blocked_plugin_reasons

    def _detect_plugin_conflicts(
        self,
        manifests: Dict[str, PluginManifest],
        blocked_plugin_reasons: Dict[str, str],
    ) -> Dict[str, str]:
        """检测插件之间的 Python 依赖冲突。

        Args:
            manifests: 当前已解析到的插件 Manifest 映射。
            blocked_plugin_reasons: 已经因为其他原因被阻止加载的插件。

        Returns:
            Dict[str, str]: 新增的插件冲突原因映射。
        """

        blocked_by_plugin_conflicts: Dict[str, str] = {}
        dependency_usages = self._collect_package_usages(manifests, blocked_plugin_reasons)

        for _package_name, usages in dependency_usages.items():
            display_package_name = usages[0].package_name
            for index, left_usage in enumerate(usages):
                for right_usage in usages[index + 1 :]:
                    left_specifier = self._manifest_validator.build_specifier_set(left_usage.version_spec)
                    right_specifier = self._manifest_validator.build_specifier_set(right_usage.version_spec)
                    if left_specifier is None or right_specifier is None:
                        continue

                    if self._manifest_validator.requirements_may_overlap(left_specifier, right_specifier):
                        continue

                    left_reason = (
                        f"Python 包依赖冲突: 与插件 {right_usage.plugin_id} 在 {display_package_name} 上的约束不兼容 "
                        f"({left_usage.version_spec} vs {right_usage.version_spec})"
                    )
                    right_reason = (
                        f"Python 包依赖冲突: 与插件 {left_usage.plugin_id} 在 {display_package_name} 上的约束不兼容 "
                        f"({right_usage.version_spec} vs {left_usage.version_spec})"
                    )
                    self._append_block_reason(blocked_by_plugin_conflicts, left_usage.plugin_id, left_reason)
                    self._append_block_reason(blocked_by_plugin_conflicts, right_usage.plugin_id, right_reason)

        return blocked_by_plugin_conflicts

    def _collect_package_usages(
        self,
        manifests: Dict[str, PluginManifest],
        blocked_plugin_reasons: Dict[str, str],
    ) -> Dict[str, List[PackageDependencyUsage]]:
        """收集所有未被阻止加载插件的包依赖声明。

        Args:
            manifests: 当前已解析到的插件 Manifest 映射。
            blocked_plugin_reasons: 已经被阻止加载的插件及原因。

        Returns:
            Dict[str, List[PackageDependencyUsage]]: 按规范化包名分组后的依赖声明。
        """

        dependency_usages: Dict[str, List[PackageDependencyUsage]] = {}
        for manifest in manifests.values():
            if manifest.id in blocked_plugin_reasons:
                continue

            for dependency in manifest.python_package_dependencies:
                normalized_package_name = canonicalize_name(dependency.name)
                dependency_usages.setdefault(normalized_package_name, []).append(
                    PackageDependencyUsage(
                        package_name=dependency.name,
                        plugin_id=manifest.id,
                        version_spec=dependency.version_spec,
                    )
                )

        return dependency_usages

    def _build_install_requirements(
        self,
        manifests: Dict[str, PluginManifest],
        blocked_plugin_reasons: Dict[str, str],
    ) -> Tuple[PluginPackageRequirement, ...]:
        """构建需要安装到插件外置目录的 Python 包需求列表。

        Args:
            manifests: 当前已解析到的插件 Manifest 映射。
            blocked_plugin_reasons: 已经被阻止加载的插件及原因。

        Returns:
            Tuple[PluginPackageRequirement, ...]: 需要安装或调整版本的依赖列表。
        """

        install_requirements: List[PluginPackageRequirement] = []
        dependency_usages = self._collect_package_usages(manifests, blocked_plugin_reasons)

        for usages in dependency_usages.values():
            merged_specifier_text = self._merge_specifier_texts([usage.version_spec for usage in usages])
            package_name = usages[0].package_name

            for usage in usages:
                plugin_dependency_version = self._get_plugin_dependency_version(usage.plugin_id, package_name)
                if plugin_dependency_version is not None:
                    if self._manifest_validator.version_matches_specifier(
                        plugin_dependency_version,
                        merged_specifier_text,
                    ):
                        continue
                else:
                    installed_version = self._manifest_validator.get_installed_package_version(package_name)
                    if installed_version is not None and self._manifest_validator.version_matches_specifier(
                        installed_version,
                        merged_specifier_text,
                    ):
                        continue
                install_requirements.append(
                    PluginPackageRequirement(
                        package_name=package_name,
                        plugin_id=usage.plugin_id,
                        requirement_text=f"{package_name}{merged_specifier_text}",
                        version_spec=merged_specifier_text,
                        target_dir=get_plugin_dependency_dir(usage.plugin_id).resolve(),
                    )
                )

        return tuple(
            sorted(
                install_requirements,
                key=lambda requirement: (requirement.plugin_id, canonicalize_name(requirement.package_name)),
            )
        )

    def _get_plugin_dependency_version(self, plugin_id: str, package_name: str) -> Optional[str]:
        """读取插件外置依赖目录中指定包的版本。"""

        target_dir = get_plugin_dependency_dir(plugin_id).resolve()
        if not target_dir.is_dir():
            return None

        try:
            return self._get_distribution_version_from_path(package_name, target_dir)
        except Exception as exc:
            logger.warning(f"读取插件 {plugin_id} 外置依赖版本失败: {package_name}: {exc}")
            return None

    def _plugin_dependency_satisfies(self, plugin_id: str, package_name: str, version_spec: str) -> bool:
        """判断插件外置依赖目录中是否已有满足要求的包。"""

        installed_version = self._get_plugin_dependency_version(plugin_id, package_name)
        if installed_version is None:
            return False
        return self._manifest_validator.version_matches_specifier(installed_version, version_spec)

    @staticmethod
    def _get_distribution_version_from_path(package_name: str, target_dir: Path) -> Optional[str]:
        """从指定 site-packages 目录读取包版本。"""

        normalized_package_name = canonicalize_name(package_name)
        for distribution in importlib_metadata.distributions(path=[str(target_dir)]):
            distribution_name = distribution.metadata.get("Name", "")
            if canonicalize_name(distribution_name) == normalized_package_name:
                return distribution.version
        return None

    @staticmethod
    def _merge_specifier_texts(specifier_texts: Sequence[str]) -> str:
        """合并多个版本约束文本。

        Args:
            specifier_texts: 需要合并的版本约束文本序列。

        Returns:
            str: 合并后的版本约束文本。
        """

        merged_parts: List[str] = []
        for specifier_text in specifier_texts:
            for part in str(specifier_text or "").split(","):
                normalized_part = part.strip()
                if not normalized_part or normalized_part in merged_parts:
                    continue
                merged_parts.append(normalized_part)
        return f"{','.join(merged_parts)}" if merged_parts else ""

    async def _install_requirements(self, requirements: Sequence[PluginPackageRequirement]) -> DependencyInstallResult:
        """安装指定的 Python 包需求列表。"""

        if not requirements:
            return DependencyInstallResult(succeeded=True, environment_changed=False)

        requirements_by_target: Dict[Path, List[PluginPackageRequirement]] = {}
        for requirement in requirements:
            requirements_by_target.setdefault(requirement.target_dir, []).append(requirement)

        environment_changed = False
        failed_plugin_ids: List[str] = []
        error_messages: List[str] = []
        for target_dir, target_requirements in sorted(requirements_by_target.items(), key=lambda item: str(item[0])):
            requirement_texts = [requirement.requirement_text for requirement in target_requirements]
            plugin_ids = sorted({requirement.plugin_id for requirement in target_requirements})
            target_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"开始自动安装插件 Python 依赖: {', '.join(requirement_texts)} -> {target_dir} "
                f"(plugins={', '.join(plugin_ids)})"
            )
            command = self._build_install_command(requirement_texts, target_dir)

            try:
                completed_process = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    capture_output=True,
                    check=False,
                    cwd=self._project_root,
                    text=True,
                )
            except Exception as exc:
                failed_plugin_ids.extend(plugin_ids)
                error_messages.append(f"{', '.join(plugin_ids)}: {exc}")
                continue

            if completed_process.returncode == 0:
                environment_changed = True
                logger.info(f"插件 Python 依赖自动安装完成: {target_dir}")
                continue

            output = self._summarize_install_error(completed_process.stdout, completed_process.stderr)
            error_message = output or f"命令执行失败，退出码 {completed_process.returncode}"
            failed_plugin_ids.extend(plugin_ids)
            error_messages.append(f"{', '.join(plugin_ids)}: {error_message}")
            logger.debug(
                "插件 Python 依赖安装命令失败: returncode=%s, command=%s, stdout=%r, stderr=%r",
                completed_process.returncode,
                command,
                completed_process.stdout,
                completed_process.stderr,
            )

        if failed_plugin_ids:
            return DependencyInstallResult(
                succeeded=False,
                environment_changed=environment_changed,
                failed_plugin_ids=tuple(sorted(set(failed_plugin_ids))),
                error_message="; ".join(error_messages),
            )

        return DependencyInstallResult(succeeded=True, environment_changed=environment_changed)

    @staticmethod
    def _build_install_command(requirement_texts: Sequence[str], target_dir: Path) -> List[str]:
        """构造依赖安装命令。

        frozen 模式下复用当前 PyInstaller 运行时，通过内部 pip 子命令安装到外置插件依赖目录。
        源码模式仍优先使用 uv，否则回退到当前 Python 的 pip。
        """

        if is_frozen_app():
            return build_self_launch_command(
                PLUGIN_PIP_INSTALL_PROCESS_ARG,
                [
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-warn-script-location",
                    "--target",
                    str(target_dir),
                    "--upgrade",
                    *requirement_texts,
                ],
            )

        if shutil.which("uv"):
            return [
                "uv",
                "pip",
                "install",
                "--python",
                str(Path(sys.executable).resolve()),
                "--target",
                str(target_dir),
                "--upgrade",
                *requirement_texts,
            ]
        return [
            str(Path(sys.executable).resolve()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-warn-script-location",
            "--target",
            str(target_dir),
            "--upgrade",
            *requirement_texts,
        ]

    @staticmethod
    def _summarize_install_error(stdout: str, stderr: str) -> str:
        """提炼安装失败输出。

        Args:
            stdout: 标准输出内容。
            stderr: 标准错误内容。

        Returns:
            str: 简短的错误摘要。
        """

        selected_output = stderr.strip() if stderr and stderr.strip() else stdout.strip()
        if not selected_output:
            return ""
        lines = [line.strip() for line in selected_output.splitlines() if line.strip()]
        return " | ".join(lines[-10:])

    @staticmethod
    def _append_block_reason(
        blocked_plugin_reasons: Dict[str, str],
        plugin_id: str,
        reason: str,
    ) -> None:
        """向阻止加载映射中追加原因。

        Args:
            blocked_plugin_reasons: 待更新的阻止加载映射。
            plugin_id: 目标插件 ID。
            reason: 需要追加的原因文本。
        """

        existing_reason = blocked_plugin_reasons.get(plugin_id)
        if existing_reason is None:
            blocked_plugin_reasons[plugin_id] = reason
            return

        existing_parts = [part.strip() for part in existing_reason.split("；") if part.strip()]
        if reason in existing_parts:
            return
        blocked_plugin_reasons[plugin_id] = f"{existing_reason}；{reason}"
