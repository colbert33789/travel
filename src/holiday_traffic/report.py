"""移动端优先的 HTML 报告.

阅读顺序(先结论后依据): 一句话结论 → 天气 → 出城/返程节奏 → 错峰时刻 → 路况 →
目的地拥挤 → 推荐 → 方法与准确度(折叠细节) → 数据来源与更新时间.
每张图只回答一个问题, 用通俗单位(「平时1小时要开多久」「人数 vs 接待能力」).
"""
from __future__ import annotations

import datetime as dt
import html
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from .clock import stamp
from .data.openmeteo import RAIN_LABEL, weather_icon
from .features import tti_hours, tti_plain
from .geo import CITY_ADCODE, HIGHWAYS, TOWNS, short_name
from .recommend import trip_rank

PEAK_SHARE = 0.5   # 高峰段 = 高于当日「谷值 + 50% × 峰谷差」的连续小时
MIN_JAM_TTI = 1.7  # 当日峰值低于此(高德轻度拥堵线)视为全天较顺
HL_TOP = 5         # 「重点提醒」各栏条数
CROWD_TOP = 20     # 「哪里人最多」条形图展示的目的地数
# 「去哪儿玩」: 不逐日铺开(7×10=70 张卡片无从下手), 只给主榜 Top10 + 远途补充
TRIP_TOP = 10      # 主榜「最值得去的 10 个地方」
TRIP_DAYS = 3      # 行程天数(用于分摊车程)
TRIP_SKIP_HEAD = 1 # 窗口跳过前 N 天(出城峰)
TRIP_SKIP_TAIL = 2 # 窗口跳过后 N 天(返程峰) -> 10/2–10/5, 覆盖 10/2 或 10/3 出发
# 「远途精选」按车程分档(近郊榜会系统性挤掉这些方向), 每档各取前 LONGHAUL_TOP 个
LONGHAUL_BANDS = ((2.5, 4.0, "中远途", "2.5–4 小时"), (4.0, 99.0, "长途", "4 小时以上"))
LONGHAUL_TOP = 5       # 远途每档条数(精简后不再铺开 8 条)

# 热力图色标: 连续渐变, 在各拥堵分档的「中点」取该档代表色。
# 旧版是硬分段(重复断点), 同一档内所有值渲染成完全相同的颜色 —— 2.2 与 2.9 无法区分,
# 且图例写的「红」实际是 #ff7043(橙红), 描述与颜色对不上。改为连续渐变后:
#   · 段内仍有明暗差(区分度), 2.5 与 2.9 颜色不同
#   · 各档「中心」颜色与图例文字一致, 边界处为过渡色
#   · 配合右侧色标(colorbar), 读者可直接用颜色对照数值
TTI_VMIN, TTI_VMAX = 1.0, 3.5
_TTI_MID = ((1.15, "#1aa260"),    # 畅通档中点 -> 绿
            (1.50, "#fbc02d"),    # 轻度档中点 -> 黄
            (1.95, "#f57c00"),    # 中度档中点 -> 橙
            (2.60, "#e53935"),    # 严重档中点 -> 红
            (3.25, "#7f0000"))    # 极端档中点 -> 深红
TTI_SCALE = [[(t - TTI_VMIN) / (TTI_VMAX - TTI_VMIN), c] for t, c in _TTI_MID]
TTI_TICKS = (1.0, 1.3, 1.7, 2.2, 3.0, 3.5)


def _segments(hours: list[int]) -> list[tuple[int, int]]:
    """连续整点分段: [9,10,11,16,17] -> [(9,11),(16,17)]."""
    segs: list[tuple[int, int]] = []
    for h in hours:
        if segs and h == segs[-1][1] + 1:
            segs[-1] = (segs[-1][0], h)
        else:
            segs.append((h, h))
    return segs


def travel_advice(traffic: pd.DataFrame) -> list[dict]:
    """逐日出发建议(基于全部走廊逐小时平均 TTI, 6-23 点).

    主高峰段 = 含峰值小时的连续高峰段; 上午主峰(出城型)建议提前出发, 傍晚主峰(返程型)建议
    中午前或主峰结束后出发. 最多展示 2 个高峰段.
    """
    out = []
    for d in sorted(traffic["date"].unique()):
        h = traffic[traffic["date"] == d].groupby("hour")["tti_pred"].mean()
        day = h[(h.index >= 6) & (h.index <= 23)]
        lo, hi, peak_h = float(day.min()), float(day.max()), int(day.idxmax())
        rec = {"date": d, "peak_hour": peak_h, "peak_tti": hi, "am_peak": peak_h < 14,
               "before": None, "after": None, "jam": "全天较顺", "main": "全天较顺"}
        if hi >= MIN_JAM_TTI:
            thr = lo + PEAK_SHARE * (hi - lo)
            segs = _segments([int(x) for x in day[day >= thr].index])
            main = next(s for s in segs if s[0] <= peak_h <= s[1])
            shown = sorted(sorted(segs, key=lambda s: -day.loc[s[0]:s[1]].max())[:2])
            rec.update(before=max(0, main[0] - 1), after=min(main[1] + 1, 23),
                       main=f"{main[0]}-{main[1] + 1}点",
                       jam="、".join(f"{a}-{b + 1}点" for a, b in shown))
        out.append(rec)
    return out

PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@{v}/plotly.min.js"

# 只为「实际可横向滚动」的容器加视觉提示, 避免不滚动时右侧内容被无端淡化
SCROLL_HINT_JS = """
(function(){
  var mark = function(){
    var els = document.querySelectorAll('.tt, .src, .nav');
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      if (e.scrollWidth > e.clientWidth + 4) { e.classList.add('scrollable'); }
      else { e.classList.remove('scrollable'); }
    }
  };
  mark();
  window.addEventListener('resize', mark);
  window.addEventListener('orientationchange', mark);
})();
"""
CSS = (Path(__file__).parent / "templates" / "report.css").read_text(encoding="utf-8")
WEEK = "一二三四五六日"
FONT = dict(family="-apple-system, PingFang SC, Microsoft YaHei, sans-serif", size=12,
            color="#1d2733")
