"""特征工程与分级口径 (训练与推理共用, 保证 train/serve 一致)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .geo import HIGHWAYS
from .holiday_calendar import DAY_TYPES

HW_INDEX = {h.id: i for i, h in enumerate(HIGHWAYS)}
HW_MAP = {h.id: h for h in HIGHWAYS}
DT_INDEX = {d: i for i, d in enumerate(DAY_TYPES)}


def build_features(df: pd.DataFrame,
                   day_flows: dict[int, tuple[float, float]] | None = None) -> pd.DataFrame:
    """df 需含: date_seq, day_type, holiday_offset, hour, highway_id, rain_level, temp_dev,
    corridor_ota_heat; 以及 flow_out/flow_in 列, 或传入 day_flows {date_seq: (超额迁出, 超额迁入)}."""
    out = df.copy()
    if day_flows is not None:
        out["flow_out"] = out["date_seq"].map(lambda s: day_flows[s][0])
        out["flow_in"] = out["date_seq"].map(lambda s: day_flows[s][1])
    out["day_type_encoded"] = out["day_type"].map(DT_INDEX).astype(int)
    out["highway_id_encoded"] = out["highway_id"].map(HW_INDEX).astype(int)
    out["base_tti"] = out["highway_id"].map(lambda i: HW_MAP[i].base_tti)
    out["peak_amp"] = out["highway_id"].map(lambda i: HW_MAP[i].peak_amp)
    out["outflow_pressure"] = out["flow_out"] + out["flow_in"]
    out["direction_bias"] = (out["flow_out"] - out["flow_in"]) / (out["outflow_pressure"] + 1e-6)
    out["hour_sin"] = np.sin(2 * np.pi * out["hour"] / 24)
    out["hour_cos"] = np.cos(2 * np.pi * out["hour"] / 24)
    out["highway_x_hour"] = out["highway_id_encoded"] * 24 + out["hour"]
    out["daytype_x_hour"] = out["day_type_encoded"] * 24 + out["hour"]
    if "corridor_ota_heat" not in out.columns:
        out["corridor_ota_heat"] = 0.45
    return out


CROWD_THRESHOLDS = (0.6, 0.85, 1.0, 1.2)


def crowding_level(idx: float) -> str:
    """游客承载率(在地游客 / 接待能力)分级: 1.0 = 接待能力用满."""
    a, b, c, d = CROWD_THRESHOLDS
    if idx < a:
        return "舒畅"
    if idx < b:
        return "正常"
    if idx < c:
        return "较拥挤"
    if idx < d:
        return "拥挤"
    return "严重拥挤"


def congestion_level(tti: float) -> str:
    """TTI 分级 (高德拥堵延时指数口径)."""
    if tti < 1.3:
        return "基本畅通"
    if tti < 1.7:
        return "轻度拥堵"
    if tti < 2.2:
        return "中度拥堵"
    if tti < 3.0:
        return "严重拥堵"
    return "极端拥堵"


def tti_hours(tti: float) -> str:
    """平时 1 小时的路程在该 TTI 下的耗时, 如 '2小时10分'."""
    m = round(tti * 60)
    return f"{m // 60}小时{m % 60:02d}分" if m >= 60 else f"{m}分钟"


def tti_plain(tti: float) -> str:
    return f"平时1小时 → 约{tti_hours(tti)}"
