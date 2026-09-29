"""Open-Meteo 天气预报客户端 (免 Key, 16 天逐日预报, 支持多点批量).

降水分级采用 GB/T 28592-2012《降水量等级》24h 口径:
  小雨 0.1-9.9mm | 中雨 10-24.9mm | 大雨 25-49.9mm | 暴雨 >=50mm
工程上将 <1mm 视为无明显降水(模式微量降水噪声).
"""
from __future__ import annotations

from .http import FetchError, get_json

API = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = "precipitation_sum,precipitation_probability_max,temperature_2m_max,weather_code"


def rain_level(precip_mm: float) -> int:
    """0 无明显降水 | 1 小雨 | 2 中雨 | 3 大雨及以上."""
    if precip_mm is None or precip_mm < 1.0:
        return 0
    if precip_mm < 10.0:
        return 1
    if precip_mm < 25.0:
        return 2
    return 3


RAIN_LABEL = {0: "无雨", 1: "小雨", 2: "中雨", 3: "大雨"}


def weather_icon(code: int | None, rain: int = 0) -> str:
    """WMO 天气代码 -> emoji."""
    if code is None:
        return "🌧" if rain else "⛅"
    if code >= 95:
        return "⛈"
    if code >= 61 or 51 <= code <= 57 or 80 <= code <= 82:
        return "🌧"
    if code in (45, 48):
        return "🌫"
    if code in (1, 2):
        return "🌤"
    if code == 3:
        return "☁️"
    return "☀️"


class OpenMeteoClient:
    def daily_forecast(self, points: list[tuple[float, float]],
                       start: str, end: str) -> list[dict]:
        """返回与 points 等长的列表, 每项 {date: {precip, prob, tmax, code}}."""
        if not points:
            return []
        params = {
            "latitude": ",".join(f"{lat:.3f}" for lat, _ in points),
            "longitude": ",".join(f"{lon:.3f}" for _, lon in points),
            "daily": DAILY_VARS, "timezone": "Asia/Shanghai",
            "start_date": start, "end_date": end,
        }
        data = get_json(API, params)
        if isinstance(data, dict) and data.get("error"):
            raise FetchError(f"Open-Meteo 错误: {data.get('reason')}")
        items = data if isinstance(data, list) else [data]
        if len(items) != len(points):
            raise FetchError("Open-Meteo 返回点数与请求不一致")
        out = []
        for item in items:
            d = item["daily"]
            out.append({
                t: {"precip": d["precipitation_sum"][i],
                    "prob": d["precipitation_probability_max"][i],
                    "tmax": d["temperature_2m_max"][i],
                    "code": d["weather_code"][i]}
                for i, t in enumerate(d["time"])
            })
        return out
