"""qzone_shuoshuo 插件依赖管理。 

提供插件级别的依赖检测与自动安装能力。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


@dataclass(frozen=True)
class DependencySpec:
    """依赖规格。"""

    package_name: str
    module_name: str


@dataclass(frozen=True)
class InstallResult:
    """安装结果。"""

    success: bool
    command: list[str]
    return_code: int
    output: str


class DependencyManager:
    """插件依赖管理器。"""

    DEFAULT_DEPENDENCIES: tuple[DependencySpec, ...] = (
        DependencySpec(package_name="httpx", module_name="httpx"),
        DependencySpec(package_name="orjson", module_name="orjson"),
        DependencySpec(package_name="aiofiles", module_name="aiofiles"),
    )

    def __init__(self, dependencies: tuple[DependencySpec, ...] | None = None) -> None:
        self._dependencies = dependencies or self.DEFAULT_DEPENDENCIES

    def find_missing_packages(self) -> list[str]:
        """检测缺失依赖包名列表。"""
        missing: list[str] = []
        for dep in self._dependencies:
            if importlib.util.find_spec(dep.module_name) is None:
                missing.append(dep.package_name)
        return missing

    async def ensure_dependencies(
        self,
        *,
        auto_install: bool,
        installer: str = "auto",
        timeout_seconds: int = 180,
    ) -> bool:
        """确保依赖可用。"""
        missing = self.find_missing_packages()
        if not missing:
            logger.debug("[依赖检查] qzone_shuoshuo 依赖已满足")
            return True

        logger.warning(f"[依赖检查] 检测到缺失依赖: {', '.join(missing)}")
        if not auto_install:
            return False

        command = self._build_install_command(installer=installer, packages=missing)
        result = await self._run_install_command(command=command, timeout_seconds=timeout_seconds)

        if not result.success:
            logger.error(
                "[依赖安装] 自动安装失败 "
                f"(code={result.return_code}): {' '.join(result.command)}\n{result.output}"
            )
            return False

        logger.info(f"[依赖安装] 自动安装成功: {', '.join(missing)}")

        remaining = self.find_missing_packages()
        if remaining:
            logger.error(f"[依赖检查] 安装后仍缺失依赖: {', '.join(remaining)}")
            return False

        return True

    def _build_install_command(self, *, installer: str, packages: list[str]) -> list[str]:
        """根据安装器策略构造安装命令。"""
        normalized = (installer or "auto").strip().lower()
        if normalized not in {"auto", "uv", "pip"}:
            normalized = "auto"

        if normalized in {"auto", "uv"} and shutil.which("uv"):
            return ["uv", "pip", "install", *packages]

        return [sys.executable, "-m", "pip", "install", *packages]

    async def _run_install_command(self, *, command: list[str], timeout_seconds: int) -> InstallResult:
        """执行安装命令。"""

        def _run() -> InstallResult:
            try:
                completed = subprocess.run(  # noqa: S603
                    command,
                    capture_output=True,
                    text=True,
                    shell=False,  # noqa: S602
                    check=False,
                )
                output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
                return InstallResult(
                    success=completed.returncode == 0,
                    command=command,
                    return_code=completed.returncode,
                    output=output.strip(),
                )
            except Exception as exc:
                return InstallResult(
                    success=False,
                    command=command,
                    return_code=-1,
                    output=str(exc),
                )

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=max(1, int(timeout_seconds)))
        except asyncio.TimeoutError:
            return InstallResult(
                success=False,
                command=command,
                return_code=-2,
                output=f"安装超时（>{timeout_seconds}s）",
            )


def parse_installer_value(value: object, default: str = "auto") -> str:
    """解析安装器配置值。"""
    text = str(value or default).strip().lower()
    return text if text in {"auto", "uv", "pip"} else default
