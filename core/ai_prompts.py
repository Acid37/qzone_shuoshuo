"""Qzone AI 提示词构建模块。

负责构建评论生成、回复生成、发布改写等场景的完整提示词，
包括人设注入、安全底线、图片上下文、发布历史去重等。
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .state_manager import StateManager

logger = get_logger("qzone_shuoshuo")

# 默认系统提示词
DEFAULT_COMMENT_SYSTEM_PROMPT = (
    "你正在QQ空间场景下生成评论。\n"
    "目标：像真人用户一样，给出自然、友善、贴合上下文的短评论。\n"
    "优先级：\n"
    "1) 先贴合语境，再追求文采；\n"
    "2) 输出只给评论正文，不要解释或前缀；\n"
    "3) 不编造输入外事实，不输出攻击性/敏感内容；\n"
    "4) 风格与人设一致，允许口语化；\n"
    "5) 若不适合评论，返回空字符串。"
)

DEFAULT_REPLY_SYSTEM_PROMPT = (
    "你正在QQ空间场景下回复他人评论。\n"
    "目标：给出自然、礼貌、有人味的回应，像真实社交互动。\n"
    "优先级：\n"
    "1) 紧贴对方评论语义，先回应再延展；\n"
    "2) 输出只给回复正文，不要解释或前缀；\n"
    "3) 不编造输入外事实，不输出攻击性/敏感内容；\n"
    "4) 风格与人设一致，允许轻松口语；\n"
    "5) 若无合适回复，返回空字符串。"
)

DEFAULT_PUBLISH_SYSTEM_PROMPT = (
    "你正在QQ空间场景下准备发布说说。\n"
    "目标：将输入内容改写为一条自然、友善、可公开展示的说说正文。\n"
    "优先级：\n"
    "1) 保留原意，不编造输入外事实；\n"
    "2) 语言自然，符合人设与表达风格；\n"
    "3) 输出只给说说正文，不要解释或前后缀；\n"
    "4) 避免攻击性、敏感或冒犯表达；\n"
    "5) 若输入不适合发布，返回空字符串。"
)

DEFAULT_COMMENT_FORBIDDEN = "禁止使用Emoji表情、@符号、敏感话题"


class AIPromptBuilder:
    """AI 提示词构建器。

    负责：
    - 从 core.toml 读取人设/风格配置
    - 构建评论/回复/发布改写的完整提示词
    - 调用 LLM 生成文本
    - 净化输出文本
    """

    def __init__(self, state: "StateManager") -> None:
        self._state = state

    # ---- 系统提示词 ----

    @staticmethod
    def get_builtin_system_prompt(base_field: str) -> str:
        """获取内置系统提示词。"""
        if base_field == "comment_system_prompt":
            return DEFAULT_COMMENT_SYSTEM_PROMPT
        if base_field == "reply_system_prompt":
            return DEFAULT_REPLY_SYSTEM_PROMPT
        if base_field == "publish_system_prompt":
            return DEFAULT_PUBLISH_SYSTEM_PROMPT
        return ""

    # ---- 人设获取 ----

    @staticmethod
    def get_persona_and_style() -> tuple[str, str]:
        """从 core.toml 获取人设与风格文本。"""
        persona_text = "保持友善、真诚、自然，有基本同理心。"
        style_text = "口语化、简洁、有温度，像真实好友在聊天。"

        try:
            from src.core.config import get_core_config

            personality = get_core_config().personality
            personality_core = str(getattr(personality, "personality_core", "") or "").strip()
            personality_side = str(getattr(personality, "personality_side", "") or "").strip()
            reply_style = str(getattr(personality, "reply_style", "") or "").strip()

            if personality_core and personality_side:
                persona_text = f"{personality_core}，{personality_side}"
            elif personality_core:
                persona_text = personality_core
            elif personality_side:
                persona_text = personality_side

            if reply_style:
                style_text = reply_style
        except Exception:
            pass

        return persona_text, style_text

    @staticmethod
    def get_persona_guardrails() -> tuple[str, list[str], list[str]]:
        """获取轻量人格补充信息（身份/安全底线/禁止行为）。"""
        identity_text = ""
        safety_guidelines: list[str] = []
        negative_behaviors: list[str] = []

        try:
            from src.core.config import get_core_config

            personality = get_core_config().personality
            identity_text = str(getattr(personality, "identity", "") or "").strip()
            raw_safety = [
                str(item).strip()
                for item in list(getattr(personality, "safety_guidelines", []) or [])
                if str(item).strip()
            ]
            raw_negative = [
                str(item).strip()
                for item in list(getattr(personality, "negative_behaviors", []) or [])
                if str(item).strip()
            ]

            safety_priority_keywords = ["危险", "违法", "暴力", "色情", "隐私", "诈骗", "骚扰", "敏感", "攻击"]
            negative_priority_keywords = ["禁止", "不得", "不能", "不", "隐私", "攻击", "诈骗", "违法", "冒犯", "威胁"]

            safety_guidelines = _compact_prompt_rules(
                raw_safety,
                max_items=6,
                max_chars=60,
                priority_keywords=safety_priority_keywords,
            )
            negative_behaviors = _compact_prompt_rules(
                raw_negative,
                max_items=6,
                max_chars=60,
                priority_keywords=negative_priority_keywords,
            )
        except Exception:
            pass

        return identity_text, safety_guidelines, negative_behaviors

    # ---- 评论生成 ----

    async def generate_comment_text(
        self, content: str, nickname: str, images: list[str] | None = None
    ) -> str | None:
        """生成评论文本（AI 驱动，无模板兜底）。"""
        system_prompt = self.get_builtin_system_prompt("comment_system_prompt")

        try:
            image_context = ""
            full_prompt = self._build_full_comment_prompt(content, nickname, image_context)
            if full_prompt:
                ai_comment = await self._call_llm(full_prompt, system_prompt)
                if ai_comment:
                    logger.debug(f"[AI评论] AI生成评论成功: {ai_comment}")
                    return ai_comment
        except Exception as e:
            logger.warning(f"[AI评论] 完整提示词生成失败: {e}")

        logger.warning("[AI评论] 模型未生成有效评论，按策略不使用模板兜底，已跳过")
        return None

    def _build_full_comment_prompt(self, content: str, nickname: str, image_context: str = "") -> str:
        """构建评论的上下文输入提示词。"""
        forbidden = DEFAULT_COMMENT_FORBIDDEN
        persona_text, style_text = self.get_persona_and_style()
        identity_text, safety_guidelines, negative_behaviors = self.get_persona_guardrails()

        safety_block = "\n".join([f"- {item}" for item in safety_guidelines if str(item).strip()])
        negative_block = "\n".join([f"- {item}" for item in negative_behaviors if str(item).strip()])
        identity_section = f"\n- 身份：{identity_text}" if identity_text else ""
        safety_section = f"\n\n# 安全与互动底线\n\n{safety_block}" if safety_block else ""
        negative_section = f"\n\n# 禁止行为\n\n{negative_block}" if negative_block else ""
        relation_hint = f"你与{nickname}是 QQ 空间好友关系，互动应保持自然、友善、不冒犯。"

        current_time = datetime.datetime.now().strftime("%m月%d日 %H:%M")
        return f"""# 平台说明

