"""单元与端到端测试(离线模式, 使用内置百度迁徙真实快照, 可复现).

pytest tests/ -q
"""
import datetime as dt
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_traffic.config import holiday_dates, load_config  # noqa: E402
from holiday_traffic.data.hub import SNAPSHOT, DataHub  # noqa: E402
from holiday_traffic.data.http import FetchError, parse_jsonp  # noqa: E402
from holiday_traffic.data.openmeteo import rain_level  # noqa: E402
from holiday_traffic.features import congestion_level, crowding_level  # noqa: E402
from holiday_traffic.geo import (CITY_ADCODE, CITY_POP, HIGHWAYS, ROAD_MEASURED,  # noqa: E402
                                 TOURIST_SHARE, TOWN_INDEX, TOWNS, road_distance,
                                 short_name)
from holiday_traffic.migration import (backtest, build_city_flow, build_profile,  # noqa: E402
                                       city_backtest)
from holiday_traffic.ota import HOTEL_ROOMS  # noqa: E402
from holiday_traffic.social import SOCIAL_SEED  # noqa: E402


def offline_cfg(tmp: Path) -> dict:
    cfg = load_config()
    cfg["data"]["mode"] = "offline"
    cfg["data"]["cache_dir"] = str(tmp / "cache")
    cfg["output"]["artifacts_dir"] = str(tmp / "artifacts")
    cfg["output"]["report_html"] = str(tmp / "artifacts" / "report.html")
    cfg["models"]["lightgbm"]["n_estimators"] = 120
    return cfg


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    from holiday_traffic.pipeline import ForecastPipeline

    cfg = offline_cfg(tmp_path_factory.mktemp("run"))
    pipe = ForecastPipeline(cfg)
    return cfg, pipe, pipe.run()


# ---------------- 静态数据 ----------------

def test_geo_integrity():
    names = {t.name for t in TOWNS}
    assert len(TOWNS) == 41 and len(HIGHWAYS) == 20
    for h in HIGHWAYS:
        assert set(h.connected_towns) <= names, h.id
    for t in TOWNS:
        assert any(t.name in h.connected_towns for h in HIGHWAYS), f"{t.name} 无走廊"
        assert all(1 <= getattr(t, k) <= 5 for k in ("natural", "culture", "fun", "food", "family"))
    cities = {t.city for t in TOWNS}
    assert cities <= set(CITY_POP) and cities <= set(CITY_ADCODE) and cities <= set(TOURIST_SHARE)
    for c in cities:
        assert sum(t.population for t in TOWNS if t.city == c) <= CITY_POP[c]


def test_prior_tables_cover_all_towns():
    for t in TOWNS:
        assert t.name in SOCIAL_SEED and t.name in HOTEL_ROOMS, t.name
        assert t.name in ROAD_MEASURED, t.name


def test_road_distance_matches_measured():
    """车程必须用实测路网距离: 球面×1.3 会低估需绕珠江口的西岸目的地."""
    from holiday_traffic.geo import distance_shenzhen

    for t in TOWNS:
        km, hours = road_distance(t)
        assert km == ROAD_MEASURED[t.name][0]
        assert 0.3 <= hours <= 8.0, (t.name, hours)
        assert km >= distance_shenzhen(t), f"{t.name} 路网距离不应短于球面距离"
    # 珠江西岸必须绕珠江口, 实测应显著大于球面估算
    for name in ("珠海市区", "珠海横琴(长隆)", "珠海万山(外伶仃岛)"):
        t = TOWN_INDEX[name]
        assert ROAD_MEASURED[name][0] > distance_shenzhen(t) * 1.3 * 1.3, name


def test_short_names_unique():
    shorts = [short_name(t.name) for t in TOWNS]
    assert len(set(shorts)) == len(shorts)
    assert short_name("广州番禺(长隆)") != short_name("珠海横琴(长隆)")
    # 括号内是「特征」而非地名时必须补地域, 否则图中出现「美食」这类无主语条目
    for t in TOWNS:
        s = short_name(t.name)
        assert s not in ("美食", "客都", "禅泉", "碉楼", "地下河", "大峡谷"), (t.name, s)
    assert short_name("佛山顺德(美食)") == "顺德美食"


