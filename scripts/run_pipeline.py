"""训练 + 预测 + 报告.

  python scripts/run_pipeline.py            # 联网拉取实时数据(失败自动用缓存/内置快照)
  python scripts/run_pipeline.py --offline  # 仅用内置真实快照, 可复现
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_traffic.config import load_config  # noqa: E402
from holiday_traffic.pipeline import ForecastPipeline  # noqa: E402
from holiday_traffic.report import build_report  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略缓存 TTL 强制拉取")
    args = ap.parse_args()
    cfg = load_config()
    if args.offline:
        cfg["data"]["mode"] = "offline"

    res = ForecastPipeline(cfg).run(force=args.force)
    m = res["metrics"]

    print("=" * 64)
    print("数据来源:")
    for k, v in m["provenance"].items():
        print(f"  {k:<22} {v['status']:<28} {v['detail']}")
    mb, db, sb = m["migration_backtest"], m["destination_backtest"], m["share_backtest"]
    print("\n真实数据回测(留一年):")
    print(f"  深圳逐日迁出 MAPE {mb['out_MAPE_%']}% | 迁入 MAPE {mb['in_MAPE_%']}% | "
          f"高峰日命中 {mb['peak_day_hit_rate']:.0%}")
    print(f"  目的地逐日迁入 MAPE 中位 {db['in_MAPE_median_%']}% (均值 {db['in_MAPE_%']}%)")
    print(f"  去向城市占比 MAE: 持续性 {sb['persistence_MAE_pp']}pp vs 辐射先验 "
          f"{sb['radiation_prior_MAE_pp']}pp")
    t = m["tti_simulated_check"]
    print(f"  TTI 仿真自洽: MAPE {t['MAPE_%']}% | 区间覆盖 {t['interval_coverage_80%']:.0%}")

    mig = res["migration"]
    print("\n逐日迁徙(规模指数 / 万人次):")
    for r in mig.itertuples():
        obs = " ✓实测" if r.observed_out == r.observed_out and r.observed_out is not None else ""
        print(f"  {r.date} 出 {r.out_index if r.out_index == r.out_index else '-':>6} "
              f"入 {r.in_index:>6} 在外 {r.away_stock_万人 if r.away_stock_万人 == r.away_stock_万人 else '-':>6}{obs}")

    c = res["crowding"]
    peak = c.loc[c.groupby("town")["crowding_index"].idxmax()].nlargest(6, "crowding_index")
    print("\n最拥挤目的地(峰值日 游客/接待能力):")
    for r in peak.itertuples():
        print(f"  {r.town:<14} {r.date} {r.crowding_index:.0%} {r.crowding_level}")

    rec = res["recommendations"]
    print(f"\n{rec['date'].min()} 推荐 Top5:")
    for r in rec[rec["date"] == rec["date"].min()].head(5).itertuples():
        print(f"  #{r.daily_rank} {r.town:<14} {r.recommend_score:5.1f} | {r.reason}")

    out = build_report(res, ROOT / cfg["output"]["report_html"])
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