QQ空间是中文社交平台，用户通过"说说"记录生活，好友可以点赞、评论和回复。

# 人设定义

{persona_text}
{identity_section}

# 语言风格

{style_text}

{safety_section}
{negative_section}

# 当前情景

- 时间：{current_time}
- 场景：你正在浏览 QQ 空间好友动态并准备互动
- 目标对象：{nickname}
- 关系提示：{relation_hint}
- 对方说说内容：{content[:500] if content else "[无文字内容]"}

{image_context if image_context else ""}

# 行为规范

1. 优先贴合语境与上下文，像真人自然互动。
2. 不说教、不端着，不编造输入外事实。
3. 允许口语化，但避免攻击性、敏感或冒犯表达。

# 额外约束

{forbidden}

# 接下来你说

请直接说一句自然、得体、有互动感的评论正文。

# 输出要求（最高优先级）

你的输出必须且只能是一条评论正文本身。
单行输出，不超过35字，禁止出现 @。

绝对禁止输出：
- 思考过程（如"我应该…/让我想想…"）
- 草稿或修改说明（如"版本1/修改后"）
- 字数统计、多版本备选
- 任何前后缀说明（如"评论内容："）
- 换行符（评论需单行）

若不适合评论，请返回空字符串。"""

    # ---- 回复生成 ----

    async def generate_comment_reply(
        self,
        story_content: str,
        comment_content: str,
        commenter_name: str,
        commenter_qq: str | None,
        images: list[str],
        story_time: str | None = None,
        comment_time: str | None = None,
    ) -> str | None:
        """生成评论回复内容（调用 AI）。"""
        forbidden = DEFAULT_COMMENT_FORBIDDEN
        reply_system_prompt = self.get_builtin_system_prompt("reply_system_prompt")

        image_context = ""
        prompt = self._build_full_reply_prompt(
            story_content=story_content,
            comment_content=comment_content,
            commenter_name=commenter_name,
            commenter_qq=commenter_qq,
            story_time=story_time,
            comment_time=comment_time,
            image_context=image_context,
            forbidden=forbidden,
        )
        if prompt:
            try:
                text = await self._call_llm(prompt, reply_system_prompt)
                if text:
                    return text
            except Exception as e:
                logger.error(f"[AI回复生成] 完整提示词调用异常: {e}")

        return None

    def _build_full_reply_prompt(
        self,
        story_content: str,
        comment_content: str,
        commenter_name: str,
        commenter_qq: str | None,
        story_time: str | None,
        comment_time: str | None,
        image_context: str,
        forbidden: str,
    ) -> str:
        """构建回复评论的上下文输入提示词。"""
        persona_text, style_text = self.get_persona_and_style()
        identity_text, safety_guidelines, negative_behaviors = self.get_persona_guardrails()

        safety_block = "\n".join([f"- {item}" for item in safety_guidelines if str(item).strip()])
        negative_block = "\n".join([f"- {item}" for item in negative_behaviors if str(item).strip()])
        identity_section = f"\n- 身份：{identity_text}" if identity_text else ""
        safety_section = f"\n\n# 安全与互动底线\n\n{safety_block}" if safety_block else ""
        negative_section = f"\n\n# 禁止行为\n\n{negative_block}" if negative_block else ""

        relation_hint = f"你与{commenter_name}是 QQ 空间好友关系，互动应保持自然、礼貌、不冒犯。"
        if commenter_qq:
            relation_hint += f"（对方QQ: {commenter_qq}）"

        timeline_lines = ["- 当前时间：实时对话阶段"]
        if story_time:
            timeline_lines.append(f"- 说说发布时间：{story_time}")
        if comment_time:
            timeline_lines.append(f"- 评论时间：{comment_time}")
        timeline_block = "\n".join(timeline_lines)

        return f"""# 平台说明

