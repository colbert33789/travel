"""理想目的地推荐.

推荐分 = 游玩性(0-100) × exp(−1.2·max(0, 承载率−0.85))        体验(拥挤折减)
        − 45 × 车程h / (车程h + 3)                             时间成本(饱和, 上限 45)
        − 10 × max(0, 路线TTI − 1.3)                           路况成本
        − 10 × max(0, 酒店溢价 − 1)                            价格成本
        − 5  × 降水等级 × 自然景观占比/5                        天气成本(户外型雨天打折)

车程 = 实测平日耗时 ÷ (1 + 0.5×(平日TTI − 1)) × (1 + 0.5×(路线TTI − 1))
      · 里程与平日耗时用高德驾车路径规划实测(geo.ROAD_MEASURED), 不用球面距离估算:
        珠江西岸必须绕珠江口, 球面×1.3 会低估珠海市区 34km、珠海万山 51km。
      · 实测耗时含平日拥堵, 先除以平日拥堵系数反推自由流, 再乘假期拥堵系数。
      · 拥堵延时只作用于易堵段落(出入城 + 瓶颈, 取 50%), 其余按自由流,
        否则 300km 行程会被整程按拥堵车速折算, 车程与扣分严重高估。

车程扣分取饱和形式 h/(h+3): 边际厌恶递减(3h 与 4h 的体验差距 < 1h 与 2h),
且保证扣分有界, 推荐分不会被远途目的地主导为全部负值。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data.openmeteo import RAIN_LABEL
from .geo import HIGHWAYS, TOWN_INDEX, TOWNS, Town, WEEKDAY_TTI, road_distance

DIM_WEIGHTS = {"natural": 0.30, "culture": 0.18, "fun": 0.22, "food": 0.18, "family": 0.12}
CONGESTION_SHARE = 0.5     # 行程中受拥堵影响的比例
DRIVE_PENALTY_CAP = 45.0   # 车程扣分上限
DRIVE_HALF_HOURS = 3.0     # 车程扣分半饱和点(小时)

TOWN_HIGHWAYS: dict[str, list[str]] = {t.name: [] for t in TOWNS}
for _hw in HIGHWAYS:
    for _t in _hw.connected_towns:
        TOWN_HIGHWAYS[_t].append(_hw.id)


def playability_score(town: Town) -> dict:
    """五维游玩性(1-5) -> 0-100."""
    dims = {k: getattr(town, k) for k in DIM_WEIGHTS}
    total = sum(dims[k] * w for k, w in DIM_WEIGHTS.items()) / 5.0
    return {"score": round(total * 100, 1), **dims}


def _reasons(town: Town, crowd: float, premium: float, rain: int) -> str:
    tags = [label for attr, label in (("natural", "自然风光"), ("culture", "人文古迹"),
                                      ("fun", "玩乐丰富"), ("food", "美食"),
                                      ("family", "亲子友好"))
            if getattr(town, attr) >= 4]
    if crowd < 0.6:
        tags.append("人少")
    elif crowd >= 1.0:
        tags.append("⚠️人多")
    if premium > 1.8:
        tags.append(f"⚠️房价{premium:.1f}倍")
    if rain >= 2 and town.natural >= 4:
        tags.append(f"🌧{RAIN_LABEL[rain]}户外打折")
    return " · ".join(tags) or "综合均衡"


def trip_rank(rec: pd.DataFrame, dates: list[str], province_only: bool = False,
              trip_days: int | None = None) -> pd.DataFrame:
    """把逐日推荐聚合为「多日行程」排名(适合 2-3 天不赶路的自驾游)。

    dates     : 行程可能覆盖的日期窗口(取天气/拥挤度的最不利一天)
    trip_days : 实际行程天数, 用于分摊车程。与窗口解耦 —— 例如窗口 10/2–10/5
                覆盖"10/2 或 10/3 出发"两种安排, 而行程本身是 3 天。

    与逐日榜的三处关键差异 —— 逐日榜隐含"当天往返"假设, 直接拿它规划多日游会失真:
      · 车程按天分摊: 多日游只去一次(去 + 回 = 2 个单程), 摊到 trip_days 天后
        日均车程 = 2×单程/trip_days, 远距离目的地不再被逐日榜系统性低估
        (逐日榜每天扣一次往返车程, 3 天相当于 6 个单程)
      · 天气取窗口内最差日: 自驾遇到一天大雨就会明显影响体验
      · 拥挤度取窗口内最挤日: 同理取最不利的一天
    """
    from .geo import TOWN_INDEX, in_guangdong

    sub = rec[rec["date"].isin(dates)]
    if province_only:
        sub = sub[sub["town"].map(lambda n: in_guangdong(TOWN_INDEX[n]))]
    if not len(sub):
        return pd.DataFrame()
    days = max(1, trip_days or len(dates))

    def agg(town, g):
        play = g["playability"].iloc[0]
        hours = g["drive_hours"].mean()
        crowd = g["crowding_index"].max()
        rain = g["rain_level"].max()
        tti = g["route_tti"].max()
        prem = g["price_premium"].iloc[0]
        t = TOWN_INDEX[town]
        per_day = 2.0 * hours / days          # 多日游: 去+回摊到每天
        score = (play * np.exp(-1.2 * max(0.0, crowd - 0.85))
                 - DRIVE_PENALTY_CAP * per_day / (per_day + DRIVE_HALF_HOURS)
                 - 10.0 * max(0.0, tti - 1.3)
                 - 10.0 * max(0.0, prem - 1.0)
                 - rain * t.natural)
        return {
            "town": town, "city": g["city"].iloc[0], "playability": play,
            "drive_hours": round(hours, 1), "per_day_hours": round(per_day, 1),
            "crowding_index": round(crowd, 3), "crowding_level": g["crowding_level"].iloc[0],
            "rain_max": int(rain), "route_tti": round(tti, 2),
            "price_premium": round(prem, 2), "trip_score": round(score, 1),
        }

    # 显式迭代而非 groupby.apply(后者在 pandas 2.x 会告警且行为将变更)
    rows = [agg(town, g) for town, g in sub.groupby("town", sort=False)]
    return (pd.DataFrame(rows).sort_values("trip_score", ascending=False)
            .reset_index(drop=True))


def recommend(crowding: pd.DataFrame, traffic: pd.DataFrame,
              weather: pd.DataFrame | None = None) -> pd.DataFrame:
    """逐日全量排名(daily_rank 从 1 开始)."""
    peak = traffic[traffic["hour"].between(9, 16)]
    route = peak.groupby(["date", "highway_id"])["tti_pred"].mean()
    rain = {}
    if weather is not None:
        rain = {(r.place, r.date): int(r.rain_level) for r in weather.itertuples()}

    rows = []
    for r in crowding.itertuples():
        town = TOWN_INDEX[r.town]
        play = playability_score(town)["score"]
        ttis = [route.get((r.date, h)) for h in TOWN_HIGHWAYS[town.name]]
        ttis = [t for t in ttis if t is not None]
        route_tti = float(np.mean(ttis)) if ttis else 1.0
        dist, base_hours = road_distance(town)
        # 实测平日耗时 -> 反推自由流 -> 叠加假期拥堵(仅作用于易堵段落)
        free_hours = base_hours / (1.0 + CONGESTION_SHARE * (WEEKDAY_TTI - 1.0))
        hours = free_hours * (1.0 + CONGESTION_SHARE * max(0.0, route_tti - 1.0))
        rain_lvl = rain.get((town.name, r.date), 0)
        score = (play * np.exp(-1.2 * max(0.0, r.crowding_index - 0.85))
                 - DRIVE_PENALTY_CAP * hours / (hours + DRIVE_HALF_HOURS)
                 - 10.0 * max(0.0, route_tti - 1.3)
                 - 10.0 * max(0.0, r.price_premium - 1.0)
                 - 5.0 * rain_lvl * town.natural / 5)
        rows.append({
            "date": r.date, "town": town.name, "city": town.city,
            "playability": play, "crowding_index": round(r.crowding_index, 3),
            "crowding_level": r.crowding_level,
            "distance_km": round(dist), "drive_hours": round(hours, 1),
            "route_tti": round(route_tti, 2), "price_premium": round(r.price_premium, 2),
            "rain_level": rain_lvl, "recommend_score": round(score, 1),
            "reason": _reasons(town, r.crowding_index, r.price_premium, rain_lvl),
        })
    df = pd.DataFrame(rows).sort_values(["date", "recommend_score"], ascending=[True, False])
    df["daily_rank"] = df.groupby("date").cumcount() + 1
    return df.reset_index(drop=True)