CONFIG = {"displayModeBar": False, "responsive": True, "scrollZoom": False}
LEVEL_COLOR = {"舒畅": "#1aa260", "正常": "#8bc34a", "较拥挤": "#f5a623",
               "拥挤": "#ff7043", "严重拥挤": "#e53935"}
STATUS_CLASS = {"live": "live", "cache": "live", "stale": "snap", "static": "prior",
                "prior": "prior", "simulated": "prior", "not_configured": "off",
                "offline": "snap", "partial": "snap", "user_supplied": "prior",
                "quota_exhausted": "off"}
STATUS_TEXT = {"live": "实时", "cache": "实时(缓存)", "stale": "过期缓存", "static": "静态",
               "prior": "先验", "simulated": "仿真", "not_configured": "未接入",
               "offline": "离线快照", "partial": "部分覆盖", "user_supplied": "用户导入",
               "quota_exhausted": "当日额度耗尽"}


def _e(s) -> str:
    return html.escape(str(s))


def _md(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return f"{x.month}/{x.day}"


def _mdw(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return f"{x.month}/{x.day} 周{WEEK[x.weekday()]}"


def _status(s: str) -> tuple[str, str]:
    key = s.split("(")[0].split(" ")[0]
    if key.startswith("snapshot"):
        return "snap", "内置真实快照"
    if key.startswith("climate"):
        return "snap", "气候均值"
    if key.startswith("failed"):
        return "off", "失败"
    return STATUS_CLASS.get(key, "prior"), STATUS_TEXT.get(key, key)


def _layout(fig: go.Figure, height: int, **kw) -> go.Figure:
    opts = dict(template="plotly_white", font=FONT, height=height, autosize=True,
                margin=dict(l=8, r=8, t=8, b=8), hovermode="x unified", dragmode=False,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11)))
    opts.update(kw)
    fig.update_layout(**opts)
    return fig


def _chart(fig: go.Figure) -> str:
    return f'<div class="chart">{fig.to_html(full_html=False, include_plotlyjs=False, config=CONFIG)}</div>'


# ======================= 分块 =======================

def _mig_badge(provenance: dict) -> str:
    """顶部徽章随数据真实状态变化 —— 降级到快照时不能还写着「真实数据」."""
    st = (provenance.get("百度慧眼迁徙") or {}).get("status", "")
    if st.startswith(("live", "cache")):
        return "百度迁徙真实数据"
    if st.startswith("snapshot"):
        return "迁徙=内置真实快照"
    return "迁徙数据已降级"


def _wx_badge(provenance: dict) -> str:
    st = (provenance.get("Open-Meteo 天气") or {}).get("status", "")
    if st.startswith(("live", "cache")):
        return "逐日天气预报"
    if "气候均值" in st:
        return "天气=气候均值兜底"
    return "天气数据已降级"


def _tldr(res: dict) -> str:
    mig, traffic, rec, crowd = res["migration"], res["traffic"], res["recommendations"], res["crowding"]
    hol = mig[mig["out_index"].notna()]
    peak_out = hol.loc[hol["excess_out_万人次"].idxmax()]
    peak_in = mig.loc[mig["in_index"].idxmax()]
    worst = traffic.loc[traffic["tti_pred"].idxmax()]
    hot = crowd.groupby("town")["crowding_index"].max().sort_values(ascending=False)
    hot_names = "、".join(short_name(t) for t in hot.index[:3])
    # 推荐必须和「去哪儿玩」主榜同源(多日行程视角), 否则两处结论会打架;
    # 同时排除「人最多」前三, 否则同一段话既说它最挤又推荐它。
    dates = sorted(rec["date"].unique())
    span = dates[TRIP_SKIP_HEAD:len(dates) - TRIP_SKIP_TAIL]
    rank = trip_rank(rec, span, province_only=True, trip_days=TRIP_DAYS)
    rank = rank[~rank["town"].isin(hot.index[:3])]
    top_rec = rank["town"].head(3).tolist()
    # 天气调度: 全省哪天最干爽 / 哪天雨最广。比只报"深圳有雨"对自驾更有用 ——
    # 深圳下雨不代表目的地也下雨, 反之亦然, 直接给全省口径才能安排行程。
    w = res["weather"]
    wet = {d: int((w[w["date"] == d]["rain_level"] >= 1).sum()) for d in span}
    driest = min(span, key=lambda d: wet[d])
    wettest = max(span, key=lambda d: wet[d])
    adv = {a["date"]: a for a in travel_advice(traffic)}
    a_out, a_in = adv.get(peak_out["date"]), adv.get(peak_in["date"])
    out_tip = f"，<b>{a_out['before']}点前出门</b>可避开 {a_out['main']} 高峰" \
        if a_out and a_out["before"] else ""
    in_tip = f"，<b>{a_in['main']}</b> 最堵，建议中午前或 {a_in['after']} 点后上路" \
        if a_in and a_in["after"] else "，下午到晚上最堵"
    items = [
        f"🚗 <b>出城最挤：{_mdw(peak_out['date'])}</b>（比平日多约 {peak_out['excess_out_万人次']:.0f} 万人次）{out_tip}",
        f"🏠 <b>返程最挤：{_mdw(peak_in['date'])}</b>{in_tip}",
        f"🛣 <b>最堵路段：{_e(worst['highway_name'])}</b>（{_md(worst['date'])} {int(worst['hour'])}点，{tti_plain(worst['tti_pred'])}）",
        f"👥 <b>人最多：</b>{_e(hot_names)}",
        f"💎 <b>{TRIP_DAYS} 天行程最值得去：</b>"
        f"{_e('、'.join(short_name(t) for t in top_rec))}"
        f"（{_md(span[0])}–{_md((dt.date.fromisoformat(span[-1]) - dt.timedelta(days=TRIP_DAYS - 1)).isoformat())} 出发 · 省内）",
        (f"🌧 <b>{_md(driest)} 最干爽、{_md(wettest)} 雨最广</b>"
         f"（{wet[wettest]}/{len(w[w.date == wettest])} 地点有雨）："
         f"户外排在 {_md(driest)}，{_md(wettest)} 安排室内或雨景")
        if wet[driest] < wet[wettest] else "",
    ]
    rw = res.get("railway") or {}
    daily = rw.get("daily") or {}
    if daily:
        # 逐日: 取每个方向的假期峰值售罄率及其出现日, 而不是只看首日
        peak = {}
        for c in {c for d in daily.values() for c in d}:
            peak[c] = max((daily[d][c]["rate"], d) for d in daily if c in daily[d])
        hot = [(c, r, d) for c, (r, d) in sorted(peak.items(), key=lambda kv: -kv[1][0])
               if r >= 0.9]
        if hot:
            names = "、".join(c for c, _, _ in hot[:4])
            day = min(d for _, _, d in hot[:4])
            items.append(f"🎫 <b>火车票最紧张：</b>{_e(names)} 方向"
                         f"（12306 售罄率峰值 ≥90%，{_md(day)} 前后最难买）")
    elif rw.get("city"):
        full = sorted(rw["city"].items(), key=lambda kv: -kv[1]["rate"])
        full = [c for c, v in full if v["rate"] >= 0.9]
        if full:
            items.append(f"🎫 <b>火车票已抢光：</b>{'、'.join(full[:4])}方向"
                         f"（12306 假期首日售罄率 ≥90%）")
    lis = "".join(f"<li>{x}</li>" for x in items if x)
    return f'<div class="tldr"><h2>📌 一分钟看懂</h2><ul>{lis}</ul></div>'


