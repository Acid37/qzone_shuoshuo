"""Qzone 插件公共类型定义。

包含操作结果封装、状态枚举等通用数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class ResultStatus(Enum):
    """操作结果状态"""
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class Result(Generic[T]):
    """操作结果封装"""
    status: ResultStatus
    data: T | None = None
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    @classmethod
    def ok(cls, data: T) -> "Result[T]":
        return cls(status=ResultStatus.SUCCESS, data=data)

    @classmethod
    def fail(cls, message: str) -> "Result[T]":
        return cls(status=ResultStatus.ERROR, error_message=message)