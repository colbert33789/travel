"""刷新仓库内置的百度迁徙真实数据快照(离线兜底 & 测试可复现).

用法: python scripts/update_reference.py
只保留建模所需窗口(各年 08-15~10-15), 控制文件体积.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_traffic.config import load_config  # noqa: E402
from holiday_traffic.data.hub import SNAPSHOT, DataHub  # noqa: E402


def _window(curve: dict) -> dict:
    return {k: round(v, 4) for k, v in sorted(curve.items()) if "0815" <= k[4:] <= "1015"}


def main():
    cfg = load_config()
    data = DataHub(cfg).migration_inputs(force=True)
    snap = {
        "source": "https://huiyan.baidu.com/migration (historycurve / cityrank)",
        "origin_adcode": cfg["project"]["origin_adcode"],
        "fetched_at": data.get("fetched_at", time.strftime("%Y-%m-%d %H:%M")),
        "out": _window(data["out"]), "in": _window(data["in"]), "rank": data["rank"],
        "dest": {c: {"in": _window(v["in"]), "out": _window(v["out"])}
                 for c, v in data["dest"].items()},
    }
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    print(f"快照已更新: {SNAPSHOT} | 深圳 {len(snap['out'])} 天 | "
          f"目的地 {len(snap['dest'])} 城 | rank {len(snap['rank'])} 日")


if __name__ == "__main__":
    main()
