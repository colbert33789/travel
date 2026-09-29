"""配置加载."""
from __future__ import annotations

import copy
import datetime as dt
import os
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


_DOTENV_DONE = False


def _load_dotenv() -> None:
    """读取项目根 .env 注入环境变量(不覆盖已有值), 使密钥无需写进配置/版本库.

    只做一次; 无 python-dotenv 依赖。优先级: 真实环境变量 > .env > config.yaml。
    """
    global _DOTENV_DONE
    if _DOTENV_DONE:
        return
    _DOTENV_DONE = True
    f = PROJECT_ROOT / ".env"
    if not f.exists():
        return
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _abs(value: str | Path) -> str:
    """配置里的相对路径一律按项目根解析.

    否则 uvicorn / cron 从不同工作目录启动时, artifacts 与 data/cache 会散落到
    别处, /report 与 /predictions/* 直接 503。
    """
    p = Path(value)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def load_config(path: str | None = None) -> dict:
    """返回配置副本(调用方可安全修改); 环境变量 AMAP_KEY 覆盖高德 Key.

    Key 优先级: 真实环境变量 > 项目根 .env > config.yaml 的 amap.key。
    """
    _load_dotenv()
    cfg = copy.deepcopy(_load(str(path or CONFIG_PATH)))
    if os.getenv("AMAP_KEY"):
        cfg.setdefault("amap", {})["key"] = os.environ["AMAP_KEY"]
    for section, keys in (("output", ("artifacts_dir", "report_html")),
                          ("data", ("cache_dir", "partner_observations_file"))):
        for key in keys:
            if (cfg.get(section) or {}).get(key):
                cfg[section][key] = _abs(cfg[section][key])
    return cfg


def holiday_dates(cfg: dict) -> list[str]:
    start = dt.date.fromisoformat(cfg["holiday"]["start"])
    end = dt.date.fromisoformat(cfg["holiday"]["end"])
    return [(start + dt.timedelta(k)).isoformat() for k in range((end - start).days + 1)]
