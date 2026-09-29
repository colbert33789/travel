# 深圳国庆出行预测

预测 2026 国庆（10/1–10/7）深圳居民出行节奏、周边 **41 个目的地**拥挤度、**20 条出城高速**逐小时拥堵，并给出每日目的地推荐。输出移动端优先的 HTML 报告与 REST API。

## 实时数据

| 数据 | 接口 | Key | 刷新 |
|---|---|---|---|
| 深圳/20 个目的地城市迁徙规模、深圳去向占比 | 百度慧眼 `huiyan.baidu.com/migration` | 免 | 6h 缓存，T+1 更新 |
| 42 个地点逐日天气预报 | Open-Meteo | 免 | 3h 缓存 |
| 各方向火车票售罄率（真实订票需求） | 12306 `kyfw.12306.cn/otn` | 免（会话 Cookie） | 2h 缓存，逐日 7 天 × 21 方向 |
| 20 条高速实时车速 | 高德道路路况 | `AMAP_KEY` | 10min 缓存，可选 |
| 深圳→41 目的地当前驾车预计耗时（独立观测） | 腾讯驾车路线规划 | `TENCENT_MAP_KEY` | 1h 缓存；日额度耗尽后本日停止请求 |
| 41 个目的地中心周边 5km 住宿地点检索数 | 高德 POI 周边搜索 | `AMAP_KEY` | 24h 缓存；不受强制刷新影响 |
| 41 个目的地中心周边 5km 酒店/民宿/宾馆关键词检索数 | 百度地图 Place 2.0 | `BAIDU_MAP_AK` | 24h 缓存；不受强制刷新影响 |
| 深圳→各目的地自驾里程与平日耗时 | 高德驾车路径规划 | `AMAP_KEY` | 实测一次，已固化（40/40） |

各数据源的注册/申请门槛已逐一实测，见 [`docs/API_ACCESS.md`](docs/API_ACCESS.md)。
高德道路路况实测仅 **7/20** 条走廊在道路名索引内（需带编号的路名，如 `G4京港澳高速`），
未收录的走廊保持模型预测，报告中如实标注覆盖率。
自驾里程用实测路网而非球面距离估算——珠江西岸需绕珠江口，球面×1.3 会低估珠海 34km、
万山 51km，直接导致推荐分虚高（见 `docs/METHODOLOGY.md` §2.6）。腾讯路线先将项目 WGS84 地点坐标批量转换为 GCJ-02，再查询当前 ETA，单独保存为 `tencent_drive_eta.csv`；不把整段驾车耗时当作某条高速 TTI，也不用于预测未来假期。2026-09-29 该 Key 返回 `status=121`，当天无 41 地点完整数据，等待额度恢复自动尝试；`--force` 可在额度提升后手动重试。

联网失败依次退回：过期缓存 → 仓库内置真实快照（`reference/baidu_snapshot.json`）/ 气候均值。每个来源的实际状态与时间写入 `metrics.json` 的 `provenance` 并展示在报告末尾。

社媒（小红书/抖音/微信）和 OTA（携程/去哪儿/美团/飞猪）没有当前可直接使用的免授权全站数据接口。`social_heat.csv` 与 `ota_signals.csv` 仍是先验/模拟，**不代表实时帖子热度、酒店房价、库存或预订量**。高德住宿 POI 是地图地点供给代理（统一近 5km，可能重复/遗漏），单独存入 `lodging_supply.csv`；实测 18/41 地点返回恰好 600，疑似检索上限，`possibly_capped=true` 时数量仅作下界。百度住宿关键词数据单独存入 `baidu_lodging_supply.csv`，输入坐标采用 WGS84；其目录覆盖、检索排序与高德不同，**两者计数不能相加或按比例互换**。百度 Place `total` 最大返回 150 且严格半径检索可能不精确，达到 150 时标记 `possibly_capped=true`。两种数据均不替换客房估计或改变预测。

取得平台合法授权及导出文件后，可在 `data/partner_observations.csv` 提供 `town,platform,metric,value,observed_at` 五列；`town` 使用项目现有目的地名。`platform` 支持 `ctrip`、`qunar`、`meituan`、`fliggy`、`xiaohongshu`、`douyin`、`weibo`、`wechat`。OTA 的 `metric` 可为 `booking_heat`（归一化 0–1）、`price_premium`（1–10）、`sell_out`（0–1）、`hotel_price_cny` 或 `available_rooms`；社交平台可为 `social_heat`（归一化 0–1）、`post_count` 或 `interaction_count`。`observed_at` 用 ISO 日期或时间。导入值仅经格式校验，平台来源/授权需自行确认；作为独立观测写入 `artifacts/partner_observations.csv`，**不会自动修改模型的先验或推荐**，也不会经公开 API 暴露。不提供文件时返回空观测；原始导入文件已在 `.gitignore` 忽略。若有商业保密要求，须自行管理 `artifacts/` 的文件权限。