def _weather(res: dict) -> str:
    sz = res["weather"][res["weather"]["place"] == "深圳"]
    cells = []
    for r in sz.itertuples():
        rain = int(r.rain_level)
        code = None if pd.isna(r.code) else int(r.code)
        icon = weather_icon(code, rain)
        # 雷暴码(>=95)常伴短时强降水但 24h 累计仍是小雨级, 显示「⛈ 小雨」会自相矛盾
        label = "雷阵雨" if (code is not None and code >= 95) else RAIN_LABEL[rain]
        prob = f"{int(r.prob)}%" if not pd.isna(r.prob) else "-"
        cells.append(f'<div class="day{" rain" if rain >= 1 else ""}"><div class="d">{_md(r.date)}</div>'
                     f'<div class="i">{icon}</div><div class="p">{label}</div>'
                     f'<div class="p">{prob}</div><div class="p">{r.tmax:.0f}°</div></div>')
    src = "Open-Meteo 实时预报" if (sz["source"] == "forecast").all() else "部分日期为气候均值"
    return f"""<section class="card" id="weather"><h2>🌦 假期天气（深圳）</h2>
<p class="how">{src}；降水按国标 24h 雨量分级，<b>中雨以上会让拥堵加重约 12%</b>。</p>
<div class="days">{''.join(cells)}</div></section>"""


def _rhythm(res: dict) -> str:
    mig = res["migration"]
    # 只画到假期最后一天(D0..Dn)。Dn+1 是节后首日, 只有迁入没有迁出,
    # 画出来会在右端留一段"半条曲线", 且超出用户关心的假期范围。
    hol = mig[mig["out_index"].notna()]
    x = [_md(d) for d in hol["date"]]
    inx = mig.iloc[:len(x)]
    fig = go.Figure()
    band = hol[hol["out_high"].notna()]
    bx = [_md(d) for d in band["date"]]
    fig.add_trace(go.Scatter(x=bx + bx[::-1],
                             y=list(band["out_high"]) + list(band["out_low"])[::-1],
                             fill="toself", fillcolor="rgba(255,36,66,.12)", line=dict(width=0),
                             hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=bx, y=band["out_index"], name="离开深圳",
                             mode="lines+markers", line=dict(color="#ff2442", width=3),
                             hovertemplate="%{y:.1f}"))
    fig.add_trace(go.Scatter(x=x + x[::-1], y=list(inx["in_high"]) + list(inx["in_low"])[::-1],
                             fill="toself", fillcolor="rgba(41,128,185,.12)", line=dict(width=0),
                             hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=x, y=inx["in_index"], name="回到深圳", mode="lines+markers",
                             line=dict(color="#2980b9", width=3), hovertemplate="%{y:.1f}"))
    base = res["profile"].baseline_out
    fig.add_hline(y=base, line_dash="dot", line_color="#9aa4ae",
                  annotation_text="平日水平", annotation_position="top left")
    obs = mig[mig["observed_out"].notna()]
    if len(obs):
        fig.add_trace(go.Scatter(x=[_md(d) for d in obs["date"]], y=obs["observed_out"],
                                 name="已实测", mode="markers",
                                 marker=dict(symbol="diamond", size=10, color="#1d2733")))
    _layout(fig, 280, yaxis=dict(title="迁徙规模指数", gridcolor="#eef0f3", rangemode="tozero"))
    stock = mig[mig["away_stock_万人"].notna()]
    peak = stock.loc[stock["away_stock_万人"].idxmax()]
    # 假期最后一天仍有一部分人在外(节后首日才返程完毕), 补一句避免读者困惑
    last = stock.iloc[-1]
    tail = (f"假期最后一天（{_md(last['date'])}）仍有约 {last['away_stock_万人']:.0f} 万人在外，"
            f"于节后首日返程完毕。" if last["away_stock_万人"] >= 1 else "")
    return f"""<section class="card" id="rhythm"><h2>📈 什么时候走、什么时候回</h2>
<p class="how">百度慧眼迁徙真实数据推算，假期起止为 {x[1]}–{x[-1]}。
阴影 = 按 2024/2025 两年规律给出的可能范围。
<b>{_mdw(peak['date'])} 在外的深圳人最多</b>（约 {peak['away_stock_万人']:.0f} 万人）。{tail}</p>
{_chart(fig)}</section>"""


