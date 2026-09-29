"""OTA 在线旅游平台信号模块 (携程/去哪儿/美团/飞猪).

第一性原理定位:
  社媒热度 = 注意力(陈述性偏好, 可被流量操纵, 高噪声)
  OTA 预订 = 支付意愿(显示性偏好, 花了钱的硬意向, 低噪声) —— 信号可靠性更高
  酒店价格溢价 = 供需均衡价格(微观经济学: 溢价幅度揭示超额需求强度)

三类 OTA 信号:
  1. booking_heat  预订热度(0-1): 客房/门票提前预订量指数, 提前 1-4 周的先行指标
  2. price_premium 价格溢价(>=1): 预订均价/平日趋近价, 均衡状态的市场化度量
  3. sell_out      售罄率(0-1): 热门房源/时段售罄占比, 供给紧张度

数据源(live 模式):
  - 携程: 酒店报价 API(affiliate)、携程研究院公开报告
  - 去哪儿: 骆驼平台商家 API、去哪儿大数据研究院
  - 美团: 开放平台(酒店/门票商品库)
  - 飞猪: 淘宝客/开放平台商品接口
  聚合口径: 按平台间夜量市占率加权(携程/去哪儿系 0.42, 美团 0.31, 飞猪 0.19, 其他 0.08,
  参考 Fastdata/易观 2024 中国在线旅游市场份额).

酒店客房存量(万间): 校准自中国旅游饭店业协会星级饭店统计 + 地方文旅局
公开数据 + OTA 平台可订房源量级估计(含民宿), 是过夜游客的物理硬约束.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .geo import TOWNS

# 酒店客房存量估计(万间): 供给硬约束
HOTEL_ROOMS: dict[str, float] = {
    "惠东县(巽寮湾)": 2.8, "惠城区": 2.5, "惠阳区": 1.8, "博罗县": 1.5,
    "龙门县": 1.4, "虎门镇": 0.8, "长安镇": 0.7, "松山湖": 0.6, "塘厦镇": 0.6,
    "南沙区": 1.0, "从化区": 1.6, "增城区": 1.2, "广州番禺(长隆)": 2.6,
    "佛山顺德(美食)": 2.4, "中山市区": 2.0, "珠海市区": 3.2, "珠海横琴(长隆)": 1.6,
    "台山市(上下川岛)": 1.5, "江门开平(碉楼)": 1.0, "汕尾城区(红海湾)": 1.2,
    "汕头南澳岛": 0.8, "潮州古城": 1.5, "梅州梅县(客都)": 1.3,
    "清城区": 1.6, "英德市(英西峰林)": 1.2, "源城区(万绿湖)": 1.8,
    "韶关仁化(丹霞山)": 0.7, "韶关曲江(南华寺)": 1.0,
    "阳江海陵岛": 2.2, "郴州东江湖": 0.8, "贺州黄姚古镇": 0.3, "端州区(鼎湖山)": 1.5,
    "始兴县(车八岭)": 0.4, "韶关乳源(大峡谷)": 0.6, "清远连州(地下河)": 0.8,
    "清远连山(欧家梯田)": 0.25, "阳江阳春(凌霄岩)": 0.9, "云浮新兴(禅泉)": 0.9,
    "茂名电白(浪漫海岸)": 1.8, "珠海万山(外伶仃岛)": 0.3, "湛江市区(湖光岩)": 1.4,
}
DEFAULT_ROOMS = 0.3

# 物理参数
PARTY_PER_ROOM = 2.8     # 间/房均人数(家庭出游口径)
CAPACITY_ELASTICITY = 1.6  # 民宿/亲友家/一日游滞留的供给弹性系数


class OTASignalSource:
    """OTA 平台信号源(synthetic: 按先行预订规律校准仿真; live: 接平台 API)."""

    PLATFORM_SHARES = {"ctrip_qunar": 0.42, "meituan": 0.31, "fliggy": 0.19, "other": 0.08}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg["data"]["random_seed"] + 51)

    def fetch_town_signals(self) -> pd.DataFrame:
        """生成/拉取各目的地 OTA 三信号(行前锁定, 假期中仅小幅更新)."""
        from .social import SOCIAL_SEED, DEFAULT_SEED

        rows = []
        for t in TOWNS:
            seed = SOCIAL_SEED.get(t.name, DEFAULT_SEED)
            # 预订热度: 旅游供给 x 社媒种草共同驱动, OTA 对过夜型目的地更敏感
            booking = (0.18 + 0.38 * (t.tourism / 2.3) + 0.42 * seed
                       + self.rng.normal(0, 0.07))
            booking = float(np.clip(booking, 0.05, 0.97))
            # 价格溢价: 超额需求的市场化均衡响应
            premium = 1.0 + 2.4 * max(0.0, booking - 0.35) + abs(self.rng.normal(0, 0.08))
            premium = float(np.clip(premium, 1.0, 3.0))
            # 售罄率
            sell_out = float(np.clip(1.35 * booking - 0.28, 0.0, 1.0))
            rows.append({
                "town": t.name,
                "booking_heat": round(booking, 3),
                "price_premium": round(premium, 3),
                "sell_out": round(sell_out, 3),
                "hotel_rooms_万间": HOTEL_ROOMS.get(t.name, DEFAULT_ROOMS),
            })
        return pd.DataFrame(rows)


def ota_multiplier(booking_heat: float, beta: float = 0.55,
                   day_type: str = "mid") -> float:
    """预订热度 -> 客流权重调制系数(显示性偏好通道).

    day_type 修正: 订房集中在入住首夜(D+1..D+3), 返程日退房离场.
    """
    day_fac = {"depart": 1.08, "mid": 1.0, "return_early": 0.88,
               "return_last": 0.82, "pre": 1.0, "post": 0.9}.get(day_type, 1.0)
    return max(0.35, 1.0 + beta * (booking_heat - 0.45)) * day_fac


def hotel_capacity(town_name: str) -> float:
    """目的地单日可容纳过夜+弹性滞留游客(万人) —— 物理硬约束."""
    return HOTEL_ROOMS.get(town_name, DEFAULT_ROOMS) * PARTY_PER_ROOM * CAPACITY_ELASTICITY

