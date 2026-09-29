# 深圳国庆出行预测

[![在线报告](https://img.shields.io/badge/在线报告-GitHub%20Pages-ff2442)](https://colbert33789.github.io/travel/)
[![Python](https://img.shields.io/badge/Python-3.9+-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![测试](https://img.shields.io/badge/测试-35%20passed-1aa260)](tests/)

预测 2026 国庆（10/1–10/7）深圳**什么时候出城最堵、哪条高速最难走、哪个景点人最多、去哪儿最值得**，
覆盖 **41 个周边目的地 × 20 条出城高速 × 逐小时**。

> **📊 [在线报告 · 点击直接看](https://colbert33789.github.io/travel/)** —— 手机端适配，断网也能看（单文件自包含，内联 plotly.js）

## 它回答什么

| 问题 | 报告里看 |
|---|---|
| 哪天出城 / 返程最挤？ | 出行节奏图 + 错峰时刻表（精确到「8 点前出发」） |
| 几点上路最省时间？ | 20 条高速逐小时拥堵热力图 |
| 哪个景点人最多？ | 41 个目的地拥挤度排名（游客数 / 接待能力） |
| 去哪儿最值得？ | 每日推荐榜（游玩性 − 拥挤 − 车程 − 房价 − 雨天） |
| 天气怎么安排？ | 42 个地点逐日降水，自动建议室内/户外 |
| 火车票哪个方向紧张？ | 12306 售罄率（20 个方向 × 7 天） |

## 快速开始

**零配置即可运行**——迁徙、天气、12306 三个核心数据源都不需要 Key：

```bash
python3.9 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/run_pipeline.py --offline   # 内置快照，约 6 秒出报告，可复现
.venv/bin/python scripts/run_pipeline.py             # 联网拉最新数据
.venv/bin/python -m pytest tests/ -q                 # 35 项离线测试
```

报告写到 `artifacts/report.html`。想发布到在线地址：`./scripts/publish_report.sh`。

<details><summary>可选：配置密钥解锁更多数据源</summary>

```bash
cp .env.example .env && chmod 600 .env
# AMAP_KEY        高德：实时路况(20/20 走廊) + 住宿 POI
# TENCENT_MAP_KEY 腾讯：深圳→41 目的地当前驾车 ETA
# BAIDU_MAP_AK    百度：住宿关键词 POI 检索数
```

优先级：环境变量 > `.env` > `config.yaml`。`.env` 已被 `.gitignore` 忽略，密钥不会入库。

</details>

## 数据源

| 数据 | 来源 | 需要 Key |
|---|---|---|
| 深圳 / 20 城逐日迁徙规模、深圳去向占比 | 百度慧眼迁徙 | 否 |
| 42 个地点逐日天气预报 | Open-Meteo | 否 |
| 20 个方向 × 7 天火车票售罄率 | 12306 余票 | 否（会话 Cookie） |
| 20 条高速实时车速 | 高德驾车路径规划 | `AMAP_KEY` |
| 41 个目的地住宿供给代理 | 高德 / 百度 POI | `AMAP_KEY` / `BAIDU_MAP_AK` |
| 深圳→各目的地当前驾车 ETA | 腾讯路线规划 | `TENCENT_MAP_KEY` |

**降级链**：联网失败 → 过期缓存 → 内置真实快照（`reference/baidu_snapshot.json`）→ 气候均值。
每个来源的真实状态与更新时间写入 `metrics.json` 的 `provenance`，并展示在报告末尾——**用了真实数据还是兜底值，报告里写明，不蒙混**。

各数据源的注册门槛与实测坑（如高德路名必须带编号 `G4京港澳高速`）见 [`docs/API_ACCESS.md`](docs/API_ACCESS.md)。

<details><summary>关于社媒 / OTA 信号（重要）</summary>

小红书、抖音、携程、美团等**没有免授权的全站数据接口**。因此 `social_heat.csv` 与 `ota_signals.csv` 是**先验与模拟**，
不代表实时帖子热度、房价、库存或预订量。

住宿 POI 只是「地图上的地点供给」代理（统一近 5km，可能重复/遗漏），不是客房数或订单：
高德 18/41 地点返回恰好 600（疑似接口上限），此时仅作下界；百度上限 150。两者口径不同，**计数不可相加或按比例换算**。

若你持有平台授权数据，可在 `data/partner_observations.csv` 按 `town,platform,metric,value,observed_at` 五列导入
（平台：`ctrip`/`qunar`/`meituan`/`fliggy`/`xiaohongshu`/`douyin`/`weibo`/`wechat`）。
导入值作为**独立观测**落盘，**不会自动修改模型先验**，也不经公开 API 暴露。

</details>

## 准确度（真实数据留一年回测）

| 对象 | 方法 | 结果 |
|---|---|---|
| 深圳逐日迁出 / 迁入 | 2024↔2025 互测 | **MAPE 17.1% / 12.8%，高峰日命中 100%** |
| 20 个目的地城市逐日迁入 | 同上，42 城-年 | MAPE 中位 **29.3%** |
| 深圳去向城市占比（21 城） | 2024 预测 2025 | MAE **0.51pp**（辐射模型先验 2.25pp） |
| 高速拥堵 TTI | 仿真自洽（训练→校准→检验） | MAPE 5.2%，80% 区间实测覆盖 **80%** |

**怎么读这些数字**：预测「哪天最挤」比「具体多少人」可靠得多——峰值日稳定命中，但逐日绝对值误差可达 30%。
拥堵模型训练于仿真数据（高德不开放历史路况），**只提供相对强弱与时段形态，不代表真实路况精度**。

方法与假设详见 [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md)。

## 运行形态

**批量**：`run_pipeline.py` 训练一次 → 预测 → 落盘 → 生成报告。

**常驻**：`refresh_live.py` 每轮拉最新数据 → 重新推理 → 原子写 → 重建报告。缓存命中约 1 秒，需联网时 1–2 分钟（12306 最耗时），建议间隔 ≥30 分钟。

```bash
.venv/bin/python scripts/refresh_live.py --loop 20
```

假期开始后，已公布的迁徙日值会替换预测并修正后续日；配置高德 Key 时，当天剩余时段按实测车速校准（限幅 0.8–1.25，仅 6–22 点）。

**API**：`uvicorn holiday_traffic.api:app --port 8300 --app-dir src`

| 端点 | 说明 |
|---|---|
| `GET /report` | HTML 报告 |
| `GET /predictions/summary` | 出城/返程高峰日、最堵路段、最挤目的地 |
| `GET /predictions/migration` · `/weather` · `/crowding` · `/traffic` · `/recommendations` | 迁徙 / 天气 / 拥挤度 / 逐小时 TTI(P10-P90) / 每日推荐 |
| `GET /observations/lodging-pois` · `/baidu-lodging-pois` · `/tencent-drive` | 住宿供给代理、当前驾车 ETA（均非假期预测） |
| `GET /metrics` · `/engine/status` · `POST /engine/refresh` | 回测与数据溯源 / 引擎状态 / 手动刷新 |

## 目录

```
config/config.yaml                 假期窗口、缓存 TTL、模型参数
src/holiday_traffic/
  data/       http · baidu(迁徙) · openmeteo(天气) · railway(12306) · amap
              · hub(缓存/降级/溯源; 含腾讯驾车 ETA) · synthetic(TTI 仿真)
  migration.py    出行节奏、目的地客流、守恒存量、真实数据回测
  pipeline.py     训练 / 推理 / 评估 / 落盘
  engine.py       实时引擎    clock.py  时区统一
  models/         gbdt(分位数 LightGBM + 保形区间校准) · radiation(回测对照)
  recommend.py · social.py · ota.py · features.py · geo.py · report.py · api.py
  reference/      baidu_snapshot.json(真实快照) · hourly_shape.json(实测逐时车速)
  templates/report.css
scripts/    run_pipeline.py · refresh_live.py · itinerary.py
            update_reference.py · publish_report.sh
```

## 边界

- 目标人群是**深圳出发的自驾/高铁出行者**；目的地为项目内置 41 个，非全量。
- 迁徙数据 T+1 更新，报告反映的是**预测时点**的已知信息。
- 社媒/OTA 为模拟值，住宿 POI 为供给代理，均非交易数据（见上方折叠说明）。

## 文档

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) —— 模型、公式、假设与局限
- [`docs/API_ACCESS.md`](docs/API_ACCESS.md) —— 各数据源申请门槛与实测结论

## 许可

代码为 **MIT** 许可。注意：第三方数据（百度慧眼/百度地图、高德、腾讯位置服务、12306、国家统计局）
归各自权利方所有，受其平台条款与配额约束，**不在本许可范围内**——详见 [`LICENSE`](LICENSE) 末尾说明。