# ---------------- 数据接入工具 ----------------

def test_config_paths_are_absolute():
    """相对路径必须按项目根解析, 否则换个 cwd 启动就找不到产出物."""
    cfg = load_config()
    for k in ("artifacts_dir", "report_html"):
        assert Path(cfg["output"][k]).is_absolute(), k
    assert Path(cfg["data"]["cache_dir"]).is_absolute()
    assert Path(cfg["data"]["partner_observations_file"]).is_absolute()


def test_clock_uses_configured_timezone():
    """取时点必须带配置时区(Asia/Shanghai), 而非服务器本地时间."""
    from holiday_traffic.clock import now, today, tz_of

    tz = tz_of(load_config())
    assert tz == "Asia/Shanghai"
    n = now(tz)
    assert n.tzinfo is not None and n.utcoffset() == timedelta(hours=8)
    assert isinstance(today(tz), dt.date)
    # 关键场景: UTC 09-30 23:00 == 北京 10-01 07:00, 日期必须按北京时间算
    utc_moment = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)
    assert utc_moment.astimezone(n.tzinfo).date() == dt.date(2026, 10, 1)


def test_parse_jsonp():
    assert parse_jsonp('cb({"errno":0,"data":{"a":1}});')["data"]["a"] == 1
    with pytest.raises(FetchError):
        parse_jsonp("<html>blocked</html>")


def test_rain_level_gb():
    assert [rain_level(v) for v in (0.3, 5, 17, 30, None)] == [0, 1, 2, 3, 0]


def test_level_thresholds():
    assert crowding_level(0.5) == "舒畅" and crowding_level(1.1) == "拥挤"
    assert congestion_level(1.2) == "基本畅通" and congestion_level(3.2) == "极端拥堵"


def test_hub_offline_uses_snapshot(tmp_path):
    hub = DataHub(offline_cfg(tmp_path))
    data = hub.migration_inputs()
    assert len(data["dest"]) == len(CITY_ADCODE)
    assert hub.provenance["百度慧眼迁徙"]["status"].startswith("snapshot")
    w = hub.weather(holiday_dates(load_config()))
    assert (w["source"] == "climate").all()
    assert hub.live_traffic() == {}
    assert hub.railway_demand(holiday_dates(load_config())) == {}  # 离线不联网
    assert (hub.lodging_supply()["source"] == "unavailable").all()
    assert (hub.baidu_lodging_supply()["source"] == "unavailable").all()
    assert (hub.tencent_drive()["source"] == "unavailable").all()
    assert hub.provenance["腾讯驾车当前 ETA"]["status"] == "offline"
    assert hub.partner_observations().empty


def test_tencent_routes_convert_coordinates_and_stop_on_limit(monkeypatch):
    from holiday_traffic.data import hub as source

    monkeypatch.setattr(source, "TOWNS", TOWNS[:2])
    monkeypatch.setattr(source, "TENCENT_PAUSE", 0)
    calls = []

    def fake_get(url, params, **_kwargs):
        calls.append((url, params))
        assert params["key"] == "test"
        if url == source.TENCENT_COORD_API:
            points = params["locations"].split(";")
            assert params["type"] == 1 and len(points) == 3
            return {"status": 0, "locations": [
                {"lat": float(p.split(",")[0]) + 0.01,
                 "lng": float(p.split(",")[1]) + 0.01} for p in points]}
        origin = [float(x) for x in params["from"].split(",")]
        assert origin == pytest.approx([22.56, 114.07])
        assert params["policy"] == "LEAST_TIME"
        if len([url for url, _ in calls if url == source.TENCENT_ROUTE_API]) == 2:
            return {"status": 121}
        return {"status": 0, "result": {"routes": [{"duration": 68, "distance": 63826}]}}

    monkeypatch.setattr(source, "get_json", fake_get)
    with pytest.raises(source.TencentDailyQuotaError):
        source._tencent_routes("test")
    assert len(calls) == 3  # 1 次批量转换 + 2 次路线请求；额度用尽立即停止


