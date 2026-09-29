"""LightGBM 分位数集成 TTI 预测器 (P10/P50/P90) + 保形区间校准.

目标: 拥堵延时指数 TTI (高德口径: 实际行程时间 / 自由流时间, >=1).
支持 save/load, 供实时引擎在不重训的情况下用最新输入重新推理.

区间校准: 分位数回归在「与训练同分布」的数据上是校准的, 但预测目标年存在训练期
看不到的年际波动(仿真中的 year_factor, 现实中即年度出行强度差异), 直接输出 P10/P90
会系统性偏窄。故用留出年做保形分位校准 (Conformalized Quantile Regression,
Romano et al. 2019): 取校准集上 (P10 − y) 与 (y − P90) 的 (1−α/2) 分位数作为
双侧平移量, 使边际覆盖率回到名义 80%。
"""
from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

FEATURES = [
    "day_type_encoded", "holiday_offset", "hour",
    "rain_level", "temp_dev", "highway_id_encoded", "base_tti", "peak_amp",
    "direction_bias", "outflow_pressure", "hour_sin", "hour_cos",
    "highway_x_hour", "daytype_x_hour", "corridor_ota_heat",
]
CATEGORICAL = ["day_type_encoded", "highway_id_encoded"]
# 报告用中文标签; hour / hour_sin / hour_cos 同表"时段", 合并后按增益求和,
# 避免读者看到"小时"与"小时周期"两个含义重复的名目。
FEATURE_LABELS = {
    "day_type_encoded": "假期日型", "holiday_offset": "假期第几天",
    "hour": "时段", "hour_sin": "时段", "hour_cos": "时段",
    "rain_level": "降水", "temp_dev": "气温偏差", "highway_id_encoded": "路段",
    "base_tti": "平日基线", "peak_amp": "路段峰值系数", "direction_bias": "出城/返程方向",
    "outflow_pressure": "当日出行量",
    "highway_x_hour": "路段×时段", "daytype_x_hour": "日型×时段",
    "corridor_ota_heat": "线路热度",
}
QUANTILES = (0.10, 0.50, 0.90)
NOMINAL_COVERAGE = 0.80
INTERVAL_FILE = "tti_model_interval.json"


def _fname(q: float) -> str:
    return f"tti_model_p{int(q * 100):02d}.txt"


class TTIPredictor:
    def __init__(self, params: dict | None = None):
        base = {"n_estimators": 400, "learning_rate": 0.05, "num_leaves": 63,
                "min_child_samples": 20, "subsample": 0.9, "subsample_freq": 1,
                "colsample_bytree": 0.9, "verbose": -1}
        base.update(params or {})
        base.pop("objective", None)
        self.params = base
        self.boosters: dict[float, lgb.Booster] = {}
        # (下界平移, 上界平移); 未校准时为 (0, 0), 等价于原始分位数输出
        self.interval_shift: tuple[float, float] = (0.0, 0.0)
        self.interval_calibrated = False

    def fit(self, df: pd.DataFrame, y: str = "tti") -> "TTIPredictor":
        X = df[FEATURES]
        for q in QUANTILES:
            m = lgb.LGBMRegressor(objective="quantile", alpha=q, **self.params)
            m.fit(X, df[y], categorical_feature=CATEGORICAL)
            self.boosters[q] = m.booster_
        return self

    def calibrate(self, df: pd.DataFrame, y: str = "tti",
                  coverage: float = NOMINAL_COVERAGE) -> "TTIPredictor":
        """保形分位校准: 用留出年的残差修正 P10/P90 宽度, 使边际覆盖率 ≈ coverage."""
        yt = np.asarray(df[y], float)
        p50 = self._raw(0.50, df)
        p10 = np.minimum(self._raw(0.10, df), p50)
        p90 = np.maximum(self._raw(0.90, df), p50)
        q = 1.0 - (1.0 - coverage) / 2.0
        lo = -float(np.quantile(p10 - yt, q))   # P10 需向下平移的量
        hi = float(np.quantile(yt - p90, q))    # P90 需向上平移的量
        self.interval_shift = (round(lo, 4), round(max(hi, 0.0), 4))
        self.interval_calibrated = True
        return self

    def _raw(self, q: float, df: pd.DataFrame) -> np.ndarray:
        return np.clip(self.boosters[q].predict(df[FEATURES]), 1.0, None)

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self._raw(0.50, df)

    def predict_interval(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """80% 区间(保形校准后); 强制 1.0 <= P10 <= P50 <= P90(修正分位数交叉)."""
        p50 = self._raw(0.50, df)
        lo_s, hi_s = self.interval_shift
        p10 = np.minimum(self._raw(0.10, df) + lo_s, p50)
        p90 = np.maximum(self._raw(0.90, df) + hi_s, p50)
        return np.clip(p10, 1.0, None), p90

    def feature_importance(self) -> pd.Series:
        """P50 模型增益重要性, 中文标签(同义特征合并)."""
        b = self.boosters[0.50]
        s = pd.Series(b.feature_importance("gain"), index=b.feature_name())
        s.index = [FEATURE_LABELS.get(k, k) for k in s.index]
        return s.groupby(level=0).sum().sort_values(ascending=False)

    def save(self, directory: str | Path) -> None:
        d = Path(directory)
        for q, b in self.boosters.items():
            b.save_model(str(d / _fname(q)))
        (d / INTERVAL_FILE).write_text(json.dumps({
            "lo_shift": self.interval_shift[0], "hi_shift": self.interval_shift[1],
            "coverage": NOMINAL_COVERAGE, "calibrated": self.interval_calibrated,
            "method": "conformalized quantile regression (Romano et al., 2019)",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory: str | Path) -> "TTIPredictor":
        obj = cls()
        for q in QUANTILES:
            f = Path(directory) / _fname(q)
            if not f.exists():
                raise FileNotFoundError(f"模型文件缺失: {f}, 请先运行 scripts/run_pipeline.py")
            obj.boosters[q] = lgb.Booster(model_file=str(f))
        meta = Path(directory) / INTERVAL_FILE
        if meta.exists():   # 兼容未校准的旧产出物
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except ValueError:
                return obj  # 元数据损坏: 退回未校准区间, 不让服务起不来
            obj.interval_shift = (float(m.get("lo_shift", 0.0)), float(m.get("hi_shift", 0.0)))
            obj.interval_calibrated = bool(m.get("calibrated", False))
        return obj
