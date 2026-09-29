"""迁徙规模与结构模型 (以百度慧眼真实数据驱动, 可回测).

总量层: 规模指数 = 预测年节前基线 × 假期日剖面(相对基线的倍数)
  - 迁出剖面按假期首日对齐 (D0=节前一天 ... Dn)
  - 迁入剖面按假期末日对齐 (返程高峰锚定末日, 兼容 7/8 天不同假期长度)
  - 剖面取参考年(2024/2025 国庆)均值, 区间取参考年极值 -> 诚实的不确定性
  - 节前基线窗口 09-01~09-20: 避开中秋(2026-09-25)与节前提前出行
结构层: 假期 D1-D3 各城市迁出占比(cityrank), 参考年均值
实时层(nowcast): 已发布的真实日值直接替换预测, 并按最近偏差衰减修正后续日

回测: 留一年交叉验证(用 A 年剖面 + B 年真实基线预测 B 年逐日指数).
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

# 国务院办公厅放假安排: 2024 国庆 7 天; 2025 国庆+中秋 8 天
REFERENCE_HOLIDAYS: dict[int, tuple[str, int]] = {
    2024: ("2024-10-01", 7),
    2025: ("2025-10-01", 8),
}
BASELINE_WINDOW = ((9, 1), (9, 20))
NOWCAST_DECAY = 0.6


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _key(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def baseline(curve: dict[str, float], year: int) -> float | None:
    (m1, d1), (m2, d2) = BASELINE_WINDOW
    a, b = dt.date(year, m1, d1), dt.date(year, m2, d2)
    vals = [curve[_key(a + dt.timedelta(i))] for i in range((b - a).days + 1)
            if _key(a + dt.timedelta(i)) in curve]
    return statistics.mean(vals) if len(vals) >= 10 else None


def _window(year: int, n_days: int) -> list[dt.date]:
    """参考年假期窗口 -> 目标年 k=0..n_days+1 的日期序列(两端对齐 + 中段压缩).

    参考年假期长度可能不等于目标年(2024 国庆 7 天 / 2025 国庆+中秋 8 天, 目标 2026 为 7 天)。
    锚点: k=0 = 节前一天, k=n_days = 假期末日, k=n_days+1 = 节后首日;
    长度差落在假期中段(曲线最平缓处), 保证日期严格递增、无重复、无缺口累积。

    旧的「单一端对齐」有缺陷: 仅末日对齐时, 8 天假期的 k=0 会落到假期首日(10-01)
    而非节前一天, 与 7 天假期参考年(k=0=09-30)语义不一致; 两端对齐则同时锁住
    首日(出城峰)与末日(返程峰)。
    """
    start, length = REFERENCE_HOLIDAYS[year]
    s = _d(start)
    e = s + dt.timedelta(length - 1)
    last = max(1, n_days)
    seq: list[dt.date] = []
    prev: dt.date | None = None
    for k in range(n_days + 2):
        if k == 0:
            day = s - dt.timedelta(1)
        elif k == last:
            day = e
        elif k == last + 1:
            day = e + dt.timedelta(1)
        else:
            # 参考年 length 天映射到目标年 last 天; length < last 时会出现重复日,
            # 由下面的严格递增约束兜底(顺延一天), 避免出现同一天取两次。
            j = min(max(round(k * length / last), 1), length)
            day = s + dt.timedelta(j - 1)
        if prev is not None and day <= prev:
            day = prev + dt.timedelta(1)
        seq.append(day)
        prev = day
    return seq


def _ratios(curve: dict[str, float], year: int, n_days: int,
            align: str = "end") -> list[float] | None:
    """单个参考年的比值剖面. align=start 取 D0..Dn, align=end 取 D0..Dn+1(含节后首日)."""
    base = baseline(curve, year)
    if not base:
        return None
    win = _window(year, n_days)
    out = []
    for k in range(n_days + 2 if align == "end" else n_days + 1):
        v = curve.get(_key(win[k]))
        if v is None:
            return None
        out.append(v / base)
    return out


@dataclass
class MigrationProfile:
    dates: list[str]                 # D1..Dn
    out_ratio: list[float]           # D0..Dn
    out_low: list[float]
    out_high: list[float]
    in_ratio: list[float]            # D0..Dn+1
    in_low: list[float]
    in_high: list[float]
    baseline_out: float
    baseline_in: float
    city_shares: dict[str, float]    # 假期城市迁出占比(%)
    ref_years: list[int]
    observed_out: dict[str, float] = field(default_factory=dict)  # 实测指数(nowcast)
    observed_in: dict[str, float] = field(default_factory=dict)
    source: str = "snapshot"

    # ---- 逐日指数 (含 D0) ----
    def out_index(self) -> list[float]:
        return [r * self.baseline_out for r in self.out_ratio]

    def in_index(self) -> list[float]:
        return [r * self.baseline_in for r in self.in_ratio]

    def day_flows(self, people_per_index: float) -> list[tuple[float, float]]:
        """D0..Dn 的假期诱发流量(万人次): (超额迁出, 超额迁入); 超额=高于基线部分."""
        oi, ii = self.out_index(), self.in_index()
        return [(max(0.0, oi[k] - self.baseline_out) * people_per_index,
                 max(0.0, ii[k] - self.baseline_in) * people_per_index)
                for k in range(len(self.dates) + 1)]

    def post_inflow(self, people_per_index: float) -> float:
        """节后首日超额迁入(万人次), 用于守恒配平."""
        return max(0.0, self.in_index()[-1] - self.baseline_in) * people_per_index


def build_profile(out_curve: dict[str, float], in_curve: dict[str, float],
                  city_ranks: dict[str, dict[str, float]],
                  target_start: str, n_days: int, source: str) -> MigrationProfile:
    target_year = _d(target_start).year
    outs, ins, years = [], [], []
    for year, (start, length) in REFERENCE_HOLIDAYS.items():
        if year >= target_year:
            continue
        o = _ratios(out_curve, year, n_days, "start")
        i = _ratios(in_curve, year, n_days, "end")
        if o and i:
            outs.append(o), ins.append(i), years.append(year)
    if not outs:
        raise ValueError("无可用参考年迁徙数据")

    def agg(rows, fn):
        return [round(fn(col), 4) for col in zip(*rows)]

    b_out = baseline(out_curve, target_year) or statistics.mean(
        baseline(out_curve, y) for y in years)
    b_in = baseline(in_curve, target_year) or statistics.mean(
        baseline(in_curve, y) for y in years)

    # 结构层: 参考年 D1-D3 城市占比均值
    shares: dict[str, list[float]] = {}
    for year in years:
        s = _d(REFERENCE_HOLIDAYS[year][0])
        for k in range(3):
            for city, v in city_ranks.get(_key(s + dt.timedelta(k)), {}).items():
                shares.setdefault(city, []).append(v)
    n_obs = max((len(v) for v in shares.values()), default=1)
    city_shares = {c: round(sum(v) / n_obs, 3) for c, v in shares.items()}

    dates = [(_d(target_start) + dt.timedelta(k)).isoformat() for k in range(n_days)]
    prof = MigrationProfile(
        dates=dates,
        out_ratio=agg(outs, statistics.mean), out_low=agg(outs, min), out_high=agg(outs, max),
        in_ratio=agg(ins, statistics.mean), in_low=agg(ins, min), in_high=agg(ins, max),
        baseline_out=round(b_out, 4), baseline_in=round(b_in, 4),
        city_shares=city_shares, ref_years=years, source=source,
    )
    return nowcast(prof, out_curve, in_curve)


def nowcast(prof: MigrationProfile, out_curve: dict[str, float],
            in_curve: dict[str, float]) -> MigrationProfile:
    """用已发布的真实日值替换预测, 并把最近偏差按指数衰减传递给后续日.

    不确定性区间同步处理: 已实测日区间收窄为该点(不再有不确定性),
    后续日的上下界按与均值相同的衰减系数平移, 避免实测点落在自己的区间之外。
    """
    d0 = _d(prof.dates[0]) - dt.timedelta(1)
    for curve, ratios, low, high, observed, base in (
        (out_curve, prof.out_ratio, prof.out_low, prof.out_high,
         prof.observed_out, prof.baseline_out),
        (in_curve, prof.in_ratio, prof.in_low, prof.in_high,
         prof.observed_in, prof.baseline_in),
    ):
        last_bias, last_k = None, None
        for k in range(len(ratios)):
            day = d0 + dt.timedelta(k)
            v = curve.get(_key(day))
            if v is not None and base:
                pred = ratios[k]
                r = round(v / base, 4)
                ratios[k] = low[k] = high[k] = r
                observed[day.isoformat()] = round(v, 3)
                last_bias, last_k = r / pred if pred else 1.0, k
        if last_bias is not None:
            for k in range(last_k + 1, len(ratios)):
                adj = 1 + (last_bias - 1) * NOWCAST_DECAY ** (k - last_k)
                ratios[k] = round(ratios[k] * adj, 4)
                low[k] = round(low[k] * adj, 4)
                high[k] = round(high[k] * adj, 4)
    return prof


def backtest(out_curve: dict[str, float], in_curve: dict[str, float],
             n_days: int = 7) -> dict:
    """留一年交叉验证: 用其它参考年剖面 + 本年真实基线, 预测本年逐日规模指数."""
    per_year = {}
    all_out_err, all_in_err = [], []
    for year, (start, length) in REFERENCE_HOLIDAYS.items():
        others = [(y, v) for y, v in REFERENCE_HOLIDAYS.items() if y != year]
        o_true = _ratios(out_curve, year, n_days, "start")
        i_true = _ratios(in_curve, year, n_days, "end")
        o_pred = [_ratios(out_curve, y, n_days, "start") for y, _ in others]
        i_pred = [_ratios(in_curve, y, n_days, "end") for y, _ in others]
        o_pred = [p for p in o_pred if p]
        i_pred = [p for p in i_pred if p]
        if not (o_true and i_true and o_pred and i_pred):
            continue
        o_hat = [statistics.mean(c) for c in zip(*o_pred)]
        i_hat = [statistics.mean(c) for c in zip(*i_pred)]
        oe = [abs(p - t) / t for p, t in zip(o_hat, o_true)]
        ie = [abs(p - t) / t for p, t in zip(i_hat, i_true)]
        all_out_err += oe
        all_in_err += ie
        peak_t = max(range(len(o_true)), key=o_true.__getitem__)
        peak_p = max(range(len(o_hat)), key=o_hat.__getitem__)
        per_year[year] = {
            "out_MAPE_%": round(100 * statistics.mean(oe), 1),
            "in_MAPE_%": round(100 * statistics.mean(ie), 1),
            "peak_day_hit": peak_t == peak_p,
        }
    if not per_year:
        return {}
    return {
        "method": "留一年交叉验证(2024↔2025 国庆, 百度慧眼真实迁徙规模指数)",
        "out_MAPE_%": round(100 * statistics.mean(all_out_err), 1),
        "in_MAPE_%": round(100 * statistics.mean(all_in_err), 1),
        "peak_day_hit_rate": round(
            sum(v["peak_day_hit"] for v in per_year.values()) / len(per_year), 2),
        "per_year": per_year,
    }


# ====================== 目的地城市(全来源)客流 ======================

def _hybrid(curve: dict[str, float], year: int, n_days: int) -> list[float] | None:
    """两端对齐的比值剖面(兼容 7/8 天假期), k=0..n+1. 与 _ratios(align="end") 同源."""
    return _ratios(curve, year, n_days, align="end")


def _series(curve: dict[str, float], target_start: str, n_days: int) -> tuple[list[float], float, list[str]]:
    """目标年逐日指数预测(D0..Dn+1) + 实测替换(nowcast). 返回 (指数, 基线, 已观测日期)."""
    target_year = _d(target_start).year
    ratios = [r for y, (s, n) in REFERENCE_HOLIDAYS.items() if y < target_year
              for r in [_hybrid(curve, y, n_days)] if r]
    if not ratios:
        raise ValueError("目的地无可用参考年数据")
    mean_r = [statistics.mean(c) for c in zip(*ratios)]
    base = baseline(curve, target_year) or statistics.mean(
        baseline(curve, y) for y in REFERENCE_HOLIDAYS if y < target_year)
    idx = [r * base for r in mean_r]
    d0 = _d(target_start) - dt.timedelta(1)
    observed, last = [], None
    for k in range(len(idx)):
        day = d0 + dt.timedelta(k)
        v = curve.get(_key(day))
        if v is not None:
            last = (k, v / idx[k] if idx[k] else 1.0)
            idx[k] = v
            observed.append(day.isoformat())
    if last:
        k0, bias = last
        for k in range(k0 + 1, len(idx)):
            idx[k] *= 1 + (bias - 1) * NOWCAST_DECAY ** (k - k0)
    return idx, base, observed


@dataclass
class CityFlow:
    """目的地城市假期客流双存量(单位: 迁徙规模指数点, D0..Dn+1).
    visitors: 在地外来游客; locals_away: 外出本地居民."""
    city: str
    visitors: list[float]
    locals_away: list[float]
    observed_days: list[str]


MEAN_STAY_DAYS = 2.5  # 国庆外来人员平均停留(天), 存量-流量模型的平均驻留时间


def closed_stock(adds: list[float], subs: list[float]) -> list[float]:
    """守恒存量: 求返程缩放系数使期末存量归零(二分). 存量非负时早到的'返程'无效,
    简单按总量配平会留下残量, 故用二分直接满足 '走多少回多少'."""
    def run(scale):
        s, out = 0.0, []
        for a, b in zip(adds, subs):
            s = max(0.0, s + a - b * scale)
            out.append(s)
        return out

    if sum(subs) <= 0:
        return run(0.0)
    lo, hi = 0.0, 1.0
    while run(hi)[-1] > 1e-6 and hi < 1e4:
        hi *= 2
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if run(mid)[-1] > 1e-6 else (lo, mid)
    return run(hi)


def build_city_flow(city: str, in_curve: dict[str, float], out_curve: dict[str, float],
                    target_start: str, n_days: int) -> CityFlow:
    """双存量(时序识别): 超额迁入前段主要是外来到达、后段主要是本地人返回; 超额迁出反之,
    权重 φ_k = k/(n+1) 线性过渡.
      外来人口 V_k = V_{k-1}·(1 − 1/平均停留) + 外来到达_k     (存量-流量, 平均驻留 2.5 天)
      外出本地人 L_k: 离开/返回按守恒配平(走多少回多少)
    外来存量不依赖迁出侧拆分 —— 客源型大城市(广州/东莞)净流出很大, 拆分迁出会淹没游客信号.
    """
    in_idx, b_in, obs = _series(in_curve, target_start, n_days)
    out_idx, b_out, _ = _series(out_curve, target_start, n_days)
    m = n_days + 1
    phi = [k / m for k in range(m + 1)]
    arrivals = [max(0.0, v - b_in) * (1 - p) for v, p in zip(in_idx, phi)]
    l_dep = [max(0.0, v - b_out) * (1 - p) for v, p in zip(out_idx, phi)]
    l_ret = [max(0.0, v - b_in) * p for v, p in zip(in_idx, phi)]
    keep = 1 - 1 / MEAN_STAY_DAYS
    visitors, vk = [], 0.0
    for a in arrivals:
        vk = vk * keep + a
        visitors.append(vk)
    return CityFlow(city, visitors, closed_stock(l_dep, l_ret), obs)


def city_backtest(dest: dict[str, dict[str, dict[str, float]]], n_days: int = 7) -> dict:
    """目的地城市迁入指数留一年验证(20 城平均)."""
    errs = []
    for curves in dest.values():
        c = curves["in"]
        for year, (s, n) in REFERENCE_HOLIDAYS.items():
            truth = _hybrid(c, year, n_days)
            others = [_hybrid(c, y, n_days) for y, _ in REFERENCE_HOLIDAYS.items()
                      if y != year]
            others = [o for o in others if o]
            if truth and others:
                pred = [statistics.mean(col) for col in zip(*others)]
                errs.append(statistics.mean(abs(p - t) / t for p, t in zip(pred, truth)))
    if not errs:
        return {}
    return {"method": "留一年交叉验证(20 个目的地城市全来源迁入指数)",
            "in_MAPE_%": round(100 * statistics.mean(errs), 1),
            "in_MAPE_median_%": round(100 * statistics.median(errs), 1),
            "n_city_years": len(errs)}


def share_backtest(city_ranks: dict[str, dict[str, float]], cities: list[str],
                   prior_shares: dict[str, float]) -> dict:
    """结构层回测: 预测 2025 假期 D1-D3 城市占比.
    对照: (a) 2024 同期占比(持续性) vs (b) 纯辐射模型先验. 指标: 平均绝对误差(百分点).
    """
    def mean_share(year):
        s = _d(REFERENCE_HOLIDAYS[year][0])
        rows = [city_ranks.get(_key(s + dt.timedelta(k)), {}) for k in range(3)]
        rows = [r for r in rows if r]
        if not rows:
            return None
        return {c: statistics.mean(r.get(c, 0.0) for r in rows) for c in cities}

    y24, y25 = mean_share(2024), mean_share(2025)
    if not (y24 and y25):
        return {}
    total = sum(y25.values()) or 1.0
    prior_total = sum(prior_shares.get(c, 0) for c in cities) or 1.0
    prior = {c: prior_shares.get(c, 0) / prior_total * total for c in cities}
    mae_persist = statistics.mean(abs(y24[c] - y25[c]) for c in cities)
    mae_prior = statistics.mean(abs(prior[c] - y25[c]) for c in cities)
    return {"cities": len(cities),
            "persistence_MAE_pp": round(mae_persist, 2),
            "radiation_prior_MAE_pp": round(mae_prior, 2)}