def _railway(res: dict) -> str:
    """逐日抢票热度(日期 × 方向)。

    只展示首日会漏掉返程日才紧张的方向 —— 出城日与返程日热门方向往往不同,
    故用热力图同时呈现「哪个方向最难买」与「哪天最难买」。
    """
    rw = res.get("railway") or {}
    daily = rw.get("daily") or {}
    if not daily:                      # 兼容只含首日的旧缓存: 退化为单日展示
        if not rw.get("city"):
            return ""
        daily = {(res.get("dates") or sorted(rw.get("curve", {})) or [""])[0]: rw["city"]}
        if not list(daily)[0]:
            return ""
    dates = sorted(daily)
    # 方向按假期均值排序(最难的在前); 售罄率缺失记 None, 热力图显示为空白
    mean_rate = {c: (sum(daily[d].get(c, {}).get("rate", 0.0) for d in dates) / len(dates))
                 for c in {c for d in dates for c in daily[d]}}
    cities = sorted(mean_rate, key=lambda c: -mean_rate[c])
    z = [[daily[d].get(c, {}).get("rate", None) and daily[d][c]["rate"] * 100
          for d in dates] for c in cities]
    fig = go.Figure(go.Heatmap(
        z=z, x=[_md(d) for d in dates], y=cities, xgap=1, ygap=1,
        colorscale=[[0, "#1aa260"], [0.35, "#fbc02d"], [0.6, "#f57c00"],
                    [0.85, "#e53935"], [1, "#7f0000"]],
        zmin=0, zmax=100, colorbar=dict(thickness=10, len=0.8, outlinewidth=0,
                                        ticksuffix="%", tickfont=dict(size=10)),
        hovertemplate="%{y} · %{x} 售罄率 %{z:.0f}%<extra></extra>"))
    _layout(fig, 70 + 21 * len(cities),
            yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
            xaxis=dict(side="top"), hovermode="closest")
    updated = rw.get("fetched_at", "")
    peak_day = max(dates, key=lambda d: sum(daily[d].get(c, {}).get("rate", 0) for c in cities))
    worst = max(cities, key=lambda c: mean_rate[c]) if cities else ""
    return f"""<section class="card" id="rail"><h2>🎫 火车票实时抢票热度</h2>
<p class="how">12306 真实订票数据（{updated} 抓取）：深圳出发各方向<b>逐日车票售罄率</b>。
最难买的是 <b>{_e(worst)}</b>（假期均值 {mean_rate.get(worst, 0) * 100:.0f}%），
整体最紧张的一天是 <b>{_md(peak_day)}</b>。售罄的方向当地客流大概率超预期，模型已据此上调拥挤度。</p>
{_chart(fig)}</section>"""


def _timetable(res: dict) -> str:
    adv = travel_advice(res["traffic"])
    rows = []
    for a in adv:
        if a["before"] is None:
            go_txt = "全天均可"
        elif a["am_peak"]:
            go_txt = f"{a['before']}点前"          # 上午高峰(出城型): 早走
        else:
            go_txt = f"午前 / {a['after']}点后"     # 傍晚高峰(返程型): 中午前或入夜
        rows.append(f'<tr><td><b>{_md(a["date"])}</b></td>'
                    f'<td><span class="pill go">{go_txt}</span></td>'
                    f'<td><span class="pill stop">{a["jam"]}</span></td>'
                    f'<td>{tti_hours(a["peak_tti"])}</td></tr>')
    # 文案由数据生成, 不写死"前 3 天上午堵"
    n_am = sum(1 for a in adv if a["before"] is not None and a["am_peak"])
    n_pm = sum(1 for a in adv if a["before"] is not None and not a["am_peak"])
    if n_am and n_pm:
        phase = f"其中 {n_am} 天以<b>上午出城峰</b>为主、{n_pm} 天以<b>傍晚返程峰</b>为主"
    elif n_am:
        phase = f"{n_am} 天为<b>上午出城峰</b>"
    elif n_pm:
        phase = f"{n_pm} 天为<b>傍晚返程峰</b>"
    else:
        phase = "各日高峰均不明显"
    return f"""<section class="card" id="time"><h2>⏰ 错峰出行时刻表</h2>
<p class="how">{len(HIGHWAYS)} 条出城高速逐小时平均拥堵。<b>最堵时</b> = 平时 1 小时的路要开多久。
{phase}。</p>
<table class="tt"><thead><tr><th>日期</th><th>建议出发</th><th>拥堵时段</th><th>最堵时</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section>"""


def _roads(res: dict) -> str:
    tr = res["traffic"]
    top = tr.groupby("highway_name")["tti_pred"].max().sort_values(ascending=False).index[:8]
    sub = tr[tr["highway_name"].isin(top)].groupby(["highway_name", "date"])["tti_pred"].max().unstack()
    sub = sub.loc[top]
    z = sub.values
    text = [[f"{v:.1f}" for v in row] for row in z]
    fig = go.Figure(go.Heatmap(
        z=z, x=[_md(d) for d in sub.columns], y=[n.replace("高速", "") for n in sub.index],
        text=text, texttemplate="%{text}", textfont=dict(size=11),
        colorscale=TTI_SCALE, zmin=TTI_VMIN, zmax=TTI_VMAX,
        colorbar=dict(thickness=10, len=0.8, outlinewidth=0,
                      tickvals=list(TTI_TICKS), tickfont=dict(size=10)),
        hovertemplate="%{y}<br>%{x} 最堵时 TTI %{z:.2f}<extra></extra>"))
    _layout(fig, 60 + 34 * len(top), yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
            xaxis=dict(side="top"))
    live = res["live_calibration"]
    if live.get("applied"):
        live_txt = (f"已用高德实时车速校准当日剩余时段"
                    f"（{len(live.get('ratios', {}))} 条走廊有实时数据）。")
    elif live.get("reason"):
        live_txt = f"未做实时校准（{_e(live['reason'])}）。"
    else:
        live_txt = "暂未接入高德实时路况（配置 AMAP_KEY 后自动校准）。"
    return f"""<section class="card" id="roads"><h2>🛣 哪些高速最堵</h2>
<p class="how">数字 = 当天最堵时刻的<b>拥堵延时指数</b>：2.0 表示平时 1 小时的路要开 2 小时。
颜色由绿到深红连续加深，右侧色标可直接对照数值：
&lt;1.3 基本畅通 · 1.7 轻度 · 2.2 中度 · 3.0 严重 · 3.5 极端。{live_txt}</p>
{_chart(fig)}</section>"""