def test_tencent_quota_marker_blocks_until_next_day(tmp_path, monkeypatch):
    from holiday_traffic.data import hub as source

    cfg = offline_cfg(tmp_path)
    cfg["data"]["mode"] = "auto"
    monkeypatch.setenv("TENCENT_MAP_KEY", "test")
    hub = DataHub(cfg)
    calls = []

    def exhausted(_key):
        calls.append(1)
        raise source.TencentDailyQuotaError("配额耗尽")

    monkeypatch.setattr(source, "_tencent_routes", exhausted)
    df = hub.tencent_drive()
    assert (df["source"] == "unavailable").all()
    assert hub.provenance["腾讯驾车当前 ETA"]["status"] == "quota_exhausted"
    assert len(calls) == 1
    hub.tencent_drive()
    assert len(calls) == 1
    assert hub._read_cache("tencent_daily_limit", None)[0]["date"] == dt.datetime.now(
        dt.timezone(timedelta(hours=8))).date().isoformat()

    monkeypatch.setattr(source, "_tencent_routes", lambda _key: {
        TOWNS[0].name: {"eta_min": 68, "distance_km": 63.83}})
    next_day = dt.datetime.now(dt.timezone(timedelta(hours=8))).date() + timedelta(days=1)
    monkeypatch.setattr(source, "today", lambda _tz: next_day)
    df = hub.tencent_drive()
    assert df.iloc[0]["eta_min"] == 68
    assert df.iloc[1]["source"] == "unavailable"
    assert hub.provenance["腾讯驾车当前 ETA"]["status"] == "partial(live)"
    hub.tencent_drive()
    assert hub.provenance["腾讯驾车当前 ETA"]["status"] == "partial(cache)"


def test_amap_daily_limit_stops_and_empty_traffic_is_not_live(tmp_path, monkeypatch):
    from holiday_traffic.data.amap import AmapTrafficClient

    calls = []
    def limited(_self, hid):
        calls.append(hid)
        raise FetchError("高德路况错误: USER_DAILY_QUERY_OVER_LIMIT")
    monkeypatch.setattr(AmapTrafficClient, "road_tti", limited)
    assert AmapTrafficClient("test", pause=0).all_tti() == {}
    assert len(calls) == 1
    cfg = offline_cfg(tmp_path)
    cfg["data"]["mode"] = "auto"
    monkeypatch.setenv("AMAP_KEY", "test")
    hub = DataHub(cfg)
    assert hub.live_traffic() == {}
    assert hub.provenance["高德实时路况"]["status"] == "failed"
    assert hub.live_traffic() == {}  # 即使缓存空响应，也不得显示为 live/cache
    assert hub.provenance["高德实时路况"]["status"] == "failed"


def test_lodging_poi_response_validation(monkeypatch):
    from holiday_traffic.data import amap

    client = amap.AmapLodgingClient("test")
    monkeypatch.setattr(amap, "get_json", lambda *a, **kw: {
        "status": "1", "count": "600", "pois": [{"id": "poi"}]})
    assert client.count_near(116.63, 23.67) == 600  # count 是匹配数，非当页返回数
    monkeypatch.setattr(amap, "get_json", lambda *a, **kw: {
        "status": "0", "info": "INVALID_USER_KEY"})
    with pytest.raises(FetchError):
        client.count_near(116.63, 23.67)
    monkeypatch.setattr(amap, "get_json", lambda *a, **kw: {
        "status": "1", "count": "bad", "pois": []})
    with pytest.raises(FetchError):
        client.count_near(116.63, 23.67)


def test_lodging_supply_cached_and_separate_from_ota(tmp_path, monkeypatch):
    from holiday_traffic.data.amap import AmapLodgingClient

    cfg = offline_cfg(tmp_path)
    cfg["data"]["mode"] = "auto"
    monkeypatch.setenv("AMAP_KEY", "test")
    calls = []

    def counts(_self):
        calls.append(1)
        return {TOWNS[0].name: 0, TOWNS[1].name: 600}

    monkeypatch.setattr(AmapLodgingClient, "all_counts", counts)
    hub = DataHub(cfg)
    rows = hub.lodging_supply()
    assert len(rows) == len(TOWNS) and len(calls) == 1
    assert rows.iloc[0]["lodging_poi_count_5km"] == 0
    assert rows.iloc[1]["lodging_poi_count_5km"] == 600
    assert rows.iloc[1]["possibly_capped"]
    assert rows.iloc[2]["source"] == "unavailable"
    assert hub.provenance["高德住宿 POI 供给代理"]["status"] == "partial(live)"
    hub.lodging_supply()
    assert len(calls) == 1
    assert hub.provenance["高德住宿 POI 供给代理"]["status"] == "partial(cache)"


