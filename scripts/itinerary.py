"""自驾行程建议: 针对指定的一段假期日期给出可执行的出行方案.

与报告的区别: 报告是逐日榜(隐含"当天往返"), 本脚本按「多日行程」视角规划 ——
车程按天分摊、天气取最差日、拥挤度取最挤日, 并给出出发/返回时刻与天气调度。

用法:
  python scripts/itinerary.py                          # 默认 10/3-10/5 省内
  python scripts/itinerary.py --start 2026-10-03 --end 2026-10-05
  python scripts/itinerary.py --all                    # 不限省内
  python scripts/itinerary.py --top 8
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from holiday_traffic.config import holiday_dates, load_config  # noqa: E402
from holiday_traffic.geo import TOWN_INDEX, province_of  # noqa: E402
from holiday_traffic.recommend import trip_rank  # noqa: E402
from holiday_traffic.report import travel_advice  # noqa: E402

RAIN = {0: "无雨", 1: "小雨", 2: "中雨", 3: "大雨"}


def main() -> None:
    cfg = load_config()
    art = Path(cfg["output"]["artifacts_dir"])
    dates = holiday_dates(cfg)
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=dates[2] if len(dates) > 2 else dates[0])
    ap.add_argument("--end", default=dates[4] if len(dates) > 4 else dates[-1])
    ap.add_argument("--all", action="store_true", help="不限制广东省内")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    need = ["recommendations.csv", "traffic_predictions.csv", "weather.csv",
            "crowding_predictions.csv"]
    miss = [f for f in need if not (art / f).exists()]
    if miss:
        sys.exit(f"产出物缺失({miss}), 请先运行 scripts/run_pipeline.py")

    rec = pd.read_csv(art / "recommendations.csv")
    traffic = pd.read_csv(art / "traffic_predictions.csv")
    weather = pd.read_csv(art / "weather.csv")
    crowd = pd.read_csv(art / "crowding_predictions.csv")

    span = [d for d in dates if args.start <= d <= args.end]
    if not span:
        sys.exit(f"日期区间 {args.start}~{args.end} 不在假期 {dates[0]}~{dates[-1]} 内")
    scope = "广东省内" if not args.all else "不限省份"

    print("=" * 68)
    print(f"自驾行程建议  {span[0]} ~ {span[-1]}（{len(span)} 天） · {scope}")
    print("=" * 68)

    # ---- 天气: 决定哪天户外、哪天室内 ----
    w = weather[weather["date"].isin(span)]
    print("\n【天气】")
    per_day = {}
    for d in span:
        s = w[w["date"] == d]
        wet = int((s["rain_level"] >= 1).sum())
        per_day[d] = wet
        print(f"  {d[5:]}  全省 {wet}/{len(s)} 地点有雨 · 降水中位 {s['precip'].median():.1f}mm"
              f" · 最大 {s['precip'].max():.1f}mm")
    wettest = max(span, key=lambda d: per_day[d])
    driest = min(span, key=lambda d: per_day[d])
    print(f"  → {driest[5:]} 最干爽，安排户外/爬山/看海；"
          f"{wettest[5:]} 雨最广（{per_day[wettest]}/{len(w[w.date==wettest])}），"
          f"安排室内或雨景（温泉、溶洞、古镇、美食）")

    # ---- 出发 / 返回时刻 ----
    adv = {a["date"]: a for a in travel_advice(traffic)}
    print("\n【出发与返回】")
    for label, d in (("出发", span[0]), ("返回", span[-1])):
        a = adv.get(d)
        if not a:
            continue
        go = (f"{a['before']}点前" if a["am_peak"] else f"午前或 {a['after']}点后") \
            if a["before"] is not None else "全天均可"
        print(f"  {label} {d[5:]}: 建议 {go} · 拥堵 {a['jam']} · 最堵时 {a['peak_tti']:.2f}"
              f"（平时 1 小时 → 约 {int(a['peak_tti'] * 60)} 分钟）")

    # ---- 目的地排名 ----
    rank = trip_rank(rec, span, province_only=not args.all)
    if not len(rank):
        sys.exit("该区间无匹配目的地")
    print(f"\n【推荐目的地】（{scope}，按多日行程分）")
    print(f"  {'#':<3}{'目的地':<18}{'省份':<5}{'车程':>6}{'游玩':>6}"
          f"{'最挤日':>7}{'雨':>5}{'房价':>6}{'行程分':>8}")
    for i, r in enumerate(rank.head(args.top).itertuples(), 1):
        print(f"  {i:<3}{r.town:<18}{province_of(TOWN_INDEX[r.town]):<5}"
              f"{r.drive_hours:>5.1f}h{r.playability:>6.0f}"
              f"{r.crowding_index * 100:>6.0f}%{RAIN[r.rain_max]:>5}"
              f"{r.price_premium:>5.2f}x{r.trip_score:>8.1f}")

    # ---- 逐日天气细看(前 5 个候选) ----
    print("\n【候选目的地逐日天气】(前 5)")
    for r in rank.head(5).itertuples():
        rows = weather[(weather["place"] == r.town) & (weather["date"].isin(span))] \
            .sort_values("date")
        cells = " ".join(f"{x.date[5:]}:{RAIN[int(x.rain_level)]}{x.precip:.0f}mm"
                         for x in rows.itertuples())
        print(f"  {r.town:<18} {cells}")

    # ---- 拥挤度提醒 ----
    hot = crowd[(crowd["date"].isin(span))] \
        .groupby("town")["crowding_index"].max().sort_values(ascending=False)
    over = hot[hot >= 1.0]
    top3 = "、".join(f"{t}({v * 100:.0f}%)" for t, v in hot.head(3).items())
    print(f"\n【避挤提醒】区间内最挤：{top3}")
    if len(over):
        print(f"  ⚠ 接待能力吃满（≥100%）：{'、'.join(over.index)}，尽量避开")
    else:
        print("  区间内无接待能力吃满的目的地，比 10/1 宽松得多")

    print("\n注：行程分 = 游玩性 − 车程(按天分摊) − 路况 − 房价 − 雨天；"
          "拥挤度/天气取区间内最不利的一天。")


if __name__ == "__main__":
    main()