def _crowd(res: dict) -> str:
    c = res["crowding"]
    peak = c.loc[c.groupby("town")["crowding_index"].idxmax()]
    peak = peak.sort_values("crowding_index", ascending=False).head(CROWD_TOP).iloc[::-1]
    fig = go.Figure(go.Bar(
        x=peak["crowding_index"] * 100, y=[short_name(t) for t in peak["town"]], orientation="h",
        marker_color=[LEVEL_COLOR[lv] for lv in peak["crowding_level"]],
        text=[f"{v * 100:.0f}% · {_md(d)}" for v, d in zip(peak["crowding_index"], peak["date"])],
        textposition="outside", cliponaxis=False,
        hovertemplate="%{y}: 游客约 %{customdata:.1f} 万人<extra></extra>",
        customdata=peak["visitors_万人"]))
    fig.add_vline(x=100, line_dash="dot", line_color="#e53935",
                  annotation_text="接待能力用满", annotation_position="bottom right")
    _layout(fig, 60 + 24 * len(peak), xaxis=dict(title="峰值日游客 ÷ 接待能力 (%)",
                                                 range=[0, max(130, peak["crowding_index"].max() * 118)]),
            hovermode="closest", margin=dict(l=8, r=52, t=8, b=8))
    counts = c[c["date"] == c.loc[c["crowding_index"].idxmax(), "date"]]["crowding_level"].value_counts()
    summary = "、".join(f"{k} {counts.get(k, 0)} 个" for k in ("舒畅", "正常", "较拥挤", "拥挤", "严重拥挤")
                        if counts.get(k, 0))
    # 各等级的目的地清单: 只给 Top 图不够, 读者还需要知道"哪些地方要避开"
    pk = c.loc[c.groupby("town")["crowding_index"].idxmax()]
    graded = []
    for lv in ("严重拥挤", "拥挤", "较拥挤"):
        names = [short_name(t) for t in pk[pk["crowding_level"] == lv]
                 .sort_values("crowding_index", ascending=False)["town"]]
        if names:
            graded.append(f'<div class="gl"><b>{lv}</b>：{_e("、".join(names))}</div>')
    return f"""<section class="card" id="crowd"><h2>👥 哪里人最多</h2>
<p class="how">每个目的地<b>最挤那天的游客 ÷ 酒店民宿可接待人数</b>。超过 100% 意味着订房难、景点排长队。
最挤那天 {len(TOWNS)} 个目的地中：{summary}。下图为最挤的 {len(peak)} 个。</p>
{_chart(fig)}{''.join(graded)}</section>"""


def _highlights(res: dict) -> str:
    c, rec = res["crowding"], res["recommendations"]
    hot = c.loc[c.groupby("town")["crowding_index"].idxmax()].nlargest(HL_TOP, "crowding_index")
    tr = res["traffic"]
    jam = tr.loc[tr.groupby("highway_name")["tti_pred"].idxmax()].nlargest(HL_TOP, "tti_pred")
    agg = rec.groupby("town").agg(play=("playability", "first"), crowd=("crowding_index", "max"),
                                  prem=("price_premium", "first"), hours=("drive_hours", "mean"))
    gems = agg[(agg["crowd"] < 0.6) & (agg["play"] >= 60)].sort_values("play", ascending=False).head(HL_TOP)

    def li(title, sub):
        return f'<li>{title}<span class="sub">{sub}</span></li>'
    hot_li = "".join(li(f"<b>{_e(short_name(r.town))}</b>",
                        f"{_md(r.date)} 游客达接待能力 {r.crowding_index * 100:.0f}%") for r in hot.itertuples())
    jam_li = "".join(li(f"<b>{_e(r.highway_name)}</b>",
                        f"{_md(r.date)} {int(r.hour)}点 · {tti_plain(r.tti_pred)}") for r in jam.itertuples())
    gem_li = "".join(li(f"<b>{_e(short_name(n))}</b>",
                        f"游玩性 {g.play:.0f} · 最挤时仅 {g.crowd * 100:.0f}% · 车程约 {g.hours:.1f}h")
                     for n, g in gems.iterrows()) or li("暂无", "本期没有同时满足人少、好玩的目的地")
    return f"""<div class="hl-grid">
<div class="hl hl-red"><h3>⚠️ 人最多</h3><ul>{hot_li}</ul></div>
<div class="hl hl-orange"><h3>🚧 最堵路段</h3><ul>{jam_li}</ul></div>
<div class="hl hl-green"><h3>💎 人少又好玩</h3><ul>{gem_li}</ul></div></div>"""


DAY_ROLES = {1: "出城日", 2: "出城次高峰", 3: "假期前段", 4: "假期中段"}


def _day_role(d: str, dates: list[str]) -> str:
    """当天的假期角色: 前 4 天按固定语义, 其后按距末日的天数标注返程属性."""
    k = dates.index(d) + 1
    if k in DAY_ROLES:
        return DAY_ROLES[k]
    n = len(dates)
    return "返程日前一日" if k == n - 1 else ("返程日" if k == n else f"假期第{k}天")