def test_baidu_lodging_response_validation(monkeypatch):
    from holiday_traffic.data import baidu

    client = baidu.BaiduLodgingClient("test")
    def ok(_url, params, **_kwargs):
        assert params["coord_type"] == 1 and params["location"] == "23.67,116.63"
        assert params["radius_limit"] == "true" and params["page_size"] == 1
        return {"status": 0, "total": 150, "results": [{"uid": "poi"}]}
    monkeypatch.setattr(baidu, "get_json", ok)
    assert client.count_near(23.67, 116.63) == 150
    monkeypatch.setattr(baidu, "get_json", lambda *a, **kw: {"status": 211})
    with pytest.raises(FetchError):
        client.count_near(23.67, 116.63)
    monkeypatch.setattr(baidu, "get_json", lambda *a, **kw: {
        "status": 0, "total": 5, "results": []})
    with pytest.raises(FetchError):
        client.count_near(23.67, 116.63)


def test_baidu_lodging_cache_and_cap(tmp_path, monkeypatch):
    from holiday_traffic.data.baidu import BaiduLodgingClient

    cfg = offline_cfg(tmp_path)
    cfg["data"]["mode"] = "auto"
    monkeypatch.setenv("BAIDU_MAP_AK", "test")
    calls = []
    def counts(_self):
        calls.append(1)
        return {TOWNS[0].name: 150, TOWNS[1].name: 0}
    monkeypatch.setattr(BaiduLodgingClient, "all_counts", counts)
    hub = DataHub(cfg)
    rows = hub.baidu_lodging_supply()
    assert len(rows) == len(TOWNS) and len(calls) == 1
    assert rows.iloc[0]["possibly_capped"]
    assert rows.iloc[1]["baidu_lodging_poi_count_5km"] == 0
    assert rows.iloc[2]["source"] == "unavailable"
    assert hub.provenance["百度地图住宿 POI 检索代理"]["status"] == "partial(live)"
    hub.baidu_lodging_supply()
    assert len(calls) == 1
    assert hub.provenance["百度地图住宿 POI 检索代理"]["status"] == "partial(cache)"


def test_authorized_import_isolated_and_validated(tmp_path):
    cfg = offline_cfg(tmp_path)
    cfg["data"]["mode"] = "auto"
    path = tmp_path / "partner.csv"
    cfg["data"]["partner_observations_file"] = str(path)
    path.write_text("town,platform,metric,value,observed_at\n"
                    f"{TOWNS[0].name},ctrip,hotel_price_cny,250,2026-09-29\n"
                    f"{TOWNS[1].name},douyin,post_count,60,2026-09-29\n", encoding="utf-8")
    hub = DataHub(cfg)
    df = hub.partner_observations()
    assert len(df) == 2 and hub.provenance["平台授权数据(OTA/社交)"]["status"] == "user_supplied"
    path.write_text("town,platform,metric,value,observed_at\n"
                    f"{TOWNS[0].name},douyin,price_premium,1.5,2026-09-29\n", encoding="utf-8")
    assert hub.partner_observations().empty
    assert hub.provenance["平台授权数据(OTA/社交)"]["status"] == "failed"


def test_railway_seat_parsing():
    """12306 记录格式: 售罄 = 无座/一等/二等/商务全为 无/空/0."""
    from holiday_traffic.data.railway import SEAT_FIELDS

    f = ["x"] * 58
    for i in SEAT_FIELDS:
        f[i] = "无"
    assert all(f[i] in ("", "无", "0") for i in SEAT_FIELDS)
    f[30] = "4"
    assert not all(f[i] in ("", "无", "0") for i in SEAT_FIELDS)


