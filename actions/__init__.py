"""Actions 模块。

提供发送QQ空间说说的主动交互功能。
"""

from .send_shuoshuo import SendShuoshuoAction
from .delete_shuoshuo import DeleteShuoshuoAction
from .like_shuoshuo import LikeShuoshuoAction
from .comment_shuoshuo import CommentShuoshuoAction
from .read_shuoshuo import ReadShuoshuoAction
from .auto_monitor import AutoMonitorAction

__all__ = [
    "SendShuoshuoAction",
    "DeleteShuoshuoAction",
    "LikeShuoshuoAction",
    "CommentShuoshuoAction",
    "ReadShuoshuoAction",
    "AutoMonitorAction",
]
