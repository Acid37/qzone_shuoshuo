"""Cookie 管理器。

负责从本地文件加载、通过适配器获取、以及验证 Cookie 有效性。
Cookie 可能会刷新失效，因此需要定期检查。
"""

import json
from pathlib import Path

import aiofiles

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")

# QQ空间 Cookie 失效的错误码
COOKIE_EXPIRED_CODE = -3000


class CookieManager:
    """Cookie 管理器"""

    def __init__(self, data_dir: Path) -> None:
        """初始化 Cookie 管理器

        Args:
            data_dir: 数据存储目录
        """
        self.data_dir = data_dir
        self.cookies_dir = self.data_dir / "cookies"
        self.cookies_dir.mkdir(parents=True, exist_ok=True)

    def _get_cookie_path(self, qq: str) -> Path:
        """获取 Cookie 文件路径"""
        return self.cookies_dir / f"cookies-{qq}.json"

    async def load_cookies(self, qq: str) -> dict[str, str] | None:
        """从本地文件加载 Cookie

        Args:
            qq: QQ号

        Returns:
            Cookie 字典或 None
        """
        path = self._get_cookie_path(qq)
        if not path.exists():
            return None

        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.error(f"加载 Cookie 失败 {qq}: {e}")
            return None

    async def save_cookies(self, qq: str, cookies: dict[str, str]) -> None:
        """保存 Cookie 到本地文件

        Args:
            qq: QQ号
            cookies: Cookie 字典
        """
        path = self._get_cookie_path(qq)
        try:
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                content = json.dumps(cookies, ensure_ascii=False, indent=2)
                await f.write(content)
            logger.info(f"Cookie 已保存: {path}")
        except Exception as e:
            logger.error(f"保存 Cookie 失败 {qq}: {e}")

    async def fetch_cookies_from_adapter(self, adapter_sign: str) -> dict[str, str] | None:
        """从适配器获取 Cookie

        Args:
            adapter_sign: 适配器签名

        Returns:
            Cookie 字典或 None
        """
        try:
            result = await adapter_api.send_adapter_command(
                adapter_sign=adapter_sign,
                command_name="get_cookies",
                command_data={"domain": "user.qzone.qq.com"},
                timeout=20.0,
            )

            if result.get("status") == "ok":
                data = result.get("data", {})
                cookie_str = data.get("cookies", "")
                if cookie_str:
                    return self._parse_cookie_str(cookie_str)
            else:
                logger.warning(f"适配器返回错误: {result}")
        except Exception as e:
            logger.error(f"从适配器获取 Cookie 失败: {e}")

        return None

    def _parse_cookie_str(self, cookie_str: str) -> dict[str, str]:
        """解析 Cookie 字符串

        Args:
            cookie_str: Cookie 字符串 "k=v; k2=v2"

        Returns:
            Cookie 字典
        """
        cookies = {}
        for item in cookie_str.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                cookies[k] = v
        return cookies

    async def get_cookies(self, qq: str, adapter_sign: str = "") -> dict[str, str] | None:
        """获取 Cookie（优先本地，失败则尝试适配器）

        Args:
            qq: QQ号
            adapter_sign: 适配器签名（如果需要从适配器获取）

        Returns:
            Cookie 字典或 None
        """
        # 1. 尝试本地加载
        cookies = await self.load_cookies(qq)
        if cookies:
            return cookies

        # 2. 尝试从适配器获取
        if adapter_sign:
            logger.info(f"本地无 Cookie，尝试从适配器 {adapter_sign} 获取...")
            cookies = await self.fetch_cookies_from_adapter(adapter_sign)
            if cookies:
                await self.save_cookies(qq, cookies)
                return cookies

        return None

    async def delete_cookies(self, qq: str) -> bool:
        """删除指定QQ号的Cookie文件

        Args:
            qq: QQ号

        Returns:
            是否删除成功
        """
        path = self._get_cookie_path(qq)
        try:
            if path.exists():
                path.unlink()
                logger.info(f"Cookie 已删除: {path}")
                return True
            return False
        except Exception as e:
            logger.error(f"删除 Cookie 失败 {qq}: {e}")
            return False

    async def refresh_cookies(self, qq: str, adapter_sign: str) -> dict[str, str] | None:
        """强制从适配器刷新 Cookie（用于 Cookie 失效时）

        Args:
            qq: QQ号
            adapter_sign: 适配器签名

        Returns:
            新 Cookie 字典或 None
        """
        logger.info(f"[Cookie刷新] 正在从适配器刷新 QQ:{qq} 的 Cookie...")
        cookies = await self.fetch_cookies_from_adapter(adapter_sign)
        if cookies:
            await self.save_cookies(qq, cookies)
            logger.info("[Cookie刷新] 成功获取新 Cookie")
            return cookies
        logger.error("[Cookie刷新] 从适配器获取新 Cookie 失败")
        return None