def _day_tip(d: str, adv: dict, crowd: pd.DataFrame) -> str:
    """当天出行要点: 错峰出发时间 + 拥堵时段 + 目的地拥挤概况."""
    bits = []
    if adv.get("before") is not None:
        go = f"{adv['before']}点前出发" if adv["am_peak"] else f"午前或{adv['after']}点后出发"
    else:
        go = "全天均可出发"
    bits.append(f"🚗 {go}")
    if adv.get("jam") and adv["jam"] != "全天较顺":
        bits.append(f"拥堵 {adv['jam']}")
    bits.append(f"最堵时 {tti_hours(adv['peak_tti'])}")
    hot = int((crowd["crowding_index"] >= 1.0).sum())
    if hot:
        bits.append(f"{hot} 个目的地已挤满")
    else:
        bits.append(f"目的地普遍{('偏挤' if crowd['crowding_index'].median() >= 0.4 else '宽松')}")
    return " · ".join(bits)


def _trip(res: dict) -> str:
    """主榜「最值得去的 10 个地方」: 按多日自驾行程排序, 覆盖 10/2 或 10/3 出发.

    逐日榜隐含「当天往返」假设, 用它规划 2-3 天自驾会系统性低估远一点的地方
    (车程扣分被重复计算), 因此改用 trip_rank: 车程按 TRIP_DAYS 天分摊。
    """
    rec = res["recommendations"]
    dates = sorted(rec["date"].unique())
    if len(dates) < 4:
        return ""
    tail = len(dates) - TRIP_SKIP_TAIL
    span = dates[TRIP_SKIP_HEAD:tail]        # 10/2–10/5
    if len(span) < 2:
        return ""
    rank = trip_rank(rec, span, province_only=True, trip_days=TRIP_DAYS).head(TRIP_TOP)
    if not len(rank):
        return ""
    w = res["weather"]
    wet = {d: int((w[w["date"] == d]["rain_level"] >= 1).sum()) for d in span}
    driest = min(span, key=lambda d: wet[d])
    wettest = max(span, key=lambda d: wet[d])
    # 逐 town 的天气: 列出窗口内「哪几天有雨」。
    # 只标最差那天会误导 —— 10/4 全省有雨而 10/5 放晴, 标成"中雨"会让人以为全程下雨。
    wsub = w[w["date"].isin(span)]
    wet_days, wet_code = {}, {}
    for place, g in wsub.groupby("place"):
        days = [d for d in span if float(g.loc[g["date"] == d, "rain_level"].iloc[0]) >= 1]
        wet_days[place] = days
        if days:
            row = g[g["date"] == days[0]].iloc[0]
            wet_code[place] = (int(row["rain_level"]),
                               None if pd.isna(row["code"]) else int(row["code"]))
    items = []
    for i, r in enumerate(rank.itertuples(), 1):
        days = wet_days.get(r.town, [])
        if days:
            rain, code = wet_code.get(r.town, (int(r.rain_max), None))
            label = f"{weather_icon(code, rain)}雨 " + "、".join(_md(d) for d in days)
        else:
            label = "☀️全程无雨"
        tags = (f'<span class="tag">车程约{r.drive_hours:.1f}h</span>'
                f'<span class="tag">游玩性 {r.playability:.0f}</span>'
                f'<span class="tag">最挤时 {r.crowding_index * 100:.0f}%</span>'
                f'<span class="tag">{label}</span>')
        items.append(f'<div class="rec"><div class="rk">{i}</div><div class="body">'
                     f'<div class="name">{_e(short_name(r.town))} '
                     f'<span class="meta">{_e(r.city)}</span></div>'
                     f'<div class="tags">{tags}</div></div>'
                     f'<div class="score">{r.trip_score:.0f}</div></div>')
    return (f'<h3>🎒 最值得去的 {len(rank)} 个地方'
            f'（{_md(span[0])}–{_md(span[-1])} 出发 · {TRIP_DAYS} 天 · 广东省内）</h3>'
            f'<div class="daytip">避开 {_md(dates[0])} 出城峰与 {_md(dates[-1])} 返程峰；'
            f'{_md(span[0])} 或 {_md(span[1])} 出发都合适。'
            f'<b>{_md(driest)} 最干爽</b>，宜安排户外；'
            f'<b>{_md(wettest)} 雨最广</b>（{wet[wettest]}/{len(w[w.date==wettest])} 地点有雨），'
            f'宜安排室内或雨景（温泉、溶洞、古镇、美食）。</div>'
            + "".join(items))