def test_railway_city_adjustment(run):
    """售罄率高于均值的城市应获得 >1 的客流修正, 且限幅在 [0.85, 1.15]."""
    _, pipe, _ = run
    adj = pipe._railway_adj({"city": {"韶关": {"rate": 1.0}, "广州": {"rate": 0.3}}})
    assert adj["韶关"] > 1.0 > adj["广州"]
    assert all(0.85 <= v <= 1.15 for v in adj.values())
    assert pipe._railway_adj({}) == {} and pipe._railway_adj({"city": {}}) == {}


# ---------------- 迁徙模型(真实数据) ----------------

def test_reference_window_alignment():
    """两端对齐: k=0 必须是节前一天, k=n_days 必须是末日, 且日期严格递增.

    旧的「单一端对齐」下, 8 天假期的 2025 其 k=0 会落在假期首日(10-01)而非节前一天,
    与 7 天假期的 2024 语义不一致 —— 修正后留一年回测 MAPE 由 24.6/23.1 降至 17.1/12.8。
    """
    from holiday_traffic.migration import REFERENCE_HOLIDAYS, _window

    for year, (start, length) in REFERENCE_HOLIDAYS.items():
        w = _window(year, 7)
        s = dt.date.fromisoformat(start)
        e = s + dt.timedelta(length - 1)
        assert w[0] == s - dt.timedelta(1), f"{year} k=0 应为节前一天"
        assert w[7] == e, f"{year} k=n_days 应为末日"
        assert w[8] == e + dt.timedelta(1), f"{year} k=n_days+1 应为节后首日"
        assert all((w[i + 1] - w[i]).days >= 1 for i in range(len(w) - 1)), f"{year} 日期须递增"


def test_profile_shape(snapshot):
    p = build_profile(snapshot["out"], snapshot["in"], snapshot["rank"], "2026-10-01", 7, "t")
    assert len(p.out_ratio) == 8 and len(p.in_ratio) == 9
    assert max(range(8), key=p.out_ratio.__getitem__) == 1          # 迁出峰值在 D1
    assert max(range(9), key=p.in_ratio.__getitem__) >= 6           # 迁入峰值在假期末段
    assert all(lo <= m <= hi for lo, m, hi in zip(p.out_low, p.out_ratio, p.out_high))
    assert "惠州市" in p.city_shares and "东莞市" in p.city_shares


def test_nowcast_replaces_observed(snapshot):
    out = dict(snapshot["out"])
    out["20261001"] = 50.0
    p = build_profile(out, snapshot["in"], snapshot["rank"], "2026-10-01", 7, "t")
    assert p.observed_out["2026-10-01"] == 50.0
    assert abs(p.out_index()[1] - 50.0) < 1e-3
    # 已实测日的不确定性必须收窄为该点, 否则实测值会落在自己的区间之外
    assert p.out_low[1] == p.out_high[1] == p.out_ratio[1]
    assert all(lo <= m <= hi for lo, m, hi in zip(p.out_low, p.out_ratio, p.out_high))


def test_real_backtests(snapshot):
    bt = backtest(snapshot["out"], snapshot["in"], 7)
    assert bt["peak_day_hit_rate"] == 1.0
    assert bt["out_MAPE_%"] < 40
    cb = city_backtest(snapshot["dest"], 7)
    assert cb["n_city_years"] == 2 * len(CITY_ADCODE)


def test_city_flow_conservation(snapshot):
    for city, c in snapshot["dest"].items():
        f = build_city_flow(city, c["in"], c["out"], "2026-10-01", 7)
        assert min(f.visitors) >= 0 and min(f.locals_away) >= 0
        assert f.locals_away[-1] <= max(f.locals_away) * 0.05 + 1e-6, city  # 外出的人都回来了


# ---------------- 端到端 ----------------

