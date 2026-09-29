"""实时引擎: 每轮 拉最新数据 -> 用已训练模型重新推理 -> 原子写产出物 + 重建报告.

与训练解耦: 模型只在 scripts/run_pipeline.py 训练一次; 引擎每轮只做推理(秒级),
新数据来源于 DataHub(TTL 缓存, 迁徙 6h / 天气 3h / 路况 10min).
"""
from __future__ import annotations

import json
import threading
import time

from .clock import now as local_now
from .clock import stamp, tz_of
from .config import load_config
from .models.gbdt import TTIPredictor
from .pipeline import ForecastPipeline, atomic_json
from .report import build_report


class RealtimeEngine:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.pipe = ForecastPipeline(self.cfg)
        self.tz = tz_of(self.cfg)
        self._model: TTIPredictor | None = None
        self._lock = threading.Lock()
        self.tick = 0

    @property
    def model(self) -> TTIPredictor:
        if self._model is None:
            self._model = TTIPredictor.load(self.pipe.artifacts)
        return self._model

    def refresh(self, force: bool = False) -> dict:
        """一轮刷新; 并发调用串行化."""
        with self._lock:
            t0 = time.time()
            inputs = self.pipe.load_inputs(force)
            result = self.pipe.forecast(self.model, inputs, now=local_now(self.tz))
            result["metrics"] = self.pipe.evaluate(inputs)
            # 训练期指标只在训练时产生, 推理轮次沿用上次结果
            prev = self.pipe.artifacts / "metrics.json"
            if prev.exists():
                old = json.loads(prev.read_text(encoding="utf-8"))
                if "tti_simulated_check" in old:
                    result["metrics"]["tti_simulated_check"] = old["tti_simulated_check"]
            self.pipe.save(result)
            build_report(result, self.cfg["output"]["report_html"])
            self.tick += 1
            status = {
                "tick": self.tick, "refreshed_at": stamp(self.tz),
                "elapsed_s": round(time.time() - t0, 2),
                "sources": {k: v["status"] for k, v in self.pipe.hub.provenance.items()},
                "live_calibration": result["live_calibration"],
                "observed_days": result["metrics"]["migration_profile"]["observed_days"],
            }
            atomic_json(status, self.pipe.artifacts / "engine_status.json")
            return status
