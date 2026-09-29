"""FastAPI 预测服务 (实时引擎驱动).

启动: uvicorn holiday_traffic.api:app --port 8300 --app-dir src
需先运行 scripts/run_pipeline.py 训练模型并生成基线产出物.
engine.enabled=true 时服务内置定时刷新; 产出物按 mtime 热加载.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .config import load_config
from .engine import RealtimeEngine

CFG = load_config()
ART = Path(CFG["output"]["artifacts_dir"])
ENGINE = RealtimeEngine(CFG)
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = None
    eng = CFG.get("engine", {})
    if eng.get("enabled"):
        interval = max(1, int(eng.get("interval_minutes", 30))) * 60

        async def loop():
            while True:
                try:
                    await asyncio.to_thread(ENGINE.refresh)
                except Exception as e:  # noqa: BLE001 - 后台任务不能因单轮失败退出
                    print(f"[engine] refresh failed: {e}", flush=True)
                await asyncio.sleep(interval)

        task = asyncio.create_task(loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="深圳国庆出行预测 API", version="2.0.0", lifespan=_lifespan,
              description="目的地拥挤度 · 高速拥堵 · 迁徙节奏 · 推荐(百度慧眼/Open-Meteo 实时数据)")


def _load(name: str) -> pd.DataFrame:
    f = ART / name
    if not f.exists():
        raise HTTPException(503, f"产出物不存在, 请先运行 scripts/run_pipeline.py: {name}")
    mtime = f.stat().st_mtime
    if name not in _CACHE or _CACHE[name][0] < mtime:
        _CACHE[name] = (mtime, pd.read_csv(f))
    return _CACHE[name][1]


def _json(name: str) -> dict:
    f = ART / name
    if not f.exists():
        raise HTTPException(503, f"{name} 不存在")
    return json.loads(f.read_text(encoding="utf-8"))


def _check_date(df: pd.DataFrame, date: Optional[str]) -> pd.DataFrame:
    if not date:
        return df
    if date not in set(df["date"]):
        raise HTTPException(404, f"日期不在预测范围: {date}")
    return df[df["date"] == date]


def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


@app.get("/health")
def health():
    return {"status": "ok", "ready": (ART / "traffic_predictions.csv").exists(),
            "mode": CFG["data"].get("mode")}


@app.get("/report", response_class=FileResponse)
def report():
    f = Path(CFG["output"]["report_html"])
    if not f.exists():
        raise HTTPException(503, "报告未生成")
    return FileResponse(f, media_type="text/html")


@app.get("/predictions/summary")
def summary():
    t, c, m = _load("traffic_predictions.csv"), _load("crowding_predictions.csv"), \
        _load("migration_forecast.csv")
    hol = m[m["out_index"].notna()]
    # 与报告口径一致: 出城高峰日按「假期诱发超额迁出」最大计(而非绝对指数)
    return {
        "dates": sorted(t["date"].unique().tolist()),
        "peak_outflow_day": hol.loc[hol["excess_out_万人次"].idxmax(), "date"],
        "peak_return_day": m.loc[m["in_index"].idxmax(), "date"],
        "worst_highway": _records(t.loc[[t["tti_pred"].idxmax()]])[0],
        "most_crowded": _records(c.loc[[c["crowding_index"].idxmax()]])[0],
    }


@app.get("/predictions/migration")
def migration():
    return _records(_load("migration_forecast.csv"))


@app.get("/predictions/weather")
def weather(place: str = Query("深圳")):
    df = _load("weather.csv")
    if place not in set(df["place"]):
        raise HTTPException(404, f"未知地点: {place}")
    return _records(df[df["place"] == place])


@app.get("/predictions/crowding")
def crowding(date: Optional[str] = None):
    df = _check_date(_load("crowding_predictions.csv"), date)
    return _records(df.sort_values("crowding_index", ascending=False))


@app.get("/predictions/traffic")
def traffic(date: Optional[str] = None, highway_id: Optional[str] = None,
            peak_only: bool = Query(False, description="仅 8-20 时")):
    df = _check_date(_load("traffic_predictions.csv"), date)
    if highway_id:
        df = df[df["highway_id"] == highway_id]
    if peak_only:
        df = df[df["hour"].between(8, 20)]
    return _records(df)


@app.get("/predictions/recommendations")
def recommendations(date: Optional[str] = None, top_n: int = Query(10, ge=1, le=40)):
    df = _check_date(_load("recommendations.csv"), date)
    return _records(df[df["daily_rank"] <= top_n])


@app.get("/predictions/social-heat")
def social_heat(town: Optional[str] = None):
    df = _load("social_heat.csv")
    if town:
        if town not in set(df["town"]):
            raise HTTPException(404, f"未知目的地: {town}")
        return _records(df[df["town"] == town])
    top = df.groupby("town")["social_heat"].mean().sort_values(ascending=False).head(10)
    return [{"town": k, "social_heat": round(v, 3)} for k, v in top.items()]


@app.get("/predictions/ota")
def ota(metric: str = Query("booking_heat", pattern="^(booking_heat|price_premium|sell_out)$")):
    return _records(_load("ota_signals.csv").sort_values(metric, ascending=False))


@app.get("/observations/lodging-pois")
def lodging_pois(town: Optional[str] = None):
    """高德附近住宿地点数量；不能代表房量、价格或 OTA 预订。"""
    df = _load("lodging_supply.csv")
    if town:
        if town not in set(df["town"]):
            raise HTTPException(404, f"未知目的地: {town}")
        df = df[df["town"] == town]
    return _records(df)


@app.get("/observations/baidu-lodging-pois")
def baidu_lodging_pois(town: Optional[str] = None):
    """百度酒店/民宿/宾馆关键词检索数；不是可订房量或社交热度。"""
    df = _load("baidu_lodging_supply.csv")
    if town:
        if town not in set(df["town"]):
            raise HTTPException(404, f"未知目的地: {town}")
        df = df[df["town"] == town]
    return _records(df)


@app.get("/observations/tencent-drive")
def tencent_drive(town: Optional[str] = None):
    """深圳出发当前驾车 ETA；并非假期预测或指定高速实测。"""
    df = _load("tencent_drive_eta.csv")
    if town:
        if town not in set(df["town"]):
            raise HTTPException(404, f"未知目的地: {town}")
        df = df[df["town"] == town]
    return _records(df)


@app.get("/predictions/railway")
def railway():
    """12306 余票需求(假期首日方向售罄率 + 深穗逐日曲线)."""
    return _json("railway_demand.json")


@app.get("/metrics")
def metrics():
    return _json("metrics.json")


@app.get("/engine/status")
def engine_status():
    f = ART / "engine_status.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"tick": 0}


@app.post("/engine/refresh")
def engine_refresh(force: bool = False):
    try:
        status = ENGINE.refresh(force=force)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    _CACHE.clear()
    return status
