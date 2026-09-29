"""辐射模型 (Radiation Model, Simini et al., Nature 2012).

无参数人口流动模型, 仅需人口分布与地理距离即可给出 OD 结构先验:
  T_ij ∝ m_i * m_j / ((m_i + s_ij) * (m_i + m_j + s_ij))
  s_ij: 与深圳距离不超过 d_ij 的其它目的地人口之和(竞争机会).
本项目中仅作回测对照基线: 其城市级误差远大于直接使用百度真实去向占比, 不参与预测.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..geo import TOWNS, distance_shenzhen


class RadiationModel:
    def __init__(self, cfg: dict):
        self.origin_pop = cfg["project"]["origin_population"]

    def weights(self) -> pd.Series:
        """深圳 -> 各目的地流量占比(和为 1), 含旅游吸引力与自驾距离衰减修正."""
        m_i = self.origin_pop
        pops = np.array([t.population for t in TOWNS])
        dists = np.array([distance_shenzhen(t) for t in TOWNS])
        raw = {}
        for k, town in enumerate(TOWNS):
            mask = dists <= dists[k] + 1e-6
            mask[k] = False
            s_ij = pops[mask].sum()
            m_j = town.population
            flux = m_i * m_j / ((m_i + s_ij) * (m_i + m_j + s_ij))
            raw[town.name] = flux * np.exp(-dists[k] / 260.0) * town.tourism
        s = pd.Series(raw)
        return s / s.sum()
