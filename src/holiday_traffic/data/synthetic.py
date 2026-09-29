"""TTI 仿真训练数据 (高德无公开历史接口时的替代; 结果须标注为仿真).

生成机理(乘性分解, 保证量纲与物理边界):
  TTI = 平日基线 × 日内形态 × (1 + (假期峰值系数 − 1) × 假期压力) × 观测噪声

  平日基线    hw.base_tti, 高德口径(实际行程时间 / 自由流时间), 全天均值
  日内形态    广州 214 路段实测车速(2016, 中山大学, GitHub: sysuits/urban-traffic-
              speed-dataset-Guangzhou)折算的逐时拥堵系数(周末形态), 归一后均值 1:
              夜间 ≈0.83, 午后峰 ≈1.18 —— 保证夜间回到近自由流
  假期压力    u = 当日强度 × 时段形状 × 走廊需求 × 天气 × 年际波动, clip 到 [0, 1.3]
              · 当日强度 ∝ 当日假期诱发流量(超额迁出+超额迁入, 百度慧眼真实剖面), ∈ [0.3, 1]
              · 时段形状  = 出城/返程峰高斯(10 时 / 18 时)叠加实测日内起伏, 归一化峰值 1,
                夜间地板 0.05 —— 出城日按出城/返程流量比线性混合两种形态
              · 走廊需求 = 0.85 + 0.30 × OTA 走廊热度
              · 天气     降雨 +6%/级, 气温偏差 +1%/℃
              · 年际波动 ±6%(模型不可见, 由保形分位校准吸收)
  观测噪声    ±6%

由此得到: 夜间 u→0.05 时 TTI ≈ 平日基线 × 0.83 ≈ 1.2(基本畅通);
峰值日峰值时 u→1 时 TTI ≈ 平日基线 × 1.08 × 峰值系数 ≈ 2.8–3.2(严重拥堵).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..geo import HIGHWAYS

HOURS = np.arange(24)
_SHAPE_PATH = Path(__file__).resolve().parents[1] / "reference" / "hourly_shape.json"
_SHAPE = json.loads(_SHAPE_PATH.read_text(encoding="utf-8")) if _SHAPE_PATH.exists() else None
_RAW_SHAPE = np.array(_SHAPE["weekend"]) if _SHAPE else np.ones(24)  # 假期用周末形态
BASE_SHAPE = _RAW_SHAPE / _RAW_SHAPE.mean()                          # 日内形态(均值=1)
_DIURNAL = (_RAW_SHAPE - _RAW_SHAPE.min()) / max(_RAW_SHAPE.max() - _RAW_SHAPE.min(), 1e-9)

NIGHT_FLOOR = 0.05          # 时段形状的夜间地板(夜间仍有少量货车/夜间出行)
DIURNAL_WEIGHT = 0.45       # 实测日内起伏在时段形状中的权重
DEPART_PEAK = (10.0, 2.2)   # 出城峰: 上午 10 时(节前通勤前移)
RETURN_PEAK = (18.0, 2.4)   # 返程峰: 傍晚 18 时(实测全天最堵)
DAY_FLOOR = 0.30            # 当日强度下限(假期最清淡日仍有基础出行)
PRESSURE_CAP = 1.30         # 假期压力上限(避免极端外生叠加时 TTI 失真)
YEAR_SIGMA = 0.06           # 年际波动(模型不可见)
NOISE_SIGMA = 0.06          # 观测噪声


def _gauss(x, mu, sigma):
    return np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def hour_shape(kind: str) -> np.ndarray:
    """归一化(峰值=1)的假期时段需求形状: 出城型上午达峰, 返程型傍晚达峰."""
    mu, sigma = DEPART_PEAK if kind == "depart" else RETURN_PEAK
    raw = NIGHT_FLOOR + DIURNAL_WEIGHT * _DIURNAL + _gauss(HOURS, mu, sigma)
    return raw / raw.max()


DEPART_SHAPE = hour_shape("depart")
RETURN_SHAPE = hour_shape("return")


class SyntheticTrafficSource:
    def __init__(self, seed: int, corridor_heat: dict[str, float]):
        self.rng = np.random.default_rng(seed)
        self.corridor_heat = corridor_heat

    def generate(self, year: int, day_flows: list[tuple[float, float]],
                 weather: pd.DataFrame) -> pd.DataFrame:
        totals = [o + i for o, i in day_flows]
        peak = max(totals) or 1.0
        year_factor = self.rng.normal(1.0, YEAR_SIGMA)
        frames = []
        for seq, (f_out, f_in) in enumerate(day_flows):
            intensity = DAY_FLOOR + (1.0 - DAY_FLOOR) * totals[seq] / peak
            s_out = f_out / (f_out + f_in + 1e-9)
            shape = s_out * DEPART_SHAPE + (1 - s_out) * RETURN_SHAPE
            w = weather.iloc[seq]
            ext = (1.0 + 0.06 * w["rain_level"]) * (1.0 + 0.01 * abs(w["temp_dev"]))
            for hw in HIGHWAYS:
                demand = 0.85 + 0.30 * self.corridor_heat.get(hw.id, 0.45)
                pressure = np.clip(intensity * shape * demand * ext * year_factor,
                                   0.0, PRESSURE_CAP)
                tti = hw.base_tti * BASE_SHAPE * (1.0 + (hw.peak_amp - 1.0) * pressure) \
                    * self.rng.normal(1.0, NOISE_SIGMA, size=24)
                frames.append(pd.DataFrame({
                    "year": year, "date_seq": seq, "highway_id": hw.id,
                    "hour": HOURS, "tti": np.round(np.maximum(1.0, tti), 3),
                    "corridor_ota_heat": round(self.corridor_heat.get(hw.id, 0.45), 3),
                }))
        return pd.concat(frames, ignore_index=True)


class SyntheticWeatherSource:
    """历史训练期天气: 珠三角 10 月降水日约 25%."""

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)

    def generate(self, n_days: int) -> pd.DataFrame:
        rain = [int(self.rng.choice([1, 1, 2, 3], p=[0.55, 0.25, 0.15, 0.05]))
                if self.rng.random() < 0.25 else 0 for _ in range(n_days)]
        return pd.DataFrame({"rain_level": rain,
                             "temp_dev": np.round(self.rng.normal(0, 1.5, n_days), 2)})
