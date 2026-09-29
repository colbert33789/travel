"""DataHub: 实时数据统一入口 (TTL 缓存 + 溯源审计 + 分级降级).

数据源与降级链:
  百度慧眼迁徙  live(免Key) -> 过期缓存 -> 仓库内置真实快照(reference/baidu_snapshot.json)
  Open-Meteo 天气 live(免Key) -> 过期缓存 -> 气候均值
  高德实时路况   live(需 AMAP_KEY) -> 不校准(保持模型预测)

mode: auto(默认, 联网拉取) | offline(不联网, 用于测试/CI 可复现)
每个数据源的实际来源与更新时间记录在 provenance, 随产出物与报告一起发布.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import time
from pathlib import Path

import pandas as pd

from ..clock import stamp, today, tz_of
from ..config import PROJECT_ROOT
from ..geo import TOWNS
from .http import FetchError, get_json

logger = logging.getLogger("holiday_traffic.datahub")

SNAPSHOT = Path(__file__).resolve().parents[1] / "reference" / "baidu_snapshot.json"
SHENZHEN = ("深圳", 22.55, 114.06)
TTL = {"migration": 6 * 3600, "weather": 3 * 3600, "traffic": 10 * 60,
       # 铁路逐日查询要 7 天 × 20 方向(约 4 分钟), 且逐日售票热度变化没那么快,
       # 故缓存 2 小时, 避免每轮刷新都打满 12306。
       "railway": 2 * 3600, "lodging": 24 * 3600, "baidu_lodging": 24 * 3600,
       "tencent_drive": 3600}
TENCENT_COORD_API = "https://apis.map.qq.com/ws/coord/v1/translate"
TENCENT_ROUTE_API = "https://apis.map.qq.com/ws/direction/v1/driving/"
TENCENT_PAUSE = 0.3
FORECAST_HORIZON_DAYS = 15
PARTNER_COLUMNS = ("town", "platform", "metric", "value", "observed_at")
PARTNER_METRICS = {
    "ctrip": {"booking_heat", "price_premium", "sell_out", "hotel_price_cny", "available_rooms"},
    "qunar": {"booking_heat", "price_premium", "sell_out", "hotel_price_cny", "available_rooms"},
    "meituan": {"booking_heat", "price_premium", "sell_out", "hotel_price_cny", "available_rooms"},
    "fliggy": {"booking_heat", "price_premium", "sell_out", "hotel_price_cny", "available_rooms"},
    "xiaohongshu": {"social_heat", "post_count", "interaction_count"},
    "douyin": {"social_heat", "post_count", "interaction_count"},
    "weibo": {"social_heat", "post_count", "interaction_count"},
    "wechat": {"social_heat", "post_count", "interaction_count"},
}
CLIMATE = {"precip": 2.3, "prob": 25, "tmax": 29.0}  # 深圳 10 月气候均值
CLIMATE_RAIN_INPUT = 0.31                              # 降水等级气候期望


def _now() -> str:
    """模块级兜底(无配置上下文时); 类内一律用 self._now() 以带配置时区."""
    return stamp(None, "%Y-%m-%d %H:%M")


class TencentDailyQuotaError(FetchError):
    """腾讯位置服务本日调用额度耗尽 (status=121)."""


def _tencent_check(data: dict) -> None:
    try:
        status = int(data["status"])
    except (TypeError, ValueError, KeyError) as e:
        raise FetchError("腾讯位置服务缺少有效状态码") from e
    if status == 121:
        raise TencentDailyQuotaError("腾讯位置服务本日驾车请求额度耗尽")
    if status != 0:
        raise FetchError(f"腾讯位置服务错误码 {status}")


def _tencent_routes(key: str) -> dict[str, dict[str, float]]:
    """将项目 WGS84 地点转换为腾讯坐标后获取当前最快路线 ETA。"""
    coords = [(SHENZHEN[1], SHENZHEN[2])] + [(t.lat, t.lon) for t in TOWNS]
    converted: list[tuple[float, float]] = []
    for start in range(0, len(coords), 15):
        batch = coords[start:start + 15]
        try:
            data = get_json(TENCENT_COORD_API, {
                "key": key, "type": 1,
                "locations": ";".join(f"{lat},{lon}" for lat, lon in batch),
            }, retries=1)
        except FetchError as e:
            raise FetchError("腾讯坐标转换请求失败") from e
        _tencent_check(data)
        locations = data.get("locations")
        if not isinstance(locations, list) or len(locations) != len(batch):
            raise FetchError("腾讯坐标转换结果不完整")
        for point in locations:
            try:
                lat, lon = float(point["lat"]), float(point["lng"])
            except (ValueError, TypeError, KeyError) as e:
                raise FetchError("腾讯坐标转换结果无效") from e
            if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise FetchError("腾讯坐标转换结果越界")
            converted.append((lat, lon))
        time.sleep(TENCENT_PAUSE)

    origin = f"{converted[0][0]},{converted[0][1]}"
    out: dict[str, dict[str, float]] = {}
    for i, town in enumerate(TOWNS, start=1):
        try:
            data = get_json(TENCENT_ROUTE_API, {
                "key": key, "from": origin,
                "to": f"{converted[i][0]},{converted[i][1]}",
                "policy": "LEAST_TIME",
            }, retries=1)
            _tencent_check(data)
            routes = (data.get("result") or {}).get("routes")
            if not isinstance(routes, list) or not routes:
                raise FetchError("腾讯路线规划无可用路线")
            duration = float(routes[0]["duration"])
            distance = float(routes[0]["distance"])
            if not (math.isfinite(duration) and math.isfinite(distance)
                    and 0 < duration < 1440 and 0 < distance < 2_000_000):
                raise FetchError("腾讯路线规划数值无效")
            out[town.name] = {"eta_min": round(duration, 1),
                              "distance_km": round(distance / 1000, 2)}
        except TencentDailyQuotaError:
            raise
        except (FetchError, OSError, ValueError, TypeError, KeyError) as e:
            logger.warning("腾讯驾车 ETA %s 查询失败: %s", town.name, type(e).__name__)
        time.sleep(TENCENT_PAUSE)
    if not out:
        raise FetchError("腾讯驾车 ETA 无可用目的地")
    return out


class DataHub:
    def __init__(self, cfg: dict, cache_dir: str | Path | None = None):
        self.cfg = cfg
        self.mode = cfg["data"].get("mode", "auto")
        self.tz = tz_of(cfg)
        self.cache_dir = Path(cache_dir or cfg["data"].get("cache_dir", "data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.provenance: dict[str, dict] = {}

    def _now(self) -> str:
        return stamp(self.tz, "%Y-%m-%d %H:%M")

    # ---------------- 缓存 ----------------
    def _cache_path(self, name: str) -> Path:
        return self.cache_dir / f"{name}.json"

    def _read_cache(self, name: str, ttl: float | None):
        """读缓存; 缺失/过期/损坏一律返回 None(损坏时告警后当作无缓存去重新拉取)."""
        p = self._cache_path(name)
        if not p.exists():
            return None, None
        age = time.time() - p.stat().st_mtime
        if ttl is not None and age > ttl:
            return None, age
        try:
            return json.loads(p.read_text(encoding="utf-8")), age
        except (ValueError, OSError) as e:
            # 缓存在 try 之外读取, 损坏会直接掀掉整条流水线
            logger.warning("缓存 %s 损坏(%s), 本次重新拉取", p.name, e)
            return None, age

    def _write_cache(self, name: str, obj) -> None:
        p = self._cache_path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def _fetch(self, name: str, fetch_fn, force: bool = False):
        """TTL 内读缓存; 否则拉取; 失败退回过期缓存. 返回 (数据, 状态)."""
        if not force:
            data, _ = self._read_cache(name, TTL[name])
            if data is not None:
                return data, "cache"
        try:
            data = fetch_fn()
            self._write_cache(name, data)
            return data, "live"
        except (FetchError, OSError, ValueError, KeyError) as e:
            logger.warning("%s 拉取失败: %s", name, e)
            stale, _ = self._read_cache(name, None)
            return (stale, "stale") if stale is not None else (None, f"failed: {e}")

    # ---------------- 迁徙 ----------------
    def migration_inputs(self, force: bool = False) -> dict:
        """{'out'/'in': 深圳迁出/迁入曲线, 'rank': {YYYYMMDD: {城市: %}},
            'dest': {目的地城市: {'in': 曲线, 'out': 曲线}}} (目的地为全来源口径)."""
        from ..geo import CITY_ADCODE
        from ..migration import REFERENCE_HOLIDAYS
        from .baidu import BaiduMigrationClient

        def fetch():
            cli = BaiduMigrationClient()
            adcode = self.cfg["project"]["origin_adcode"]
            ranks = {}
            for start, _ in REFERENCE_HOLIDAYS.values():
                d0 = dt.date.fromisoformat(start)
                for k in range(3):
                    key = (d0 + dt.timedelta(k)).strftime("%Y%m%d")
                    ranks[key] = cli.city_rank(adcode, key)
            dest = {city: {"in": cli.history_curve(code, "move_in"),
                           "out": cli.history_curve(code, "move_out")}
                    for city, code in CITY_ADCODE.items()}
            return {"out": cli.history_curve(adcode, "move_out"),
                    "in": cli.history_curve(adcode, "move_in"),
                    "rank": ranks, "dest": dest, "fetched_at": self._now(),
                    "source_lastdate": cli.lastdate()}

        data, status = (None, "offline") if self.mode == "offline" else self._fetch(
            "migration", fetch, force)
        if data is None:
            data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            status = f"snapshot({data.get('fetched_at', '?')})" + (
                "" if self.mode == "offline" else f" <- {status}")
        latest = data.get("source_lastdate") or (max(data["out"]) if data.get("out") else "-")
        self.provenance["百度慧眼迁徙"] = {
            "status": status, "updated": data.get("fetched_at", self._now()),
            "detail": f"官方更新至 {latest[:4]}-{latest[4:6]}-{latest[6:]}; "
                      f"国庆城市占比 {len(data.get('rank', {}))} 日"}
        return data

    # ---------------- 天气 ----------------
    def weather(self, dates: list[str], force: bool = False) -> pd.DataFrame:
        """逐地逐日天气: place(深圳/各目的地), date, precip, prob, tmax, code, rain_level, rain_input."""
        from .openmeteo import OpenMeteoClient, rain_level

        places = [SHENZHEN] + [(t.name, t.lat, t.lon) for t in TOWNS]
        horizon = today(self.tz) + dt.timedelta(FORECAST_HORIZON_DAYS)
        in_range = [d for d in dates if dt.date.fromisoformat(d) <= horizon]

        def fetch():
            res = OpenMeteoClient().daily_forecast(
                [(lat, lon) for _, lat, lon in places], in_range[0], in_range[-1])
            return {"places": [p[0] for p in places], "data": res, "fetched_at": self._now()}

        data, status = (None, "offline") if (self.mode == "offline" or not in_range) \
            else self._fetch("weather", fetch, force)
        rows = []
        for i, (name, _, _) in enumerate(places):
            series = {}
            if data and name in data["places"]:
                series = data["data"][data["places"].index(name)]
            for d in dates:
                v = series.get(d)
                if v and v.get("precip") is not None:
                    lvl = rain_level(v["precip"])
                    rows.append({"place": name, "date": d, "precip": v["precip"],
                                 "prob": v["prob"], "tmax": v["tmax"], "code": v["code"],
                                 "rain_level": lvl, "rain_input": float(lvl),
                                 "source": "forecast"})
                else:
                    rows.append({"place": name, "date": d, **CLIMATE, "code": None,
                                 "rain_level": 0, "rain_input": CLIMATE_RAIN_INPUT,
                                 "source": "climate"})
        df = pd.DataFrame(rows)
        n_fc = int((df["source"] == "forecast").sum())
        if not n_fc:
            st = f"climate <- {status}"
        elif n_fc < len(df):
            st = f"部分气候均值 <- {status}"   # 个别地点无预报时不能只报"实时"
        else:
            st = status
        self.provenance["Open-Meteo 天气"] = {
            "status": st, "updated": (data or {}).get("fetched_at", self._now()),
            "detail": f"{len(places)} 个地点逐日预报, 覆盖 {n_fc}/{len(df)} 个地点-日"}
        return df

    # ---------------- 铁路 12306 ----------------
    def railway_demand(self, dates: list[str], force: bool = False) -> dict:
        """{'city': {方向: {trains,soldout,rate}}, 'curve': {日期: 深圳北→广州南售罄率}}.
        免 Key, 显示性偏好数据; offline 或失败返回 {}."""
        from ..geo import CITY_ADCODE
        from .railway import CITY_STATIONS, RailwayClient

        if self.mode == "offline":
            self.provenance["铁路12306余票"] = {"status": "offline", "updated": "-",
                                              "detail": "离线模式未拉取"}
            return {}

        cities = [c for c in CITY_STATIONS if c in CITY_ADCODE]

        def fetch():
            cli = RailwayClient()
            # 逐日查询(不止首日): 假期各天的抢票热度差异很大 —— 出城日与返程日
            # 紧张的往往是不同方向, 只看首日会漏掉返程日的高热门方向。
            daily = {d: cli.demand_by_city(d, cities) for d in dates}
            return {
                "city": daily.get(dates[0], {}),   # 首日: 供「当年热度」修正客流
                "daily": daily,                    # 逐日: 供报告展示抢票热度变化
                "curve": {d: v for d, v in cli.daily_curve("深圳北", "广州南", dates).items()},
                "fetched_at": self._now(),
            }

        data, status = self._fetch("railway", fetch, force)
        if data is None:
            self.provenance["铁路12306余票"] = {"status": "failed", "updated": "-",
                                              "detail": status}
            return {}
        n = len(data.get("city", {}))
        n_days = len(data.get("daily", {}))
        self.provenance["铁路12306余票"] = {
            "status": status, "updated": data.get("fetched_at", self._now()),
            "detail": f"假期 {n_days} 天 × {n} 个方向售罄率 + 深穗逐日曲线"}
        return data

    # ---------------- 高德实时路况 ----------------
    def live_traffic(self, force: bool = False) -> dict[str, float]:
        """走廊实时 TTI。

        主源 = 驾车路径规划(20/20 全覆盖, 不依赖道路名索引, 配额独立);
        「道路路况」(交通态势)只收录 7/20 条高速且配额低, 仅作补充覆盖。
        """
        from .amap import HIGHWAY_ROADS, AmapDriveClient, AmapTrafficClient

        key = os.getenv("AMAP_KEY") or (self.cfg.get("amap") or {}).get("key")
        if self.mode == "offline" or not key:
            self.provenance["高德实时路况"] = {
                "status": "offline" if self.mode == "offline" else "not_configured",
                "updated": "-", "detail": "未配置 AMAP_KEY, 拥堵保持模型预测"}
            return {}

        def fetch():
            tti = AmapDriveClient(key).all_tti()          # 主源: 20/20 条走廊
            for k, v in AmapTrafficClient(key).all_tti().items():
                tti[k] = v                                # 补充: 道路路况更贴近走廊本体
            return {"tti": tti, "fetched_at": self._now()}

        data, status = self._fetch("traffic", fetch, force)
        tti = (data or {}).get("tti", {})
        total = len(HIGHWAY_ROADS)
        if not tti:
            status = "failed"  # 缓存中的空响应也不能标成 live/cache
        self.provenance["高德实时路况"] = {
            "status": status, "updated": (data or {}).get("fetched_at", "-") if tti else "-",
            "detail": f"{len(tti)}/{total} 条走廊实时 TTI"
                      "（主源=驾车路径规划 tmcs 调和平均，道路路况补充覆盖）"}
        return tti

    # ---------------- 腾讯当前驾车 ETA（不代替指定高速 TTI） ----------------
    def tencent_drive(self, force: bool = False) -> pd.DataFrame:
        key = os.getenv("TENCENT_MAP_KEY")
        status = "offline" if self.mode == "offline" else "not_configured"
        data = None
        limited = False
        if self.mode != "offline" and key:
            marker, _ = self._read_cache("tencent_daily_limit", None)
            limited = isinstance(marker, dict) and marker.get("date") == today(self.tz).isoformat()
            if limited and not force:
                data, _ = self._read_cache("tencent_drive", None)
                status = "stale" if data else "quota_exhausted"
            else:
                def fetch():
                    try:
                        routes = _tencent_routes(key)
                    except TencentDailyQuotaError:
                        self._write_cache("tencent_daily_limit", {"date": today(self.tz).isoformat()})
                        raise
                    if limited:
                        self._write_cache("tencent_daily_limit", {"date": "-"})
                    return {"routes": routes, "fetched_at": self._now()}

                data, status = self._fetch("tencent_drive", fetch, force)
                marker, _ = self._read_cache("tencent_daily_limit", None)
                limited = isinstance(marker, dict) and marker.get("date") == today(self.tz).isoformat()
                if limited and data is None:
                    status = "quota_exhausted"

        raw = (data or {}).get("routes", {})
        values = {}
        if isinstance(raw, dict):
            for town in TOWNS:
                route = raw.get(town.name)
                if not isinstance(route, dict):
                    continue
                eta, distance = route.get("eta_min"), route.get("distance_km")
                if (type(eta) in (int, float) and type(distance) in (int, float)
                        and math.isfinite(eta) and math.isfinite(distance)
                        and 0 < eta < 1440 and 0 < distance < 2000):
                    values[town.name] = (eta, distance)
        if data and not values:
            status = "failed"
        elif data and len(values) < len(TOWNS) and status not in ("quota_exhausted", "failed"):
            status = f"partial({status})"
        self.provenance["腾讯驾车当前 ETA"] = {
            "status": status, "updated": (data or {}).get("fetched_at", "-"),
            "detail": f"{len(values)}/{len(TOWNS)} 条深圳出发路线当前预计耗时；"
                      + ("本日额度耗尽，停止请求；" if limited else "")
                      + "非国庆未来路况、非指定高速 TTI，不参与预测"}
        return pd.DataFrame([
            {"town": t.name, "eta_min": values[t.name][0] if t.name in values else None,
             "distance_km": values[t.name][1] if t.name in values else None,
             "observed_at": (data or {}).get("fetched_at") if t.name in values else None,
             "source": "tencent_route" if t.name in values else "unavailable"}
            for t in TOWNS
        ])

    # ---------------- 住宿地点供给代理(不是 OTA 交易) ----------------
    def lodging_supply(self) -> pd.DataFrame:
        from .amap import AmapLodgingClient, LODGING_RADIUS_M, LODGING_TYPE

        key = os.getenv("AMAP_KEY") or (self.cfg.get("amap") or {}).get("key")
        status = "offline" if self.mode == "offline" else "not_configured"
        data = None
        if self.mode != "offline" and key:
            # POI 配额比路况低；即使强制刷新也不绕开 24 小时缓存。
            data, status = self._fetch("lodging", lambda: {
                "counts": AmapLodgingClient(key).all_counts(),
                "fetched_at": self._now(), "radius_m": LODGING_RADIUS_M,
                "type": LODGING_TYPE,
            })
        raw = (data or {}).get("counts", {})
        known = {t.name for t in TOWNS}
        counts = {name: value for name, value in raw.items()
                  if name in known and type(value) is int and value >= 0} if isinstance(raw, dict) else {}
        if data and len(counts) < len(TOWNS):
            status = f"partial({status})"
        capped = sum(value >= 600 for value in counts.values())
        self.provenance["高德住宿 POI 供给代理"] = {
            "status": status, "updated": (data or {}).get("fetched_at", "-"),
            "detail": f"{len(counts)}/{len(TOWNS)} 地点近 5km 住宿 POI 检索数；"
                      f"{capped} 地点返回 600（疑似接口上限，仅作下界）；"
                      "不是 OTA 房价、客房数、预订量；不参与预测"}
        return pd.DataFrame([
            {"town": t.name, "lodging_poi_count_5km": counts.get(t.name),
             "possibly_capped": counts[t.name] >= 600 if t.name in counts else None,
             "radius_m": LODGING_RADIUS_M,
             "source": "amap_poi" if t.name in counts else "unavailable"}
            for t in TOWNS
        ])

    # ---------------- 百度地图住宿关键词 POI 检索 ----------------
    def baidu_lodging_supply(self) -> pd.DataFrame:
        from .baidu import BaiduLodgingClient, PLACE_QUERY, PLACE_RADIUS_M, PLACE_TOTAL_LIMIT

        ak = os.getenv("BAIDU_MAP_AK")
        status = "offline" if self.mode == "offline" else "not_configured"
        data = None
        if self.mode != "offline" and ak:
            data, status = self._fetch("baidu_lodging", lambda: {
                "counts": BaiduLodgingClient(ak).all_counts(),
                "fetched_at": self._now(), "radius_m": PLACE_RADIUS_M,
                "query": PLACE_QUERY,
            })
        raw = (data or {}).get("counts", {})
        known = {t.name for t in TOWNS}
        counts = {name: value for name, value in raw.items()
                  if name in known and type(value) is int and value >= 0} if isinstance(raw, dict) else {}
        if data and len(counts) < len(TOWNS):
            status = f"partial({status})"
        capped = sum(value >= PLACE_TOTAL_LIMIT for value in counts.values())
        self.provenance["百度地图住宿 POI 检索代理"] = {
            "status": status, "updated": (data or {}).get("fetched_at", "-"),
            "detail": f"{len(counts)}/{len(TOWNS)} 地点近 5km 酒店/民宿/宾馆关键词检索数；"
                      f"{capped} 地点达到 {PLACE_TOTAL_LIMIT} 条接口计数上限；"
                      "与高德口径不可直接比较，不是 OTA 订单/房量或社交热度，不参与预测"}
        return pd.DataFrame([
            {"town": t.name, "baidu_lodging_poi_count_5km": counts.get(t.name),
             "possibly_capped": counts[t.name] >= PLACE_TOTAL_LIMIT if t.name in counts else None,
             "radius_m": PLACE_RADIUS_M,
             "source": "baidu_place" if t.name in counts else "unavailable"}
            for t in TOWNS
        ])

    # ---------------- 持证平台提供的导出数据(独立于模型先验) ----------------
    def partner_observations(self) -> pd.DataFrame:
        empty = pd.DataFrame(columns=PARTNER_COLUMNS)
        path = Path(self.cfg["data"].get("partner_observations_file",
                                         PROJECT_ROOT / "data/partner_observations.csv"))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        status = "offline" if self.mode == "offline" else "not_configured"
        detail = "尚未提供有使用授权的平台数据文件；模型继续使用先验"
        updated = "-"
        if self.mode != "offline" and path.is_file():
            try:
                data = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
                if set(data.columns) != set(PARTNER_COLUMNS) or data.empty or len(data) > 10000:
                    raise ValueError("字段或行数不符合要求")
                data = data[list(PARTNER_COLUMNS)]
                data["value"] = pd.to_numeric(data["value"], errors="raise")
                known = {t.name for t in TOWNS}
                for row in data.itertuples(index=False):
                    if row.town not in known or row.metric not in PARTNER_METRICS.get(row.platform, set()):
                        raise ValueError("地点、平台或指标不匹配")
                    if not math.isfinite(row.value) or row.value < 0:
                        raise ValueError("指标值无效")
                    if row.metric in ("booking_heat", "sell_out", "social_heat") and row.value > 1:
                        raise ValueError("归一化指标应在 0–1 范围内")
                    if row.metric == "price_premium" and not 1 <= row.value <= 10:
                        raise ValueError("价格溢价应在 1–10 范围内")
                    if not isinstance(row.observed_at, str):
                        raise ValueError("观测日期缺失")
                    dt.datetime.fromisoformat(row.observed_at.replace("Z", "+00:00"))
                status, detail = "user_supplied", f"导入 {len(data)} 条用户提供的授权数据；来源未核验，不参与预测"
                updated = max(data["observed_at"])
                empty = data
            except (OSError, ValueError, TypeError, pd.errors.ParserError) as e:
                logger.warning("平台授权数据文件无效: %s", type(e).__name__)
                status, detail = "failed", "授权数据文件格式或数值无效；未用于预测"
        self.provenance["平台授权数据(OTA/社交)"] = {
            "status": status, "updated": updated, "detail": detail}
        return empty

    # ---------------- 静态/先验来源(如实登记) ----------------
    def register_static_sources(self) -> None:
        self.provenance.setdefault("七普人口", {
            "status": "static", "updated": "2020", "detail": "国家统计局第七次人口普查"})
        self.provenance.setdefault("社媒热度(小红书/抖音/微信)", {
            "status": "prior", "updated": "-",
            "detail": "尚无对应全站热度的授权接口；模型仍采用人工先验和模拟演化"})
        self.provenance.setdefault("OTA 信号(携程/去哪儿/美团/飞猪)", {
            "status": "prior", "updated": "-",
            "detail": "尚无平台授权的预订/房价数据；模型指标仍由先验推导，非实时报价"})
        self.provenance.setdefault("拥堵 TTI 模型", {
            "status": "simulated", "updated": "-",
            "detail": "按高德报告口径的仿真训练; 日间形态由真实迁徙剖面驱动"})
