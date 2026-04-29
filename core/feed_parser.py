"""Qzone 动态 HTML 解析模块。

负责从 QQ 空间好友动态 HTML 中提取文本内容、图片 URL、
评论列表、点赞状态等结构化数据。
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

try:
    from bs4 import BeautifulSoup, Tag
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    BeautifulSoup = None  # type: ignore
    Tag = None  # type: ignore

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


def extract_text_from_feed_html(html_content: str) -> str:
    """从好友动态 HTML 片段中提取纯文本内容。

    使用 bs4.BeautifulSoup 精确解析，优先提取 div.f-info 中的文本。
    """
    text = str(html_content or "")
    if not text:
        return ""

    # 优先使用 bs4 精确解析
    if HAS_BS4 and BeautifulSoup:
        try:
            soup = BeautifulSoup(text, "html.parser")
            text_div = soup.find("div", class_="f-info")
            if isinstance(text_div, Tag):
                return text_div.get_text(strip=True)
            return soup.get_text(strip=True)
        except Exception as e:
            logger.debug(f"[bs4解析HTML] 失败: {e}，回退到正则方式")

    # 回退到正则方式
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = " ".join(text.split())
    return text.strip()


def normalize_image_url(url: str) -> str:
    """规范化图片 URL，补全协议头并处理特殊字符。

    处理 QQ 空间常见的 URL 格式：
    - //qpic.cn/xxx → https://qpic.cn/xxx
    - /path/xxx → https://qzonestyle.gtimg.cn/path/xxx
    - qpic.cn/xxx → https://qpic.cn/xxx
    - 处理 HTML 实体编码（如 &amp; → &）
    - 处理 URL 中的特殊字符（如 *）
    """
    import html

    url = url.strip()
    if not url:
        return ""

    # 解码 HTML 实体（如 &amp; → &）
    url = html.unescape(url)

    if url.startswith("//"):
        return f"https:{url}"

    if url.startswith("/"):
        return f"https://qzonestyle.gtimg.cn{url}"

    if not url.startswith(("http://", "https://")):
        if any(domain in url for domain in ["qpic.cn", "qlogo.cn", "gtimg.cn"]):
            return f"https://{url}"

    # 注意：保留 URL 中的特殊字符（如 *），让 httpx 自动处理编码
    # QQ 空间的图片 URL 常包含 *，这在技术上是不规范的，但服务器接受

    return url


def extract_image_urls_from_feed_html(html_content: str) -> list[str]:
    """从好友动态 HTML 片段中提取图片 URL 列表。

    使用 bs4.BeautifulSoup 精确提取，包括：
    - div.img-box 中的图片
    - div.video-img 中的视频封面
    排除 qzonestyle.gtimg.cn 静态资源。
    """
    text = str(html_content or "")
    if not text:
        return []

    urls: list[str] = []

    if HAS_BS4 and BeautifulSoup:
        try:
            soup = BeautifulSoup(text, "html.parser")

            img_box = soup.find("div", class_="img-box")
            if isinstance(img_box, Tag):
                for img in img_box.find_all("img"):
                    if isinstance(img, Tag):
                        src = img.get("src")
                        if src and isinstance(src, str) and "qzonestyle.gtimg.cn" not in src:
                            normalized = normalize_image_url(src)
                            if normalized and normalized not in urls:
                                urls.append(normalized)

            video_thumb = soup.select_one("div.video-img img")
            if isinstance(video_thumb, Tag) and "src" in video_thumb.attrs:
                src = video_thumb["src"]
                if src and isinstance(src, str) and "qzonestyle.gtimg.cn" not in src:
                    normalized = normalize_image_url(src)
                    if normalized and normalized not in urls:
                        urls.append(normalized)

            if urls:
                return urls
        except Exception as e:
            logger.debug(f"[bs4提取图片] 失败: {e}，回退到正则方式")

    matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
    for url in matches:
        cleaned = str(url).strip()
        if not cleaned:
            continue
        if "qzonestyle.gtimg.cn" in cleaned:
            continue
        normalized = normalize_image_url(cleaned)
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def parse_feed_html_item(html_content: str, owner_qq: str) -> dict[str, Any]:
    """从单条动态 HTML 中解析出点赞状态和评论列表。

    Returns:
        {"is_liked": bool, "comments": list[dict]}
    """
    is_liked = False
    comments: list[dict[str, Any]] = []

    if not (HAS_BS4 and BeautifulSoup and html_content):
        return {"is_liked": is_liked, "comments": comments}

    try:
        soup = BeautifulSoup(html_content, "html.parser")

        # 检测点赞状态
        like_btn = soup.find("a", class_="qz_like_btn_v3")
        if isinstance(like_btn, Tag) and like_btn.get("data-islike") == "1":
            is_liked = True

        # 提取评论列表
        comment_divs = soup.find_all("div", class_="f-single-comment")
        for comment_div in comment_divs:
            if not isinstance(comment_div, Tag):
                continue

            author_a = comment_div.find("a", class_="f-nick")
            content_span = comment_div.find("span", class_="f-re-con")

            if isinstance(author_a, Tag) and isinstance(content_span, Tag):
                comment_data = {
                    "qq_account": str(comment_div.get("data-uin", "")),
                    "nickname": author_a.get_text(strip=True),
                    "content": content_span.get_text(strip=True),
                    "comment_tid": comment_div.get("data-tid", ""),
                    "parent_tid": None,
                }
                comments.append(comment_data)

            # 提取该评论下的回复
            reply_divs = comment_div.find_all("div", class_="f-single-re")
            for reply_div in reply_divs:
                if not isinstance(reply_div, Tag):
                    continue

                reply_author_a = reply_div.find("a", class_="f-nick")
                reply_content_span = reply_div.find("span", class_="f-re-con")

                if isinstance(reply_author_a, Tag) and isinstance(reply_content_span, Tag):
                    reply_data = {
                        "qq_account": str(reply_div.get("data-uin", "")),
                        "nickname": reply_author_a.get_text(strip=True),
                        "content": reply_content_span.get_text(strip=True),
                        "comment_tid": reply_div.get("data-tid", ""),
                        "parent_tid": comment_div.get("data-tid", ""),
                    }
                    comments.append(reply_data)
    except Exception as e:
        logger.debug(f"[好友动态] bs4解析评论失败: {e}")

    return {"is_liked": is_liked, "comments": comments}