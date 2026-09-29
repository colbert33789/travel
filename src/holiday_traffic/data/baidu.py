"""百度慧眼迁徙与百度地图住宿地点检索客户端.

接口:
  historycurve.jsonp  城市每日迁徙规模指数(迁出/迁入), 覆盖 2019 至前一日
  cityrank.jsonp      城市某日迁出目的地/迁入来源地 Top100 占比(%)

说明: 规模指数为相对值(非人数), 换算人数需 config.migration.people_per_index;
数据通常 T+1 更新. 仅用于研究预测, 请控制请求频率.
"""
from __future__ import annotations

import logging
import time

from ..geo import TOWNS
from .http import FetchError, get_json, get_jsonp

BASE = "https://huiyan.baidu.com/migration"
PLACE_API = "https://api.map.baidu.com/place/v2/search"
PLACE_QUERY = "酒店$民宿$宾馆"
PLACE_RADIUS_M = 5000
PLACE_TOTAL_LIMIT = 150  # Place 2.0 官方文档说明的单次 total 最大值
logger = logging.getLogger("holiday_traffic.baidu")


class BaiduMigrationClient:
    def __init__(self, pause: float = 0.15):
        self.pause = pause  # 请求间隔, 礼貌抓取

    def _call(self, endpoint: str, params: dict) -> dict:
        data = get_jsonp(f"{BASE}/{endpoint}", params)
        if data.get("errno") not in (0, "0"):
            raise FetchError(f"百度迁徙业务错误: {data.get('errmsg')}")
        time.sleep(self.pause)
        return data.get("data") or {}

    def lastdate(self) -> str:
        """迁徙数据最新更新日 (YYYYMMDD), 用于溯源报告数据时效."""
        data = self._call("lastdate.jsonp", {})
        return str((data.get("data") or {}).get("lastdate") or data.get("lastdate") or "")

    def history_curve(self, adcode: str, direction: str = "move_out") -> dict[str, float]:
        """{YYYYMMDD: 规模指数}."""
        data = self._call("historycurve.jsonp",
                          {"dt": "city", "id": adcode, "type": direction})
        curve = {k: float(v) for k, v in (data.get("list") or {}).items() if v is not None}
        if not curve:
            raise FetchError(f"迁徙曲线为空: {adcode} {direction}")
        return curve

    def city_rank(self, adcode: str, date: str, direction: str = "move_out") -> dict[str, float]:
        """{城市名: 占比%}; date 为 YYYYMMDD. 无数据返回空 dict."""
        data = self._call("cityrank.jsonp",
                          {"dt": "city", "id": adcode, "type": direction, "date": date})
        return {row["city_name"]: float(row["value"]) for row in (data.get("list") or [])}


class BaiduLodgingClient:
    """查询周边住宿关键词 POI 总数；非客房量、房价或平台成交数据。"""

    def __init__(self, ak: str, pause: float = 0.5):
        if not ak:
            raise ValueError("未配置 BAIDU_MAP_AK")
        self.ak = ak
        self.pause = pause

    def count_near(self, lat: float, lon: float) -> int:
        data = get_json(PLACE_API, {
            "ak": self.ak, "query": PLACE_QUERY, "location": f"{lat},{lon}",
            "coord_type": 1, "radius": PLACE_RADIUS_M, "radius_limit": "true",
            "page_num": 0, "page_size": 1, "output": "json",
        }, retries=1)
        if str(data.get("status")) != "0":
            raise FetchError(f"百度地图地点检索状态异常: {data.get('status')}")
        total, results = data.get("total"), data.get("results")
        if isinstance(total, bool) or not isinstance(total, (int, str)) or not str(total).isdigit():
            raise FetchError("百度地图地点检索数量无效")
        count = int(total)
        if not isinstance(results, list) or bool(count) != bool(results):
            raise FetchError("百度地图地点检索响应不完整")
        return count

    def all_counts(self) -> dict[str, int]:
        """每次仅取一条以读 total；逐地容错，避免耗尽 POI 调用额度。"""
        counts: dict[str, int] = {}
        for i, town in enumerate(TOWNS):
            try:
                counts[town.name] = self.count_near(town.lat, town.lon)
            except (FetchError, OSError, ValueError, KeyError) as e:
                logger.warning("百度地图住宿 POI %s 查询失败: %s", town.name, type(e).__name__)
            if i < len(TOWNS) - 1:
                time.sleep(self.pause)
        if not counts:
            raise FetchError("百度地图住宿 POI 所有目的地均不可用")
        return counts
