"""项目统一时钟: 所有「现在」一律取 config.holiday.timezone 的本地时间.

为什么需要一个统一入口:
  预测窗口判定(nowcast 实测日替换、高德实时路况校准)、数据时效戳、报告时间戳
  都按「假期当地日期」处理。若直接用 dt.datetime.now()/time.strftime,
  服务器运行在 UTC 时会比北京时间晚 8 小时:
    - 假期当天 00:00-08:00 CST 被算成前一天 -> 实时校准与实测替换静默失效;
    - 天气预报可及窗口差一天;
    - 报告上的「更新于」时间误导读者。
因此所有取时点统一走本模块, 时区由配置驱动。
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger("holiday_traffic.clock")

DEFAULT_TZ = "Asia/Shanghai"
STAMP_FMT = "%Y-%m-%d %H:%M:%S"
# zoneinfo 依赖系统 tzdata; 未安装 tzdata 的环境(如 Windows)退回固定 UTC+8,
# 保证「北京时间」这一默认语义始终正确, 而不是让整条流水线在取时区时崩溃。
_FALLBACK = dt.timezone(dt.timedelta(hours=8), "CST")


def tz_of(cfg: dict | None) -> str:
    """从配置取时区名, 缺失时退回北京时间."""
    return ((cfg or {}).get("holiday") or {}).get("timezone") or DEFAULT_TZ


def zone(name: str | None = None) -> dt.tzinfo:
    n = name or DEFAULT_TZ
    try:
        return ZoneInfo(n)
    except Exception as e:                      # ZoneInfoNotFoundError 等
        logger.warning("时区 %s 不可用(%s), 退回固定 %s", n, e, _FALLBACK)
        return _FALLBACK


def now(name: str | None = None) -> dt.datetime:
    """当前时刻(带时区)."""
    return dt.datetime.now(zone(name))


def today(name: str | None = None) -> dt.date:
    """当前日期(按配置时区, 而非服务器本地日期)."""
    return now(name).date()


def stamp(name: str | None = None, fmt: str = STAMP_FMT) -> str:
    """当前时刻字符串, 供 provenance / metrics / 报告展示."""
    return now(name).strftime(fmt)