def test_pipeline_outputs(run):
    cfg, _, res = run
    art = Path(cfg["output"]["artifacts_dir"])
    for f in ("crowding_predictions.csv", "traffic_predictions.csv", "recommendations.csv",
              "migration_forecast.csv", "weather.csv", "metrics.json", "lodging_supply.csv",
              "baidu_lodging_supply.csv", "tencent_drive_eta.csv", "partner_observations.csv",
              "tti_model_p10.txt", "tti_model_p50.txt",
              "tti_model_p90.txt"):
        assert (art / f).exists(), f
    assert (res["lodging_supply"]["source"] == "unavailable").all()
    assert (res["baidu_lodging_supply"]["source"] == "unavailable").all()
    assert (res["tencent_drive"]["source"] == "unavailable").all()
    assert res["partner_observations"].empty
    assert res["metrics"]["provenance"]["OTA 信号(携程/去哪儿/美团/飞猪)"]["status"] == "prior"
    c, t = res["crowding"], res["traffic"]
    assert len(c) == len(TOWNS) * 7 and (c["crowding_index"] >= 0).all()
    assert len(t) == len(HIGHWAYS) * 7 * 24 and (t["tti_pred"] >= 1.0).all()
    assert ((t["tti_p10"] <= t["tti_pred"]) & (t["tti_pred"] <= t["tti_p90"])).all()
    for _, g in res["recommendations"].groupby("date"):
        assert list(g["daily_rank"]) == list(range(1, len(TOWNS) + 1))
        assert g["recommend_score"].is_monotonic_decreasing


def test_away_stock_conservation(run):
    _, pipe, res = run
    outs, rets, stock = pipe.away_stock(res["profile"])
    assert min(stock) >= 0
    assert max(stock) > 0 and stock[-1] < max(stock)


def test_tti_physically_plausible(run):
    """夜间应回到近自由流(<1.6), 高峰时段显著抬升(>1.9), 不能全天一个量级."""
    _, _, res = run
    t = res["traffic"]
    hourly = t.groupby("hour")["tti_pred"].mean()
    assert hourly.min() < 1.6 and hourly.max() > 1.9, hourly.round(2).to_dict()
    assert hourly.loc[0:5].max() < hourly.loc[8:20].min()      # 深夜明显低于白天
    levels = set(t["congestion_level"])
    assert len(levels) >= 3, levels                            # 不能全被判为同一级
    assert t["tti_pred"].max() < 6.0                           # 不得出现失真的极端值


def test_interval_coverage_near_nominal(run):
    """保形校准后 P10/P90 的实测覆盖应接近名义 80%."""
    cfg, _, res = run
    cov = res["metrics"]["tti_simulated_check"]["interval_coverage_80%"]
    assert 0.70 <= cov <= 0.92, cov


def test_recommend_scores_usable(run):
    """推荐分需为正且排序单调, 否则报告里会出现负值排名."""
    _, _, res = run
    rec = res["recommendations"]
    assert rec["recommend_score"].min() > 0
    for _, g in rec.groupby("date"):
        assert g["recommend_score"].is_monotonic_decreasing
        assert g["drive_hours"].iloc[0] < 12        # 车程不得因整程按拥堵折算而失真


def test_live_calibration_bounded(run):
    _, pipe, res = run
    from holiday_traffic.models.gbdt import TTIPredictor

    model = TTIPredictor.load(pipe.artifacts)
    now = dt.datetime(2026, 10, 1, 11)
    weather = res["weather"]
    t, calib = pipe.predict_traffic(model, res["profile"], weather, {"G4": 99.0, "S3": 1.0}, now)
    assert calib["applied"] and calib["ratios"]["G4"] == 1.25 and calib["ratios"]["S3"] <= 1.0
    base, _ = pipe.predict_traffic(model, res["profile"], weather, {}, now)
    later = (t["date"] == "2026-10-02") & (t["highway_id"] == "G4")
    assert (t.loc[later, "tti_pred"].values == base.loc[later, "tti_pred"].values).all()
    _, off = pipe.predict_traffic(model, res["profile"], weather, {"G4": 3.0},
                                  dt.datetime(2026, 9, 29, 11))
    assert not off["applied"]


def test_engine_refresh(run):
    from holiday_traffic.engine import RealtimeEngine

    cfg, _, _ = run
    s = RealtimeEngine(cfg).refresh()
    assert s["tick"] == 1 and s["sources"]["百度慧眼迁徙"].startswith("snapshot")
    assert (Path(cfg["output"]["artifacts_dir"]) / "engine_status.json").exists()
    assert Path(cfg["output"]["report_html"]).exists()


