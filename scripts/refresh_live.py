"""实时引擎外部调度入口.

  python scripts/refresh_live.py             # 单轮
  python scripts/refresh_live.py --loop 20   # 常驻, 每 20 分钟一轮
  python scripts/refresh_live.py --force     # 忽略缓存 TTL 强制拉取

cron: */20 * * * * cd /path/to/travel && .venv/bin/python scripts/refresh_live.py >> logs/engine.log 2>&1
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_traffic.engine import RealtimeEngine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, default=0, help="循环间隔(分钟), 0 = 单轮")
    ap.add_argument("--force", action="store_true", help="忽略缓存 TTL")
    args = ap.parse_args()
    engine = RealtimeEngine()
    while True:
        s = engine.refresh(force=args.force)
        src = ", ".join(f"{k}:{v}" for k, v in s["sources"].items()
                        if v not in ("static", "prior", "simulated"))
        print(f"[{s['refreshed_at']}] tick={s['tick']} {s['elapsed_s']}s | {src} | "
              f"实测日 {s['observed_days'] or '无'}", flush=True)
        if args.loop <= 0:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
