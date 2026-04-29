"""Qzone 核心服务层。

封装QQ空间说说相关的业务逻辑、HTTP API调用以及 Cookie 状态管理。
重构后按功能域拆分为子模块，service.py 仅保留门面入口。
"""

from .types import Result, ResultStatus
from .service import QzoneService
from .cookie_manager import CookieManager
from .state_manager import StateManager
from .http_client import QzoneHttpClient
from .feed_ops import FeedOperations
from .interaction import InteractionOps
from .monitor import MonitorScheduler
from .ai_prompts import AIPromptBuilder
from .feed_parser import (
    extract_text_from_feed_html,
    extract_image_urls_from_feed_html,
    normalize_image_url,
    parse_feed_html_item,
)

__all__ = [
    "QzoneService",
    "CookieManager",
    "Result",
    "ResultStatus",
    "StateManager",
    "QzoneHttpClient",
    "FeedOperations",
    "InteractionOps",
    "MonitorScheduler",
    "AIPromptBuilder",
    "extract_text_from_feed_html",
    "extract_image_urls_from_feed_html",
    "normalize_image_url",
    "parse_feed_html_item",
]
