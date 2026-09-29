"""端到端预测流水线.

  load_inputs  实时数据(百度迁徙 / Open-Meteo 天气 / 高德路况) -> MigrationProfile 等
  train        TTI 分位数模型(仿真训练, 日间形态由真实迁徙剖面驱动)
  forecast     纯推理: 迁徙存量 -> 目的地拥挤度; TTI; 推荐 (实时引擎每轮复用)
  evaluate     真实数据回测(迁徙规模留一年验证 / 城市结构持续性对照)
  save         原子写入全部产出物
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .clock import now as local_now
from .clock import stamp, tz_of
from .config import holiday_dates, load_config
from .data.hub import CLIMATE, DataHub
from .data.synthetic import SyntheticTrafficSource, SyntheticWeatherSource
from .evaluate import interval_coverage, per_highway_metrics, regression_metrics
from .features import build_features, congestion_level, crowding_level
from .geo import CITY_POP, HIGHWAYS, TOURIST_SHARE, TOWNS, baidu_city
from .holiday_calendar import holiday_day_type
from .migration import (CityFlow, MigrationProfile, closed_stock, backtest, build_city_flow,
                        build_profile, city_backtest, share_backtest)
from .models.gbdt import TTIPredictor
from .models.radiation import RadiationModel
from .ota import (CAPACITY_ELASTICITY, PARTY_PER_ROOM, OTASignalSource, hotel_capacity,
                  ota_multiplier)
from .recommend import recommend
from .social import SocialDynamicsModel

HW_NAME = {h.id: h.name for h in HIGHWAYS}
HW_DIR = {h.id: h.direction for h in HIGHWAYS}
LIVE_CLAMP = (0.80, 1.25)   # 实时路况校准限幅
LIVE_DECAY = 0.85           # 校准偏差逐小时衰减
CALIB_HOURS = (6, 22)       # 可校准时段(凌晨车流过小, 观测噪声会污染全天)
REST_ATTRACT = 0.6          # 城市中未收录城区的相对旅游吸引力
REST_ROOMS_PER_CAPITA = 0.015  # 未收录城区客房密度(万间/万人), 取收录城镇中位数量级
# 12306 售罄率 -> 当年客流热度修正: 相对均值每 ±10pp, 客流 ∓6%; 限幅避免单一方向过度放大
RAIL_SENSITIVITY = 0.6
RAIL_CLAMP = (0.85, 1.15)


def atomic_csv(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def atomic_json(obj, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


class ForecastPipeline:
    def __init__(self, cfg: dict | None = None, hub: DataHub | None = None):
        self.cfg = cfg or load_config()
        self.hub = hub or DataHub(self.cfg)
        self.dates = holiday_dates(self.cfg)
        self.n = len(self.dates)
        self.tz = tz_of(self.cfg)
        self.ppi = float(self.cfg["migration"]["people_per_index"])
        self.seed = int(self.cfg["data"]["random_seed"])
        self.artifacts = Path(self.cfg["output"]["artifacts_dir"])
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.ota_signals = OTASignalSource(self.cfg).fetch_town_signals()

    # ================= 输入 =================
    def load_inputs(self, force: bool = False) -> dict:
        mig = self.hub.migration_inputs(force)
        profile = build_profile(mig["out"], mig["in"], mig["rank"], self.dates[0], self.n,
                                source=self.hub.provenance["百度慧眼迁徙"]["status"])
        weather = self.hub.weather(self.dates, force)
        live_tti = self.hub.live_traffic(force)
        tencent_drive = self.hub.tencent_drive(force)
        railway = self.hub.railway_demand(self.dates, force)
        lodging = self.hub.lodging_supply()
        baidu_lodging = self.hub.baidu_lodging_supply()
        partner = self.hub.partner_observations()
        self.hub.register_static_sources()
        return {"migration": mig, "profile": profile, "weather": weather,
                "live_tti": live_tti, "tencent_drive": tencent_drive, "railway": railway,
                "lodging_supply": lodging, "baidu_lodging_supply": baidu_lodging,
                "partner_observations": partner}

    def corridor_heat(self, rng: np.random.Generator | None = None) -> dict[str, float]:
        """走廊 OTA 需求热度 = 连接目的地预订热度均值."""
        heat = dict(zip(self.ota_signals["town"], self.ota_signals["booking_heat"]))
        out = {}
        for hw in HIGHWAYS:
            v = float(np.mean([heat[t] for t in hw.connected_towns]))
            if rng is not None:
                v = float(np.clip(v * rng.normal(1.0, 0.07), 0.05, 0.97))
            out[hw.id] = round(v, 4)
        return out

    # ================= 训练 =================
    def build_history(self, profile: MigrationProfile) -> pd.DataFrame:
        base_flows = profile.day_flows(self.ppi)[1:]
        y0 = int(self.cfg["data"]["history_start_year"])
        frames = []
        for year in range(y0, y0 + 5):
            rng = np.random.default_rng(self.seed + year)
            flows = [(o * rng.normal(1, 0.1), i * rng.normal(1, 0.1)) for o, i in base_flows]
            weather = SyntheticWeatherSource(self.seed + year + 1).generate(self.n)
            traffic = SyntheticTrafficSource(self.seed + year + 2, self.corridor_heat(rng)) \
                .generate(year, flows, weather)
            meta = pd.DataFrame({
                "date_seq": range(self.n), "holiday_offset": range(1, self.n + 1),
                "day_type": [holiday_day_type(k, self.n) for k in range(1, self.n + 1)],
                "flow_out": [f[0] for f in flows], "flow_in": [f[1] for f in flows],
                "rain_level": weather["rain_level"], "temp_dev": weather["temp_dev"],
            })
            frames.append(traffic.merge(meta, on="date_seq"))
        return pd.concat(frames, ignore_index=True)

    def train(self, history: pd.DataFrame) -> tuple[TTIPredictor, dict]:
        """三段式评估: 训练(前 n−2 年) -> 保形区间校准(倒数第 2 年) -> 检验(最后一年).

        区间必须用独立的一年校准: 预测目标年存在训练期看不到的年际波动, 直接用
        分位数回归输出的 P10/P90 会系统性偏窄(实测覆盖仅 ~60%)。
        """
        years = sorted(history["year"].unique())
        if len(years) < 3:
            raise ValueError(f"训练样本至少需要 3 年, 当前 {len(years)} 年")
        tr_y, cal_y, te_y = years[:-2], years[-2], years[-1]
        tr = build_features(history[history["year"].isin(tr_y)])
        cal = build_features(history[history["year"] == cal_y])
        te = build_features(history[history["year"] == te_y])
        model = TTIPredictor(self.cfg["models"]["lightgbm"]).fit(tr).calibrate(cal)
        pred = model.predict(te)
        p10, p90 = model.predict_interval(te)
        te = te.assign(tti_pred=pred, highway_name=te["highway_id"].map(HW_NAME))
        m = regression_metrics(te["tti"], pred)
        m["interval_coverage_80%"] = interval_coverage(te["tti"], p10, p90)
        m["per_highway_MAPE_%"] = per_highway_metrics(te)
        m["train_years"] = [int(y) for y in tr_y]
        m["calibration_year"] = int(cal_y)
        m["test_year"] = int(te_y)
        m["interval_shift"] = list(model.interval_shift)
        m["note"] = ("仿真数据自洽性检验(留出最后一年) + 保形区间校准(留出倒数第二年); "
                     "仅说明模型学会了设定的规律, 不代表真实路况精度")
        return model, m

    # ================= 推理: 迁徙存量与拥挤度 =================
    def away_stock(self, profile: MigrationProfile) -> tuple[list, list, list]:
        """深圳在外假期出行者存量. 守恒: 假期累计超额迁出 = 累计返程(按迁入剖面分布).
        返回 D0..Dn 的 (超额迁出, 返程量, 在外存量)."""
        flows = profile.day_flows(self.ppi)
        outs = [o for o, _ in flows] + [0.0]
        raw_ret = [0.0] + [i for _, i in flows[1:]] + [profile.post_inflow(self.ppi)]
        stock = closed_stock(outs, raw_ret)
        rets = [max(0.0, (stock[k - 1] if k else 0.0) + outs[k] - stock[k])
                for k in range(len(outs))]
        return outs[:-1], rets[:-1], stock[:-1]

    def city_flows(self, migration: dict) -> dict[str, CityFlow]:
        return {city: build_city_flow(city, c["in"], c["out"], self.dates[0], self.n)
                for city, c in migration["dest"].items()}

    def _railway_adj(self, railway: dict) -> dict[str, float]:
        """12306 假期首日售罄率 -> 城市当年热度修正(nowcast).
        售罄率相对均值每 ±10pp, 客流 ±6%(限幅 0.85-1.15)."""
        rates = {c: v["rate"] for c, v in railway.get("city", {}).items()}
        if not rates:
            return {}
        mean = float(np.mean(list(rates.values())))
        return {c: round(float(np.clip(1 + RAIL_SENSITIVITY * (r - mean), *RAIL_CLAMP)), 3)
                for c, r in rates.items()}

    def predict_crowding(self, city_flows: dict[str, CityFlow],
                         railway: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
        """目的地拥挤度 = 在地游客 / 接待能力(游客承载率).

        在地外来人口(城市级) 来自目的地全来源迁入/迁出真实曲线的双存量分解, 再拆分:
          游客 = τ_c × 外来人口 × 铁路当年热度修正, 城内分配 ∝ 接待能力 × 吸引力 × 社媒^0.6 × OTA^0.8
                (未收录城区按 0.6 吸引力计)
          返乡探亲 = (1−τ_c) × 外来人口, 按人口分配(住亲友家, 不占用接待能力)
        外出本地人按人口分摊; 接待能力 = 客房 × 2.8 人/间 × 1.6 弹性.
        """
        rail_adj = self._railway_adj(railway or {})
        social = SocialDynamicsModel(self.cfg)
        sig = self.ota_signals.set_index("town")
        by_city: dict[str, list] = {}
        for t in TOWNS:
            by_city.setdefault(t.city, []).append(t)
        heat = {t.name: social.seed(t.name) for t in TOWNS}
        crowd_rows, heat_rows = [], []
        for k, date in enumerate(self.dates, start=1):
            dtype = holiday_day_type(k, self.n)
            mods = {t.name: social.multiplier(heat[t.name]) ** 0.6
                    * ota_multiplier(sig.at[t.name, "booking_heat"], day_type=dtype) ** 0.8
                    for t in TOWNS}
            util = {}
            for city, towns in by_city.items():
                flow = city_flows[city]
                attract = {t.name: hotel_capacity(t.name) * t.tourism * mods[t.name]
                           for t in towns}
                rest_pop = max(0.0, CITY_POP[city] - sum(t.population for t in towns))
                rest = rest_pop * REST_ROOMS_PER_CAPITA * PARTY_PER_ROOM \
                    * CAPACITY_ELASTICITY * REST_ATTRACT
                denom = sum(attract.values()) + rest
                inflow = flow.visitors[k] * self.ppi * rail_adj.get(city, 1.0)
                tau = TOURIST_SHARE[city]
                for t in towns:
                    visitors = tau * inflow * attract[t.name] / denom
                    family = (1 - tau) * inflow * t.population / CITY_POP[city]
                    away = flow.locals_away[k] * self.ppi * t.population / CITY_POP[city]
                    cap = hotel_capacity(t.name)
                    idx = visitors / cap
                    util[t.name] = idx
                    crowd_rows.append({
                        "date": date, "day_type": dtype, "town": t.name, "city": city,
                        "resident_万人": t.population, "visitors_万人": round(visitors, 2),
                        "family_visitors_万人": round(family, 2),
                        "locals_away_万人": round(away, 2),
                        "population_万人": round(t.population - away + visitors + family, 2),
                        "capacity_万人": round(cap, 2),
                        "hotel_overflow_万人": round(max(0.0, visitors - cap), 2),
                        "booking_heat": sig.at[t.name, "booking_heat"],
                        "price_premium": sig.at[t.name, "price_premium"],
                        "sell_out": sig.at[t.name, "sell_out"],
                        "crowding_index": round(idx, 3), "crowding_level": crowding_level(idx),
                        "observed": date in flow.observed_days,
                    })
            for t in TOWNS:
                heat[t.name] = social.step(heat[t.name], util[t.name])
                heat_rows.append({"date": date, "town": t.name,
                                  "social_heat": round(heat[t.name], 3),
                                  "modulation": round(mods[t.name], 3)})
        return pd.DataFrame(crowd_rows), pd.DataFrame(heat_rows)

    def migration_table(self, profile: MigrationProfile) -> pd.DataFrame:
        outs, rets, stock = self.away_stock(profile)
        d0 = dt.date.fromisoformat(self.dates[0]) - dt.timedelta(1)
        oi, ii = profile.out_index(), profile.in_index()
        rows = []
        for k in range(self.n + 2):
            date = (d0 + dt.timedelta(k)).isoformat()
            has_out = k <= self.n
            rows.append({
                "date": date, "day_type": holiday_day_type(k, self.n),
                "out_index": round(oi[k], 2) if has_out else None,
                "out_low": round(profile.out_low[k] * profile.baseline_out, 2) if has_out else None,
                "out_high": round(profile.out_high[k] * profile.baseline_out, 2) if has_out else None,
                "in_index": round(ii[k], 2),
                "in_low": round(profile.in_low[k] * profile.baseline_in, 2),
                "in_high": round(profile.in_high[k] * profile.baseline_in, 2),
                "observed_out": profile.observed_out.get(date),
                "observed_in": profile.observed_in.get(date),
                "excess_out_万人次": round(outs[k], 1) if has_out else None,
                "returns_万人次": round(rets[k], 1) if has_out else None,
                "away_stock_万人": round(stock[k], 1) if has_out else None,
            })
        return pd.DataFrame(rows)

    # ================= 推理: 拥堵 =================
    def _corridor_weather(self, weather: pd.DataFrame) -> dict[tuple[str, str], tuple[float, float]]:
        """(highway_id, date) -> (降水输入, 气温偏差): 取深圳 + 连接目的地均值."""
        w = weather.set_index(["place", "date"])
        out = {}
        for hw in HIGHWAYS:
            places = ["深圳", *hw.connected_towns]
            for d in self.dates:
                sub = w.loc[[(p, d) for p in places]]
                temp_dev = (sub["tmax"] - CLIMATE["tmax"]).where(sub["source"] == "forecast", 0.0)
                out[(hw.id, d)] = (float(sub["rain_input"].mean()), float(temp_dev.mean()))
        return out

    def predict_traffic(self, model: TTIPredictor, profile: MigrationProfile,
                        weather: pd.DataFrame, live_tti: dict[str, float] | None = None,
                        now: dt.datetime | None = None) -> tuple[pd.DataFrame, dict]:
        flows = profile.day_flows(self.ppi)[1:]
        hol = pd.DataFrame({
            "date": self.dates, "date_seq": range(self.n), "holiday_offset": range(1, self.n + 1),
            "day_type": [holiday_day_type(k, self.n) for k in range(1, self.n + 1)]})
        grid = pd.MultiIndex.from_product(
            [range(self.n), [h.id for h in HIGHWAYS], range(24)],
            names=["date_seq", "highway_id", "hour"]).to_frame(index=False).merge(hol, on="date_seq")
        cw = self._corridor_weather(weather)
        grid["rain_level"] = [cw[(h, d)][0] for h, d in zip(grid["highway_id"], grid["date"])]
        grid["temp_dev"] = [cw[(h, d)][1] for h, d in zip(grid["highway_id"], grid["date"])]
        grid["corridor_ota_heat"] = grid["highway_id"].map(self.corridor_heat())
        feats = build_features(grid, dict(enumerate(flows)))
        feats["tti_pred"] = model.predict(feats)
        feats["tti_p10"], feats["tti_p90"] = model.predict_interval(feats)
        calib = self._live_calibrate(feats, live_tti or {}, now or local_now(self.tz))
        for c in ("tti_pred", "tti_p10", "tti_p90"):
            feats[c] = feats[c].round(3)
        feats["highway_name"] = feats["highway_id"].map(HW_NAME)
        feats["direction"] = feats["highway_id"].map(HW_DIR)
        feats["congestion_level"] = feats["tti_pred"].map(congestion_level)
        return feats, calib

    def _live_calibrate(self, feats: pd.DataFrame, live: dict[str, float],
                        now: dt.datetime) -> dict:
        """仅在预测窗口内生效: 用当前实测 TTI 校准当日剩余小时(有界、逐小时衰减)."""
        today = now.date().isoformat()
        if not live or today not in self.dates:
            return {"applied": False,
                    "reason": "无实时路况" if not live else "当前不在假期预测窗口内"}
        if not CALIB_HOURS[0] <= now.hour <= CALIB_HOURS[1]:
            # 凌晨车流极小、车速噪声大, 拿此时的观测外推全天会把白天峰值系统性压低
            return {"applied": False, "reason": f"当前 {now.hour} 时不在可校准时段"
                                                f"{CALIB_HOURS[0]}-{CALIB_HOURS[1]} 点"}
        ratios = {}
        for hid, obs in live.items():
            cur = (feats["date"] == today) & (feats["highway_id"] == hid) & (feats["hour"] == now.hour)
            if not cur.any():
                continue
            ratio = float(np.clip(obs / feats.loc[cur, "tti_pred"].iloc[0], *LIVE_CLAMP))
            ratios[hid] = round(ratio, 3)
            rest = (feats["date"] == today) & (feats["highway_id"] == hid) & (feats["hour"] >= now.hour)
            factor = 1 + (ratio - 1) * LIVE_DECAY ** (feats.loc[rest, "hour"] - now.hour)
            for c in ("tti_pred", "tti_p10", "tti_p90"):
                feats.loc[rest, c] = np.maximum(1.0, feats.loc[rest, c] * factor)
        return {"applied": bool(ratios), "at": now.strftime("%Y-%m-%d %H:%M"), "ratios": ratios}

    # ================= 编排 =================
    def forecast(self, model: TTIPredictor, inputs: dict, now: dt.datetime | None = None) -> dict:
        profile = inputs["profile"]
        crowding, social_heat = self.predict_crowding(
            self.city_flows(inputs["migration"]), inputs.get("railway"))
        traffic, calib = self.predict_traffic(model, profile, inputs["weather"],
                                              inputs["live_tti"], now)
        return {
            "profile": profile, "migration": self.migration_table(profile),
            "crowding": crowding, "traffic": traffic, "social_heat": social_heat,
            "recommendations": recommend(crowding, traffic, inputs["weather"]),
            "weather": inputs["weather"], "ota_signals": self.ota_signals,
            "tencent_drive": inputs["tencent_drive"],
            "lodging_supply": inputs["lodging_supply"],
            "baidu_lodging_supply": inputs["baidu_lodging_supply"],
            "partner_observations": inputs["partner_observations"],
            "railway": inputs.get("railway", {}),
            "live_calibration": calib, "feature_importance": model.feature_importance(),
        }

    def evaluate(self, inputs: dict) -> dict:
        mig = inputs["migration"]
        cities = sorted({baidu_city(t) for t in TOWNS})
        prior = RadiationModel(self.cfg).weights()
        prior_city = {}
        for t in TOWNS:
            prior_city[baidu_city(t)] = prior_city.get(baidu_city(t), 0) + prior[t.name] * 100
        return {"migration_backtest": backtest(mig["out"], mig["in"], self.n),
                "destination_backtest": city_backtest(mig["dest"], self.n),
                "share_backtest": share_backtest(mig["rank"], cities, prior_city)}

    def run(self, force: bool = False) -> dict:
        inputs = self.load_inputs(force)
        model, tti_check = self.train(self.build_history(inputs["profile"]))
        model.save(self.artifacts)
        result = self.forecast(model, inputs)
        result["metrics"] = {**self.evaluate(inputs), "tti_simulated_check": tti_check}
        self.save(result)
        return result

    def save(self, result: dict) -> None:
        art = self.artifacts
        atomic_csv(result["crowding"], art / "crowding_predictions.csv")
        atomic_csv(result["traffic"][[
            "date", "day_type", "highway_id", "highway_name", "direction", "hour",
            "rain_level", "tti_pred", "tti_p10", "tti_p90", "congestion_level"]],
            art / "traffic_predictions.csv")
        atomic_csv(result["recommendations"], art / "recommendations.csv")
        atomic_csv(result["social_heat"], art / "social_heat.csv")
        atomic_csv(result["ota_signals"], art / "ota_signals.csv")
        atomic_csv(result["tencent_drive"], art / "tencent_drive_eta.csv")
        atomic_csv(result["lodging_supply"], art / "lodging_supply.csv")
        atomic_csv(result["baidu_lodging_supply"], art / "baidu_lodging_supply.csv")
        atomic_csv(result["partner_observations"], art / "partner_observations.csv")
        atomic_csv(result["weather"], art / "weather.csv")
        atomic_csv(result["migration"], art / "migration_forecast.csv")
        if result.get("railway"):
            atomic_json(result["railway"], art / "railway_demand.json")
        metrics = dict(result.get("metrics") or {})
        prof: MigrationProfile = result["profile"]
        metrics.update({
            "generated_at": stamp(self.tz),
            "timezone": self.tz,
            "data_mode": self.hub.mode, "provenance": self.hub.provenance,
            "live_calibration": result["live_calibration"],
            "migration_profile": {"baseline_out": prof.baseline_out,
                                  "baseline_in": prof.baseline_in,
                                  "ref_years": prof.ref_years,
                                  "observed_days": sorted(prof.observed_out),
                                  "people_per_index": self.ppi,
                                  "shenzhen_top_cities": dict(sorted(
                                      prof.city_shares.items(), key=lambda kv: -kv[1])[:12])},
        })
        result["metrics"] = metrics
        atomic_json(metrics, art / "metrics.json")
