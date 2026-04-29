"""Qzone Shuoshuo 插件配置"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class QzoneConfig(BaseConfig):
    """Qzone Shuoshuo 插件配置类"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "QQ空间说说插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置"""

        enabled: bool = Field(default=True, description="是否启用插件")

    @config_section("debug")
    class DebugSection(SectionBase):
        """调试配置"""

        enable_debug: bool = Field(default=False, description="是否启用调试模式")
        log_level: str = Field(
            default="info",
            description="日志级别: info(简洁), debug(详细)"
        )

    @config_section("qzone")
    class QzoneSection(SectionBase):
        """QQ空间配置"""

        adapter_signature: str = Field(
            default="napcat_adapter:adapter:napcat_adapter",
            description="适配器签名，用于获取 Cookie 等"
        )
        default_visible: str = Field(default="all", description="默认可见范围: all/friends/self")
        enable_image: bool = Field(default=False, description="是否允许发送图片说说")
        max_image_count: int = Field(default=9, description="单条说说最大图片数")

    @config_section("monitor")
    class MonitorSection(SectionBase):
        """监控默认配置。"""

        enabled: bool = Field(default=True, description="监控总开关，关闭后不允许启动自动监控")
        auto_start: bool = Field(default=True, description="插件加载后是否自动启动监控")
        feed_source: str = Field(
            default="friend_flow",
            description="监控动态源: friend_flow(好友动态流)/self_list(自己空间列表)"
        )
        friend_feed_count: int = Field(
            default=20,
            description="friend_flow 源每轮抓取条数（5-50）"
        )
        # 范围: 60 ~ 86400 秒
        default_interval: int = Field(default=1800, description="默认监控间隔（秒），范围 60-86400")
        log_heartbeat: bool = Field(
            default=True,
            description="是否输出每轮监控心跳日志（info级），用于确认监控仍在运行"
        )
        auto_like: bool = Field(
            default=True,
            description="是否默认启用自动点赞（start 未显式传参时生效）"
        )
        auto_comment: bool = Field(
            default=True,
            description="是否默认启用自动评论（start 未显式传参时生效）"
        )
        # 概率配置（0=不执行，1=必定执行）
        like_probability: float = Field(
            default=0.8,
            description="点赞概率 (0.0-1.0)，0.8=80%概率点赞"
        )
        comment_probability: float = Field(
            default=0.3,
            description="评论概率 (0.0-1.0)，0.3=30%概率评论"
        )
        # 回复自己说说的评论
        enable_auto_reply_comments: bool = Field(
            default=True,
            description="是否回复自己说说的评论（仅在自动评论开启时生效）"
        )
        auto_reply_probability: float = Field(
            default=0.9,
            description="回复自己说说评论的概率 (0.0-1.0)，0.9=90%概率回复"
        )
        quiet_hours_enabled: bool = Field(
            default=True,
            description="是否启用监控静默时间窗口（静默时段不执行自动监控）"
        )
        quiet_hours_start: int = Field(
            default=23,
            description="静默时段开始小时（0-23，含）"
        )
        quiet_hours_end: int = Field(
            default=7,
            description="静默时段结束小时（0-23，不含；若小于开始小时则视为跨天窗口）"
        )

    @config_section("storage")
    class StorageSection(SectionBase):
        """存储配置"""

        data_dir: str = Field(
            default="data/qzone_shuoshuo",
            description="插件数据存储目录"
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    debug: DebugSection = Field(default_factory=DebugSection)
    qzone: QzoneSection = Field(default_factory=QzoneSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    storage: StorageSection = Field(default_factory=StorageSection)