def _longhaul(rec: pd.DataFrame, weather: pd.DataFrame,
              exclude: set[str] | None = None) -> str:
    """远途补充(按车程分档, 已排除主榜): 主榜会被 1-3h 的近郊目的地占满.

    粤西(阳江/云浮/茂名/湛江)、粤东北(梅州)、粤北(韶关/清远)等方向游玩性评分
    很高, 但车程 3-6h 会被车程扣分挤下主榜。按中远途/长途分档单列, 既让每个
    方向都被看见, 也便于读者按可接受车程对号入座。exclude 用于去掉主榜已有项,
    避免两个榜重复出现同一个目的地。
    """
    agg = rec.groupby("town").agg(score=("recommend_score", "mean"),
                                  play=("playability", "first"),
                                  hours=("drive_hours", "mean"),
                                  crowd=("crowding_index", "max"),
                                  city=("city", "first"))
    # 与主榜同口径: 直接列出哪几天有雨。「有雨 4/7 天」这种分数读者无法对照行程,
    # 也不知道是哪几天; 列出日期才能直接判断"我去的这几天下不下雨"。
    wet_days: dict[str, list[str]] = {}
    for place, g in weather.groupby("place"):
        wet_days[place] = [d for d, lv in zip(g["date"], g["rain_level"]) if lv >= 1]
    wr = weather.groupby("place").agg(
        rain=("rain_level", "max"),
        code=("code", lambda s: s.dropna().max() if s.notna().any() else None))
    out = []
    for lo, hi, label, span in LONGHAUL_BANDS:
        band = agg[(agg["hours"] >= lo) & (agg["hours"] < hi)]
        if exclude:                       # 主榜出现过的不再重复展示
            band = band[~band.index.isin(exclude)]
        band = band.sort_values("score", ascending=False).head(LONGHAUL_TOP)
        if not len(band):
            continue
        items = []
        for n, g in band.iterrows():
            days = wet_days.get(n, [])
            if days:
                rain = int(wr.at[n, "rain"])
                wc = wr.at[n, "code"]
                wt = (f'<span class="tag">'
                      f'{weather_icon(None if pd.isna(wc) else int(wc), rain)}雨 '
                      + "、".join(_md(d) for d in days) + '</span>')
            else:
                wt = '<span class="tag">☀️全程无雨</span>'
            tags = (f'<span class="tag">车程约{g.hours:.1f}h</span>'
                    f'<span class="tag">游玩性 {g.play:.0f}</span>'
                    f'<span class="tag">最挤时 {g.crowd * 100:.0f}%</span>{wt}')
            items.append(f'<div class="rec"><div class="rk">🚙</div><div class="body">'
                         f'<div class="name">{_e(short_name(n))} <span class="meta">{_e(g.city)}</span></div>'
                         f'<div class="tags">{tags}</div></div>'
                         f'<div class="score">{g.score:.0f}</div></div>')
        out.append(f'<h3>🚙 {label}精选（车程 {span}）</h3>' + "".join(items))
    if not out:
        return ""
    tip = (f'主榜会被 1–3 小时的近郊目的地占满；这些方向游玩性很高，只是车程扣分大。'
           f'按可接受车程对号入座，适合 {TRIP_DAYS} 天以上的行程。')
    return f'<div class="daytip">{tip}</div>' + "".join(out)


def _trip_top_towns(res: dict) -> set[str]:
    """主榜已出现的目的地集合(供远途榜去重)."""
    rec = res["recommendations"]
    dates = sorted(rec["date"].unique())
    if len(dates) < 4:
        return set()
    span = dates[TRIP_SKIP_HEAD:len(dates) - TRIP_SKIP_TAIL]
    rank = trip_rank(rec, span, province_only=True, trip_days=TRIP_DAYS).head(TRIP_TOP)
    return set(rank["town"]) if len(rank) else set()


def _recommend(res: dict) -> str:
    """精简版: 不逐日铺开(那样会有 7×10=70 张卡片, 读者无从下手),
    只给一份「这个假期最值得去的 10 个地方」+ 远途补充。

    排序用 trip_rank(多日行程视角): 车程按天分摊, 天气/拥挤度取窗口内最不利的一天。
    """
    rec = res["recommendations"]
    return f"""<section class="card" id="rec"><h2>🏆 去哪儿玩</h2>
<p class="how">按「假期中段出发、{TRIP_DAYS} 天自驾」排序：好玩程度 − 人多扣分 −
车程与堵车扣分 − 房价上涨扣分 − 雨天扣分。车程按行程天数分摊（只去一次，不是每天往返），
天气与拥挤度取行程内最不利的一天。</p>
{_trip(res)}{_longhaul(rec, res["weather"], _trip_top_towns(res))}</section>"""


def _method(res: dict) -> str:
    m = res["metrics"]
    mb, db, sb = m.get("migration_backtest", {}), m.get("destination_backtest", {}), m.get("share_backtest", {})
    tti = m.get("tti_simulated_check", {})
    fi = res["feature_importance"].head(6)
    fi_txt = "、".join(f"{k}" for k in fi.index)
    prof = res["profile"]
    return f"""<section class="card" id="method"><h2>🔬 怎么算出来的</h2>
<ol class="steps">
<li><b>出行节奏</b>：抓取百度慧眼深圳每日迁出/迁入真实指数，以 9 月上中旬为「平日」，
套用 2024、2025 国庆的节奏曲线；假期开始后用已公布的真实数据逐日修正。</li>
<li><b>去哪儿</b>：深圳人假期去向城市占比直接来自百度真实排名；{len(TOWNS)} 个目的地分布于 {len(CITY_ADCODE)} 个城市，用各城市全来源迁入曲线
推算每天在当地的游客数，再按酒店接待能力、景点吸引力、社媒/OTA 热度分到城镇。</li>
<li><b>挤不挤</b>：游客数 ÷ 当地酒店民宿可接待人数；超过 100% 即「订房难、排长队」。</li>
<li><b>堵不堵</b>：LightGBM 分位数模型，输入当天出行/返程流量、小时节律、天气预报、线路热度，
输出每条高速逐小时拥堵指数及 80% 可能区间。</li>
<li><b>去哪玩</b>：好玩程度（自然/人文/娱乐/美食/亲子五维）扣除人多、车程、堵车、房价、下雨的影响。</li>
</ol>
<h3>准确度（用真实历史回测）</h3>
<div class="acc">
<div><div class="v">{mb.get('peak_day_hit_rate', 0) * 100:.0f}%</div><div class="l">出城高峰日判断命中率</div></div>
<div><div class="v">{mb.get('out_MAPE_%', '-')}%</div><div class="l">深圳逐日出行量误差</div></div>
<div><div class="v">{db.get('in_MAPE_median_%', '-')}%</div><div class="l">目的地逐日到访量误差(中位)</div></div>
<div><div class="v">{sb.get('persistence_MAE_pp', '-')}pp</div><div class="l">去向城市占比误差</div></div>
</div>
<div class="note">⚠️ 误差来自「留一年」检验：用 2024 年规律预测 2025、反之亦然。2025 年假期多 1 天（国庆+中秋），
逐日数值误差偏大，但高峰日判断稳定。高速拥堵模型因高德不开放历史数据，基于公开报告规律仿真训练，
只作相对强弱参考；配置高德 Key 后会用实时车速校准。</div>
<details><summary>模型细节与公式</summary><div class="inner">
<div class="formula">
平日基线 = 9/1-9/20 均值 = 迁出 <b>{prof.baseline_out:.2f}</b> / 迁入 <b>{prof.baseline_in:.2f}</b><br>
逐日指数 = 平日基线 × 参考年(<b>{'、'.join(map(str, prof.ref_years))}</b>)节奏倍数<br>
在地游客 V(t) = V(t−1)·(1 − 1/2.5天) + 超额到达(t)，其中 超额到达(t) = (迁入指数(t) − 平日基线)·(1 − t/(n+1))<br>
人次换算 = 指数 × <b>{m.get('migration_profile', {}).get('people_per_index', '-')}</b> 万人次/点<br>
拥挤度 = 在地游客 ÷ (客房 × 2.8人/间 × 1.6)<br>
城内分配 ∝ 接待能力 × 吸引力 × 社媒<b>^0.6</b> × OTA<b>^0.8</b>
</div>
<p>拥堵模型最看重的特征：{_e(fi_txt)}。仿真自洽检验（训练 {_e('、'.join(map(str, tti.get('train_years', []))))} →
校准 {tti.get('calibration_year', '-')} → 检验 {tti.get('test_year', '-')}）MAPE {tti.get('MAPE_%', '-')}%、
80% 区间覆盖 {tti.get('interval_coverage_80%', 0) * 100:.0f}%（仅说明模型学会了设定的规律）。
区间用留出年做保形校准（CQR），否则预测年的年际波动会让 P10/P90 系统性偏窄。</p>
<p>对照：经典辐射模型(Simini 2012)只用人口与距离推算去向，城市级误差 {sb.get('radiation_prior_MAE_pp', '-')}pp，
远大于直接用真实去向占比({sb.get('persistence_MAE_pp', '-')}pp)，因此本报告全部以真实迁徙数据为准。</p>
</div></details></section>"""


