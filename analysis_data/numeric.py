"""精确有理数解析与规范化输出。

资料中的数值以字符串保存（例如 ``"1/2"``、``"0.25"``）。本模块不把
任何输入转成二进制浮点数，所有内部比较和算术都使用 ``fractions.Fraction``。
"""
from __future__ import annotations

import re
from fractions import Fraction

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_FRAC_RE = re.compile(r"^[+-]?[0-9]+/[0-9]+$")
_DEC_RE = re.compile(r"^[+-]?[0-9]+\.[0-9]+$")


class NumericError(ValueError):
    """数值不符合冻结资料的表达方式。"""


def rational(value, name: str = "数值") -> Fraction:
    """把 JSON 中的整数或字符串解析为精确有理数。

    ``bool`` 不是整数，浮点数会被拒绝：冻结资料要求有理数以字符串保存。
    ``null``、列表、对象同样会被拒绝，且 ``null`` 不会被当作 0。
    """
    if type(value) is int:
        return Fraction(value)
    if not isinstance(value, str):
        raise NumericError(f"{name} 必须是以字符串保存的有理数或整数，得到 {type(value).__name__}")
    text = value.strip()
    if not text:
        raise NumericError(f"{name} 为空字符串")
    if _INT_RE.match(text):
        return Fraction(int(text))
    if _FRAC_RE.match(text):
        numerator, denominator = text.split("/")
        if int(denominator) == 0:
            raise NumericError(f"{name} 分母为零")
        return Fraction(int(numerator), int(denominator))
    if _DEC_RE.match(text):
        sign = -1 if text.startswith("-") else 1
        unsigned = text.lstrip("+-")
        whole, frac = unsigned.split(".")
        numerator = int(whole) * 10 ** len(frac) + int(frac)
        return Fraction(sign * numerator, 10 ** len(frac))
    raise NumericError(f"{name} 不是可识别的有理数：{value!r}")


def text(value: Fraction) -> str:
    """把有理数写成稳定的字符串形式（整数不带分母）。"""
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def nonnegative(value, name: str) -> Fraction:
    q = rational(value, name)
    if q < 0:
        raise NumericError(f"{name} 不能为负")
    return q


def positive(value, name: str) -> Fraction:
    q = rational(value, name)
    if q <= 0:
        raise NumericError(f"{name} 必须为正")
    return q


def in_unit_interval(value, name: str) -> Fraction:
    q = rational(value, name)
    if q <= 0 or q > 1:
        raise NumericError(f"{name} 必须落在 (0, 1]")
    return q


def integer(value, name: str = "整数", minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise NumericError(f"{name} 必须是 >= {minimum} 的整数")
    return value
