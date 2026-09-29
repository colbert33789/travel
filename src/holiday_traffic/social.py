"""社交媒体热度动力学 (种草-拔草).

  种草(行前): 初始热度 H0 = 公开榜单标定的先验(小红书/抖音/微信无公开 API)
  假期(逐日): H(t+1) = H + α·H·(1−H/K) − γ·max(0, C−1)·H   [逻辑发酵 − 拥挤负面曝光; C=游客承载率]
  客流调制:  m(t) = 1 + η·(H(t) − 0.5)

平台加权口径(接入商业数据时): 0.45·小红书 + 0.35·抖音 + 0.20·微信.
"""
from __future__ import annotations

import numpy as np

# 行前种草热度初值 H0 (0-1), 校准自小红书/抖音目的地种草内容体量排序:
# 滨海度假、古城美食、主题乐园类内容霸榜; 2024 国庆爆款: 潮州、海陵岛、
# 各地长隆、顺德美食、阿勒泰式"平替海岛"。工业镇/纯通勤目的地热度低。
SOCIAL_SEED: dict[str, float] = {
    "潮州古城": 0.88, "阳江海陵岛": 0.85, "珠海横琴(长隆)": 0.82,
    "惠东县(巽寮湾)": 0.78, "佛山顺德(美食)": 0.76, "广州番禺(长隆)": 0.74,
    "汕头南澳岛": 0.71, "江门开平(碉楼)": 0.64, "汕尾城区(红海湾)": 0.62,
    "贺州黄姚古镇": 0.58, "郴州东江湖": 0.55, "韶关仁化(丹霞山)": 0.54,
    "台山市(上下川岛)": 0.52, "珠海市区": 0.50, "端州区(鼎湖山)": 0.48,
    "源城区(万绿湖)": 0.46, "梅州梅县(客都)": 0.44, "韶关曲江(南华寺)": 0.42,
    "韶关乳源(大峡谷)": 0.50, "珠海万山(外伶仃岛)": 0.58, "茂名电白(浪漫海岸)": 0.48,
    "清远连州(地下河)": 0.45, "云浮新兴(禅泉)": 0.44, "清远连山(欧家梯田)": 0.42,
    "始兴县(车八岭)": 0.38, "阳江阳春(凌霄岩)": 0.32,
    "湛江市区(湖光岩)": 0.40,
    "从化区": 0.40, "龙门县": 0.38, "博罗县": 0.36, "英德市(英西峰林)": 0.35,
    "清城区": 0.34, "惠城区": 0.33, "中山市区": 0.30, "南沙区": 0.28,
    "增城区": 0.26, "松山湖": 0.24, "惠阳区": 0.22, "虎门镇": 0.20,
    "塘厦镇": 0.14, "长安镇": 0.12,
}
DEFAULT_SEED = 0.25


class SocialDynamicsModel:
    """假期逐日社交媒体热度演化 + 客流调制系数."""

    def __init__(self, cfg: dict, alpha: float = 0.55, gamma: float = 0.80,
                 eta: float = 0.40, carrying: float = 0.95):
        self.cfg = cfg
        self.alpha = alpha      # 内容发酵速率(抖音/小红书日传播强度)
        self.gamma = gamma      # 拥挤负面反馈(拔草)强度
        self.eta = eta          # 热度->客流转导系数
        self.K = carrying       # 热度饱和容量
        self.rng = np.random.default_rng(cfg["data"]["random_seed"] + 29)

    def seed(self, town_name: str) -> float:
        base = SOCIAL_SEED.get(town_name, DEFAULT_SEED)
        return float(np.clip(base * self.rng.normal(1.0, 0.06), 0.02, 0.98))

    def multiplier(self, heat: float) -> float:
        return 1.0 + self.eta * (heat - 0.5)

    def step(self, heat: float, crowding: float) -> float:
        """单日演化: 内容发酵 − 拥挤负面曝光(承载率超过 1.0 即「人从众」, 次日生效)."""
        growth = self.alpha * heat * (1 - heat / self.K)
        backlash = self.gamma * max(0.0, crowding - 1.0) * heat
        return float(np.clip(heat + growth - backlash, 0.01, 0.99))