QQ空间是中文社交平台，用户通过"说说"记录日常，好友会进行点赞、评论与回复互动。

# 人设定义

{persona_text}
{identity_section}

# 语言风格

{style_text}

{safety_section}
{negative_section}

# 当前情景

{timeline_block}

- 关系提示：{relation_hint}

- 你的说说内容：{story_content}
- 评论者：{commenter_name}
- 对方评论：{comment_content}

{image_context if image_context else ""}

# 额外约束

{forbidden}

# 接下来你说

请直接生成一条自然、礼貌、有人味的回复正文，贴合当前说说和评论语义。

# 输出要求（最高优先级）

你的输出必须且只能是一条回复正文本身。

绝对禁止输出：
- 思考过程（如"我应该…/让我想想…"）
- 草稿或修改说明（如"版本1/修改后"）
- 字数统计、多版本备选
- 任何前后缀说明（如"回复内容："）
- 换行符（回复需单行）

若无合适回复，请返回空字符串。"""

    # ---- 发布改写 ----

    async def rewrite_publish_content(self, content: str) -> str:
        """发布说说前按人设/风格进行改写。"""
        raw_text = str(content or "").strip()
        if not raw_text:
            return ""

        persona_text, style_text = self.get_persona_and_style()
        system_prompt = self.get_builtin_system_prompt("publish_system_prompt")
        history_block = self._state.build_publish_history_block(limit=5)
        history_text = f"\n{history_block}\n" if history_block else ""
        user_prompt = (
            "请基于以下信息将原始内容改写成一条适合发布到 QQ 空间的说说正文。\n\n"
            f"人设：{persona_text}\n"
            f"风格：{style_text}\n"
            f"{history_text}"
            f"原始内容：{raw_text}\n\n"
            "输出要求：\n"
            "- 仅输出最终说说正文\n"
            "- 不要解释、不要前缀\n"
            "- 保留原意，不编造事实\n"
            "- 避免与最近发布内容语义重复\n"
            "- 长度建议 28~80 字\n"
            "- 至少包含两个信息点（如：场景+感受 / 事件+观点）\n"
            "- 避免过短口号式表达\n"
            "- 禁止使用 Emoji、颜文字、装饰符号（如 ✨🌸~）"
        )

        try:
            text = await self._call_llm(user_prompt, system_prompt)
            if text:
                rewritten_text = str(text).strip()
                if rewritten_text:
                    rewritten_text = _sanitize_publish_output(rewritten_text)
                    if not rewritten_text:
                        logger.debug("[发布改写] 改写结果经净化后为空，回退原文")
                        return raw_text
                    if len(rewritten_text) < 24:
                        logger.debug(
                            f"[发布改写] 改写结果偏短（len={len(rewritten_text)}），按单次请求策略直接采用"
                        )
                    else:
                        logger.debug("[发布改写] 发布前内容改写成功")
                    return rewritten_text
        except Exception as e:
            logger.warning(f"[发布改写] 发布前改写失败，回退原文: {e}")

        return raw_text

    # ---- 随机主题 ----

    async def generate_random_publish_topic(self) -> str | None:
        """生成随机发布主题（LLM驱动）。"""
        persona_text, style_text = self.get_persona_and_style()
        history_block = self._state.build_publish_history_block(limit=8)
        history_text = f"\n{history_block}\n" if history_block else ""

        system_prompt = (
            "你是中文社交媒体选题助手。"
            "你的任务是给出一个简短且有启发性的发帖主题。"
            "只输出主题本身，不要解释。"
        )
        user_prompt = (
            "请随机生成一个适合 QQ 空间的发布主题，要求：\n"
            "- 仅输出主题本身（一个短语或短句）\n"
            "- 长度 6~18 字\n"
            "- 贴近日常生活，可激发具体表达\n"
            "- 与最近主题语义避免重复\n"
            "- 不要包含引号、编号、前后缀说明\n\n"
            f"人设参考：{persona_text}\n"
            f"表达风格：{style_text}\n"
            f"{history_text}"
        )

        try:
            topic = await self._call_llm(user_prompt, system_prompt)
        except Exception as e:
            logger.warning(f"[随机主题] LLM生成失败: {e}")
            return None

        cleaned = str(topic or "").strip().replace("\n", " ").replace("\r", " ")
        cleaned = cleaned.strip("\"'""`")
        cleaned = " ".join(cleaned.split())

        if len(cleaned) < 2:
            return None
        if len(cleaned) > 24:
            cleaned = cleaned[:24].strip()

        return cleaned or None

    # ---- LLM 调用 ----

    @staticmethod
    async def _call_llm(full_prompt: str, system_prompt: str) -> str | None:
        """调用 LLM 生成文本。"""
        try:
            from src.app.plugin_system.api.llm_api import get_model_set_by_task
            from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text

            logger.debug("[AI评论] 请求AI生成评论（完整提示词）...")
            model_set = get_model_set_by_task("actor")

            llm_request = LLMRequest(model_set=model_set)
            if system_prompt:
                llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            llm_request.add_payload(LLMPayload(ROLE.USER, Text(full_prompt)))

            response = await llm_request.send(stream=False)
            comment = str(getattr(response, "message", "") or "").strip()
            if comment:
                if comment.startswith('"') and comment.endswith('"'):
                    comment = comment[1:-1]
                if comment.startswith("'") and comment.endswith("'"):
                    comment = comment[1:-1]
                return comment
        except Exception as e:
            logger.warning(f"[AI评论] LLM调用失败: {e}")
        return None


# ---- 模块级工具函数 ----

def _compact_prompt_rules(
    rules: list[str],
    *,
    max_items: int = 6,
    max_chars: int = 60,
    priority_keywords: list[str] | None = None,
) -> list[str]:
    """精简提示词规则列表（去重、限长度、按优先级裁剪、限条数）。"""
    compacted_all: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    normalized_max_items = max(1, int(max_items))
    normalized_max_chars = max(8, int(max_chars))
    normalized_keywords = [
        str(item).strip().lower()
        for item in list(priority_keywords or [])
        if str(item).strip()
    ]

    for index, raw in enumerate(list(rules or [])):
        text = str(raw or "").strip()
        if not text:
            continue

        normalized = " ".join(text.split()).lower()
        if normalized in seen:
            continue
        seen.add(normalized)

        compacted_text = text
        if len(compacted_text) > normalized_max_chars:
            compacted_text = compacted_text[: normalized_max_chars - 1].rstrip() + "…"

        compacted_all.append((index, compacted_text, normalized))

    if normalized_keywords:
        def _rule_score(normalized_rule: str) -> int:
            score = 0
            for keyword in normalized_keywords:
                if keyword in normalized_rule:
                    score += 1
            return score

        compacted_all.sort(key=lambda item: (-_rule_score(item[2]), item[0]))

    return [item[1] for item in compacted_all[:normalized_max_items]]


def _sanitize_publish_output(text: str) -> str:
    """净化发布文本，去除 emoji 与装饰性符号。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"[\U00010000-\U0010FFFF]", "", cleaned)
    cleaned = re.sub(
        r"[^\u4e00-\u9fffA-Za-z0-9，。！？、；：""''（）《》【】…—\-\s]",
        "",
        cleaned,
    )
    cleaned = re.sub(r"[~～]{1,}", "", cleaned)
    cleaned = re.sub(r"[！!]{2,}", "！", cleaned)
    cleaned = re.sub(r"[？?]{2,}", "？", cleaned)
    cleaned = " ".join(cleaned.split())

    return cleaned.strip()