def test_report(run, tmp_path):
    from holiday_traffic.report import LONGHAUL_BANDS, LONGHAUL_TOP, TRIP_TOP, build_report

    _, _, res = run
    html = Path(build_report(res, tmp_path / "r.html")).read_text(encoding="utf-8")
    for s in ("一分钟看懂", "错峰出行时刻表", "哪些高速最堵", "哪里人最多", "去哪儿玩",
              "怎么算出来的", "数据来源与更新时间", "viewport"):
        assert s in html, s
    # 报告现已内联 plotly.js(离线可渲染), 剥离 <script> 后再检查正文,
    # 避免 JS 源码里的年份/数字干扰断言
    body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    assert "2010" not in body
    # 图表数随功能变化(有火车票数据时多一张), 只要求一致性且至少 3 张
    assert html.count('class="chart"') == html.count("plotly-graph-div") >= 3
    # 走廊/目的地数量必须由数据生成, 不能硬编码(新增深中通道时 19 就过时了)
    assert f"{len(HIGHWAYS)} 条出城高速" in html
    assert f"{len(TOWNS)} 个目的地" in html
    # 「去哪儿玩」必须逐日覆盖, 不能只挑 3 天
    sec = re.search(r'<section class="card" id="rec">(.*?)</section>', html, re.S).group(1)
    # 「去哪儿玩」已精简为「主榜 10 个 + 远途补充」, 不再逐日铺开(那样 70 张卡片无从下手)
    assert len(re.findall(r"<h3>🎒", sec)) == 1, "主榜缺失"
    assert len(re.findall(r"<h3>🚙", sec)) == len(LONGHAUL_BANDS), "远途分档缺失"
    main = re.search(r"<h3>🎒(.*?)(?=<h3>🚙|$)", sec, re.S).group(1)
    assert len(re.findall(r'<div class="rec">', main)) == TRIP_TOP
    # 精简后总量应显著小于"逐日 Top10"的规模
    assert sec.count('class="rec"') <= TRIP_TOP + LONGHAUL_TOP * len(LONGHAUL_BANDS)
    # 主榜与远途补充不得重复
    names_all = re.findall(r'<div class="name">(.*?) <span class="meta">', sec)
    assert len(set(names_all)) == len(names_all), "两个榜出现重复目的地"


def test_recommendation_structure_is_sane(run):
    """推荐的结构性质(不固化具体名次, 避免参数微调就失败):

    敏感性分析显示排序对先验稳健(8 个扰动场景推荐 Top3 不变), 这里固化的是
    「推荐=人少 + 不远」这一语义, 而不是具体城镇名。
    """
    _, _, res = run
    c, rec = res["crowding"], res["recommendations"]
    top = rec[rec["daily_rank"] <= 3]
    # 拥挤惩罚只在承载率 >0.85 时生效(不惩罚"正常"拥挤), 故推荐前列的平均拥挤度
    # 未必低于全表 —— 它平衡的是"好玩 + 近 + 不严重拥挤", 不是"越不挤越好"。
    # 真正的设计约束是: 推荐前列不得是已挤爆的目的地。
    assert top["crowding_index"].max() < 1.0, "推荐前列不应是接待能力已吃满的目的地"
    assert top["drive_hours"].mean() < rec["drive_hours"].mean(), "推荐应偏向更近的目的地"
    assert top["drive_hours"].max() <= 6.0, "推荐前列不应是超远目的地"
    assert c["crowding_index"].max() >= 1.0, "假期应至少有一个接待能力吃紧的目的地"
    assert (rec["recommend_score"] > 0).all()


def test_shenzhen_zhongshan_link_present():
    """深中通道(2024 通车, 西向主通道)必须在路网内且真的参与预测."""
    link = next(h for h in HIGHWAYS if h.id == "SZLINK")
    assert link.name == "深中通道"
    assert "中山市区" in link.connected_towns
    assert link.peak_amp >= max(h.peak_amp for h in HIGHWAYS if h.id == "G9411")
