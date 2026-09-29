"""节假日历法: 假期日型(categorical 特征).

日型只表达"假期第几天"的语义类别; 出行强度不再使用人工系数,
统一由 migration.MigrationProfile(百度慧眼真实剖面)提供.
"""
from __future__ import annotations

DAY_TYPES = ("pre", "depart", "mid", "return_early", "return_last", "post")


def holiday_day_type(offset: int, total_days: int = 7) -> str:
    """offset: 相对假期首日, D1=1; 0 为节前一天."""
    if offset == 0:
        return "pre"
    if offset < 1 or offset > total_days:
        return "post"
    if offset == 1:
        return "depart"
    if offset == total_days:
        return "return_last"
    if offset == total_days - 1:
        return "return_early"
    return "mid"
