"""回归评估指标."""
from __future__ import annotations

import numpy as np
import pandas as pd


def regression_metrics(y_true, y_pred) -> dict:
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    err = y_true - y_pred
    peak = y_true >= 1.7
    return {
        "MAE": round(float(np.mean(np.abs(err))), 4),
        "RMSE": round(float(np.sqrt(np.mean(err ** 2))), 4),
        "MAPE_%": round(float(np.mean(np.abs(err) / np.maximum(y_true, 1e-6)) * 100), 2),
        "peak_hit_rate": round(float((peak & (y_pred >= 1.7)).sum() / peak.sum()), 4)
        if peak.any() else None,
    }


def interval_coverage(y_true, lower, upper) -> float:
    y = np.asarray(y_true, float)
    return round(float(((y >= lower) & (y <= upper)).mean()), 4)


def per_highway_metrics(df: pd.DataFrame) -> dict:
    out = {n: regression_metrics(g["tti"], g["tti_pred"])["MAPE_%"]
           for n, g in df.groupby("highway_name")}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
