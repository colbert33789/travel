"""高德「道路路况」实时接口客户端 (需 Web 服务 Key, 环境变量 AMAP_KEY).

接口: https://restapi.amap.com/v3/traffic/status/road?name=&adcode=&extensions=all
返回路段实时平均车速; 以速度比折算拥堵延时指数:
  TTI ≈ v_free / v_obs   (TTI 定义为行程时间比, 同一路段上等价于速度反比)
  高速公路自由流速度 v_free 取 90 km/h(客货混行实测经验值).

两点实测约束(2026-09 实测):
  1. 道路名必须带国家高速编号前缀("G4京港澳高速" 可查, "京港澳高速" 返回
     UNKNOWN_ERROR/20003); 且并非 20 条走廊都在高德道路名索引内, 实测仅 7 条可查。
  2. 免费 Key 有 QPS 限制, 连续请求会返回 CGQPS_HAS_EXCEEDED_THE_LIMIT, 需逐条节流。
因此 all_tti 逐条隔离异常 —— 单条查不到不应影响其余走廊。
"""
from __future__ import annotations

import logging
import statistics
import time

from ..geo import TOWNS
from ..geo import TOWN_INDEX
from .http import FetchError, get_json

logger = logging.getLogger("holiday_traffic.amap")

# 深圳中心(WGS84): 驾车规划的起点
SHENZHEN = ("深圳", 22.55, 114.06)

API = "https://restapi.amap.com/v3/traffic/status/road"
LODGING_API = "https://restapi.amap.com/v3/place/around"
LODGING_TYPE = "100000"  # 高德 POI「住宿服务」大类
LODGING_RADIUS_M = 5000
V_FREE_KMH = 90.0
REQUEST_PAUSE = 0.5     # 逐条查询间隔, 规避免费 Key 的 QPS 限制
_SINGLE_ERRORS = (FetchError, OSError, ValueError, KeyError)

# 走廊 -> (高德道路名, 查询所在城市 adcode): 取深圳出城方向最易拥堵的一段
# 实测(2026-09-29)可查的 7 条已改为带编号的路名; 其余沿用简称, 查不到时自动跳过。
HIGHWAY_ROADS: dict[str, tuple[str, str]] = {
    "G4": ("G4京港澳高速", "440300"), "S3": ("广深沿江高速", "440300"),
    "G15": ("G15沈海高速", "440300"), "G25": ("G25长深高速", "440300"),
    "S30": ("惠深沿海高速", "440300"), "G94": ("G94珠三角环线高速", "441900"),
    "G0422": ("武深高速", "440300"), "G35": ("G35济广高速", "441300"),
    "G9411": ("G9411莞佛高速", "441900"), "S29": ("从莞深高速", "441900"),
    "G80": ("广昆高速", "441200"), "G0423": ("乐广高速", "440200"),
    "G0425": ("广澳高速", "440100"), "S32": ("西部沿海高速", "440700"),
    "G1508": ("广州绕城高速", "440100"), "G6011": ("南韶高速", "440200"),
    "G55": ("二广高速", "441800"), "G2518": ("深岑高速", "442000"),
    "S2": ("S2广河高速", "440100"),
    # 深中通道 2024 年通车, 高德道路名实测同样未收录(UNKNOWN_ERROR), 保持模型预测
    "SZLINK": ("深中通道", "440300"),
}