def _sources(res: dict) -> str:
    rows = []
    for name, p in res["metrics"].get("provenance", {}).items():
        cls, text = _status(p.get("status", ""))
        rows.append(f'<tr><td><b>{_e(name)}</b><div class="sub">{_e(p.get("detail", ""))}</div></td>'
                    f'<td><span class="st {cls}">{text}</span><div class="sub">{_e(p.get("updated", ""))}</div></td></tr>')
    return f"""<section class="card" id="src"><h2>🗂 数据来源与更新时间</h2>
<table class="src"><tbody>{''.join(rows)}</tbody></table></section>"""


# ======================= 组装 =======================

def _plotlyjs() -> str:
    """内联 plotly.js 使报告完全自包含, 无网(如微信里打开分享的 HTML)也能渲染图表.

    优先读 pip 包自带的 plotly.min.js(与已安装 plotly 同版本); 读不到再回退 CDN.
    已确认该文件不含 </script> / <!-- 字面量, 内联不会破坏 HTML.
    """
    try:
        from plotly.offline import get_plotlyjs, get_plotlyjs_version
        js = get_plotlyjs()
        if js and "</script>" not in js:
            return f"<script>{js}</script><!-- plotly {get_plotlyjs_version()} inline -->"
    except Exception:
        pass
    from plotly.offline import get_plotlyjs_version
    return f'<script src="{PLOTLY_CDN.format(v=get_plotlyjs_version())}" charset="utf-8"></script>'


def build_report(res: dict, out_path: str | Path) -> str:
    dates = sorted(res["crowding"]["date"].unique())
    gen = res["metrics"].get("generated_at") or stamp(None, "%Y-%m-%d %H:%M")
    body = "".join([
        _weather(res), _rhythm(res), _railway(res), _timetable(res), _roads(res), _crowd(res),
        f'<section class="card" id="hl"><h2>✨ 重点提醒</h2>{_highlights(res)}</section>',
        _recommend(res), _method(res), _sources(res),
    ])
    nav_items = [("weather", "天气"), ("rhythm", "出行节奏")]
    if (res.get("railway") or {}).get("city"):
        nav_items.append(("rail", "火车票"))
    nav_items += [("time", "错峰"), ("roads", "路况"), ("crowd", "人流"),
                  ("rec", "推荐"), ("method", "方法"), ("src", "数据")]
    nav = "".join(f'<a href="#{i}">{t}</a>' for i, t in nav_items)
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#ff2442">
<title>深圳国庆出行预测 {_md(dates[0])}-{_md(dates[-1])}</title>
{_plotlyjs()}
<style>{CSS}</style></head><body>
<header class="hero"><div class="hero-inner">
<h1>🚗 深圳国庆出行预测</h1>
<p class="lead">{_mdw(dates[0])} – {_mdw(dates[-1])} · 周边 {len(TOWNS)} 个目的地 × {len(HIGHWAYS)} 条出城高速</p>
<div class="badges"><span class="badge">更新于 {_e(gen[:16])}</span>
<span class="badge">{_mig_badge(res["metrics"].get("provenance", {}))}</span><span class="badge">{_wx_badge(res["metrics"].get("provenance", {}))}</span></div>
{_tldr(res)}</div></header>
<nav class="nav">{nav}</nav>
<main class="wrap">{body}
<p class="foot">预测仅供出行参考，请以当日实时路况为准。<br>数据：百度慧眼迁徙 · Open-Meteo · 国家统计局七普 · 高德（可选）</p>
</main>
<script>{SCROLL_HINT_JS}</script>
</body></html>"""
    # 原子写: 实时引擎每轮重写报告, 中途崩溃不应留下半个 HTML
    out = Path(out_path)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    return str(out)