## 准确度（真实数据留一年回测）

| 对象 | 结果 |
|---|---|
| 深圳逐日迁出 / 迁入 | MAPE 17.1% / 12.8%，高峰日命中 100% |
| 21 个目的地逐日迁入 | MAPE 中位 29.3% |
| 深圳去向城市占比 | MAE 0.51 个百分点 |
| 高速拥堵 | 训练样本为仿真（高德不开放历史数据），仅供相对强弱参考；P10/P90 经保形校准，80% 区间实测覆盖 80% |

方法、假设与局限详见 [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md)。

## 快速开始

```bash
python3.9 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/run_pipeline.py            # 联网拉取 → 训练 → 预测 → 报告
.venv/bin/python scripts/run_pipeline.py --offline  # 仅用内置快照，可复现
.venv/bin/python -m pytest tests/ -q                 # 离线测试

cp .env.example .env                                # 可选：填入高德、百度及腾讯密钥
# AMAP_KEY: 高德路况/住宿；BAIDU_MAP_AK: 百度住宿；TENCENT_MAP_KEY: 腾讯当前路线 ETA
# 优先级：环境变量 > .env > config.yaml；.env 已被 .gitignore 忽略，密钥不入库
# chmod 600 .env 可防止本机其他用户读取密钥
```

## 实时引擎

模型只训练一次；引擎每轮拉最新数据 → 重新推理 → 原子写产出物 → 重建报告。
缓存命中约 **1 秒**；缓存过期需联网时约 **1–2 分钟**（12306 逐方向查询最耗时，已按 4 路并发 + 失败串行重试），故刷新间隔建议 ≥30 分钟。

```bash
.venv/bin/python scripts/refresh_live.py             # 单轮
.venv/bin/python scripts/refresh_live.py --loop 20   # 常驻，每 20 分钟
# 或 config.yaml 设 engine.enabled: true，由 API 进程内置定时刷新
```

假期开始后，已公布的百度迁徙日值会替换预测并修正后续日；配置高德 Key 时，当天剩余时段的拥堵按实测车速校准（限幅 0.8–1.25）。

## API

```bash
.venv/bin/uvicorn holiday_traffic.api:app --port 8300 --app-dir src
```

| 端点 | 说明 |
|---|---|
| `GET /report` | HTML 报告 |
| `GET /predictions/summary` | 出城/返程高峰日、最堵路段、最挤目的地 |
| `GET /predictions/migration` | 逐日迁出/迁入指数、区间、深圳在外人数、实测值 |
| `GET /predictions/weather?place=` | 逐日天气（深圳或目的地名） |
| `GET /predictions/crowding?date=` | 目的地游客数、接待能力、拥挤度 |
| `GET /predictions/traffic?date=&highway_id=&peak_only=` | 逐小时 TTI 及 P10/P90 |
| `GET /predictions/recommendations?date=&top_n=` | 每日推荐 |
| `GET /predictions/social-heat?town=` · `GET /predictions/ota?metric=` | 先验/模拟信号 |
| `GET /observations/lodging-pois?town=` | 高德周边住宿地点数，非 OTA 交易数据 |
| `GET /observations/baidu-lodging-pois?town=` | 百度住宿关键词检索数，非 OTA 或社交热度 |
| `GET /observations/tencent-drive?town=` | 腾讯当前驾车路线 ETA，非假期预测或指定高速车速 |
| `GET /metrics` | 回测、数据来源 |
| `GET /engine/status` · `POST /engine/refresh?force=` | 引擎状态 / 手动刷新 |

## 目录

```
config/config.yaml
src/holiday_traffic/
  data/        http · baidu · openmeteo · railway(12306) · amap · hub(缓存/降级/溯源)
               · synthetic(TTI 仿真)
  migration.py 出行节奏、目的地客流、守恒存量、真实数据回测
  pipeline.py  训练 / 推理 / 评估 / 落盘
  engine.py    实时引擎
  models/      gbdt(分位数 LightGBM + 保形区间校准) · radiation(回测对照)
  recommend.py · social.py · ota.py · features.py · geo.py · report.py · api.py
  reference/baidu_snapshot.json   真实数据快照
  reference/hourly_shape.json     实测逐时车速形态
  templates/report.css
scripts/  run_pipeline.py · refresh_live.py · update_reference.py
```