class AmapDriveClient:
    """高德驾车路径规划 -> 走廊实时 TTI (覆盖 20/20 条走廊).

    为什么它比「道路路况」更适合做主源:
      · v3/traffic/status/road 依赖道路名索引, 实测 20 条走廊只收录 7 条
        (G4/G15/G25/G94/G35/G9411/S2), 其余返回 UNKNOWN_ERROR;
      · 该服务配额独立且较低, 当日调用超限会 USER_DAILY_QUERY_OVER_LIMIT,
        而驾车路径规划配额独立且充足;
      · 路径规划的 tmcs 提供分段路况状态, 不受道路名收录限制。

    TTI 计算(调和平均, 这是唯一正确的行程时间比):
      各分段按距离加权"时间", 总时间 = Σ(距离/速度), TTI = 自由流耗时 / 实际耗时。
      直接对速度做算术平均会低估拥堵 —— 一段 12km/h 的严重拥堵占总路程 1%,
      对行程时间的影响远不止 1%。
    """

    V_FREE_KMH = 90.0
    # 高德路况状态 -> 代表速度 (km/h); 未知段按畅通处理(避免高估)
    STATUS_SPEED = {"畅通": V_FREE_KMH, "缓行": 45.0, "拥堵": 22.0, "严重拥堵": 12.0}
    STRATEGY = "2"   # 距离优先: 路线稳定、不为避堵绕行, tmcs 才反映主线真实状况
    ROUTE_API = "https://restapi.amap.com/v5/direction/driving"
    SHOW_FIELDS = "cost,tmcs,polyline"

    # 走廊 -> 代表终点(该方向的城镇)。用于让规划路线尽量沿该走廊主线的方向走,
    # 属启发式: 规划路线未必逐段覆盖该高速, 结果应视为「该方向的实时路况」。
    HIGHWAY_DEST: dict[str, str] = {
        "G4": "增城区", "S3": "南沙区", "G15": "汕尾城区(红海湾)",
        "G25": "源城区(万绿湖)", "S30": "惠东县(巽寮湾)", "G94": "松山湖",
        "G0422": "英德市(英西峰林)", "G35": "塘厦镇", "G9411": "江门开平(碉楼)",
        "S29": "从化区", "G80": "端州区(鼎湖山)", "G0423": "韶关乳源(大峡谷)",
        "G0425": "珠海横琴(长隆)", "S32": "阳江海陵岛", "G1508": "佛山顺德(美食)",
        "G6011": "始兴县(车八岭)", "G55": "清远连山(欧家梯田)",
        "G2518": "云浮新兴(禅泉)", "S2": "龙门县", "SZLINK": "中山市区",
    }

    def __init__(self, key: str, pause: float = REQUEST_PAUSE):
        if not key:
            raise ValueError("未配置 AMAP_KEY")
        self.key = key
        self.pause = pause

    def route_tti(self, lon: float, lat: float) -> float | None:
        """深圳 -> (lon,lat) 路线的实时 TTI; 无有效路段返回 None."""
        data = get_json(self.ROUTE_API, {
            "key": self.key, "origin": f"{SHENZHEN[2]},{SHENZHEN[1]}",
            "destination": f"{lon},{lat}", "strategy": self.STRATEGY,
            "show_fields": self.SHOW_FIELDS,
        })
        if str(data.get("status")) != "1":
            raise FetchError(f"高德驾车规划错误: {data.get('info')}")
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            return None
        segs = []
        for step in paths[0].get("steps") or []:
            for tmc in step.get("tmcs") or []:
                dist = tmc.get("tmc_distance") or tmc.get("distance")
                stat = tmc.get("tmc_status") or tmc.get("status")
                if dist is None or stat is None:
                    continue
                try:
                    segs.append((float(dist), self.STATUS_SPEED.get(str(stat), self.V_FREE_KMH)))
                except (TypeError, ValueError):
                    continue
        total_m = sum(d for d, _ in segs)
        if total_m <= 0:
            return None
        hours = sum(d / v for d, v in segs) / 1000.0
        if hours <= 0:
            return None
        # TTI = 自由流耗时 / 实际耗时 = V_FREE * hours / 总距离(km)
        return round(max(1.0, self.V_FREE_KMH * hours / (total_m / 1000.0)), 2)

    def all_tti(self) -> dict[str, float]:
        """逐条走廊规划并取 TTI; 单条失败跳过, 末尾汇总。"""
        out: dict[str, float] = {}
        failed: list[str] = []
        for hid, dest in self.HIGHWAY_DEST.items():
            town = TOWN_INDEX.get(dest)
            if town is None:
                failed.append(hid)
                continue
            try:
                tti = self.route_tti(town.lon, town.lat)
            except _SINGLE_ERRORS as e:
                logger.debug("高德驾车规划 %s 失败: %s", hid, e)
                failed.append(hid)
                tti = None
            if tti is not None:
                out[hid] = tti
            time.sleep(self.pause)
        if failed:
            logger.warning("高德驾车规划 %d/%d 条走廊无数据: %s",
                           len(failed), len(self.HIGHWAY_DEST), "、".join(failed))
        return out


class AmapTrafficClient:
    def __init__(self, key: str, pause: float = REQUEST_PAUSE):
        if not key:
            raise ValueError("未配置 AMAP_KEY")
        self.key = key
        self.pause = pause

    def road_tti(self, highway_id: str) -> float | None:
        """单条走廊实时 TTI; 查不到或无有效车速样本返回 None(调用方跳过)."""
        name, adcode = HIGHWAY_ROADS[highway_id]
        data = get_json(API, {"key": self.key, "name": name, "adcode": adcode,
                              "extensions": "all"})
        if str(data.get("status")) != "1":
            raise FetchError(f"高德路况错误: {data.get('info')}")
        speeds = [float(r["speed"]) for r in data.get("trafficinfo", {}).get("roads", [])
                  if str(r.get("speed", "")).replace(".", "", 1).isdigit()
                  and float(r["speed"]) > 0]
        if not speeds:
            return None
        v_obs = statistics.median(speeds)
        return round(max(1.0, V_FREE_KMH / max(v_obs, 5.0)), 3)

    def all_tti(self) -> dict[str, float]:
        """逐条查询; 单条失败(路名未收录/QPS/网络)只跳过该条, 不影响其余走廊.

        12 条走廊路名未被高德索引属已知常态, 逐条报警会淹没真正的问题,
        故明细记 debug、末尾只汇总一条 warning。
        """
        out: dict[str, float] = {}
        failed: list[str] = []
        exhausted = False
        for hid in HIGHWAY_ROADS:
            try:
                tti = self.road_tti(hid)
            except _SINGLE_ERRORS as e:
                logger.debug("高德路况 %s 查询失败: %s", hid, e)
                if isinstance(e, FetchError) and "USER_DAILY_QUERY_OVER_LIMIT" in str(e):
                    exhausted = True
                    break
                failed.append(hid)
                tti = None
            if tti is not None:
                out[hid] = tti
            time.sleep(self.pause)
        if exhausted:
            logger.warning("高德实时路况每日调用配额已耗尽，本轮停止查询")
        elif failed:
            logger.warning("高德路况 %d/%d 条走廊无数据(多为道路名未收录): %s",
                           len(failed), len(HIGHWAY_ROADS), "、".join(failed))
        return out


class AmapLodgingClient:
    """按目的地中心统一 5km 检索住宿 POI 数量；不是客房量或 OTA 预订数据。"""

    def __init__(self, key: str, pause: float = REQUEST_PAUSE):
        if not key:
            raise ValueError("未配置 AMAP_KEY")
        self.key = key
        self.pause = pause

    def count_near(self, lon: float, lat: float) -> int:
        data = get_json(LODGING_API, {
            "key": self.key, "location": f"{lon},{lat}", "radius": LODGING_RADIUS_M,
            "types": LODGING_TYPE, "offset": 1, "page": 1, "extensions": "base",
        }, retries=1)
        if str(data.get("status")) != "1":
            raise FetchError(f"高德住宿 POI 错误: {data.get('info', '未知错误')}")
        raw, pois = data.get("count"), data.get("pois")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)) or not str(raw).isdigit():
            raise FetchError("高德住宿 POI 返回无效数量")
        count = int(raw)
        if not isinstance(pois, list) or (count > 0 and not pois) or (count == 0 and pois):
            raise FetchError("高德住宿 POI 返回不完整")
        return count

    def all_counts(self) -> dict[str, int]:
        """每天至多每地请求一次；单地失败不污染其他目的地。"""
        counts: dict[str, int] = {}
        for i, town in enumerate(TOWNS):
            try:
                counts[town.name] = self.count_near(town.lon, town.lat)
            except _SINGLE_ERRORS as e:
                logger.warning("高德住宿 POI %s 查询失败: %s", town.name, e)
            if i < len(TOWNS) - 1:
                time.sleep(self.pause)
        if not counts:
            raise FetchError("高德住宿 POI 所有目的地均不可用")
        return counts
