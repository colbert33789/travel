# 数据源 API 接入实测 (2026-09-29 实测)

每个数据源均实际探测了接口可达性与鉴权要求（HTTP 状态码/错误码为证）。

## A. 免 Key，已接入并验证（项目当前已用）

| 数据源 | 接口 | 实测 | 用途 |
|---|---|---|---|
| **百度慧眼迁徙** | `huiyan.baidu.com/migration/historycurve.jsonp` | `200 errno:0` | 深圳/20 目的地城市每日迁出迁入规模指数（2019 至今，T+1） |
| | `huiyan.baidu.com/migration/cityrank.jsonp` | `200 errno:0` | 城市间迁徙占比 Top100 |
| **Open-Meteo** | `api.open-meteo.com/v1/forecast` | `200` | 41 个地点 16 天逐日预报（雨量/概率/气温） |
| **铁路 12306** | `kyfw.12306.cn/otn/leftTicket/queryZ` | `200 status:true` | 各方向班次售罄率（显示性偏好，假期逐日 7 天 × 21 个方向 + 深穗逐日曲线）。需会话 Cookie（自动建立），注意访问频率。响应常带 UTF-8 BOM，须用 `utf-8-sig` 解码 |

> 12306 是本次实测发现的最有价值免 Key 数据源：真实订票行为直接反映当年出行需求，用于对参考年客流做「当年热度」修正（售罄率每 ±10pp → 客流 ±6%，限幅 0.85–1.15），并输出「抢票热度」榜。

## B. 需注册账号申请 Key（免费额度，秒批）

| 平台 | 申请入口 | 实测无 Key 时 | 免费额度 | 接入方式 |
|---|---|---|---|---|
| **高德开放平台** | lbs.amap.com → 控制台 → 应用管理 → 创建应用 → 添加「Web服务」Key | `200 info:INVALID_USER_KEY infocode:10001` | 道路路况 5000 次/日；天气 30 万次/日 | 设置环境变量 `AMAP_KEY=xxx` 或 `config.yaml: amap.key` 即自动启用实时路况校准 |
| **和风天气** | dev.qweather.com → 注册 → 项目管理 → 创建 Key | `403 AP010003`（格式错） | 预报 1000 次/日 | 可作 Open-Meteo 的中文备选，改 `data/openmeteo.py` 同类接口 |
| **百度地图开放平台** | lbsyun.baidu.com → 应用管理 → 创建「服务端」AK | `200 status:101 AK参数不存在` | 天气 2000 次/日 | 备选路况/天气源 |
| **腾讯位置服务** | lbs.qq.com → 控制台 → Key 管理 | `200 status:311 key格式错误` | 文档说明个人开发者通常每接口/Key 1 万次/日；实际以控制台为准 | `TENCENT_MAP_KEY` 已接入当前驾车 ETA 观测；当日 121 时停止请求，待额度恢复自动尝试 |

**建议优先申请高德**：它是唯一有「道路级实时路况」的免费接口，正好补齐拥堵模型当前唯一的仿真短板。

## C. 需登录或商业合作（无公开 API）

| 平台 | 探测结果 | 获取途径 | 说明 |
|---|---|---|---|
| 小红书 | `edith.xiaohongshu.com` → `406 code:-1`（反爬签名） | 灵犀/聚光平台商业授权（xiaohongshu.com/business） | 需企业主体开户，种草数据目前用先验 |
| 抖音巨量算数 | `trendinsight.oceanengine.com/api/open/*` → 200 但需登录态 | 巨量算数平台注册账号（oceanengine.com）后网页查看，无开放 API | 同上 |
| 微信指数 | `index.weixin.qq.com` DNS 不可达（仅微信小程序内可用） | 微信内小程序「微信指数」人工查询 | 无网页端 |
| 百度指数 | 页面 200，数据接口需登录 Cookie | index.baidu.com 注册账号 | 网页查看，无官方 API |
| 携程 | `m.ctrip.com/restapi/...` → `403 11010 Operation Forbidden` | 携程开放平台（open.ctrip.com）商家/联盟接口，需企业资质 | OTA 数据目前用先验 |
| 美团 | `ihotel.meituan.com` → `403 openresty` | 美团开放平台（open.meituan.com）商家接口 | 同上 |
| 飞猪 | `h5api.m.trip.com` → DNS 不可达 | 飞猪/淘宝联盟接口需签约 | 同上 |
| 去哪儿 | 页面 200 | 数据接口与携程共用体系 | 同上 |

## D. 无程序化渠道

| 来源 | 探测结果 | 说明 |
|---|---|---|
| 交通运输部路网中心 | `chinahighway.com` 200（站点在），无公开路况 API | 仅新闻/报告发布，数据须商务对接 |

## E. 高德「道路路况」接入实测结论（2026-09-29，已用真实 Key 验证）

接口 `v3/traffic/status/road`，两条硬约束：

1. **道路名必须带国家高速编号前缀**
   | 路名 | 结果 |
   |---|---|
   | `G4京港澳高速` | `200 status:1`，返回 2 段（均速 85 km/h） |
   | `京港澳高速` | `UNKNOWN_ERROR infocode:20003` |

2. **并非 20 条走廊都在高德道路名索引内**。实测 20 条中 **7 条可查**：
   `G4 / G15 / G25 / G94 / G35 / G9411 / S2`；其余 13 条（S3、S30、G0422、S29、G80、
   G0423、G0425、S32、G1508、G6011、G55、G2518、SZLINK 深中通道）用编号名 + adcode +
   city 三种组合均返回 `UNKNOWN_ERROR`。这些走廊保持模型预测，报告与 `metrics.json`
   的 `provenance` 如实标注 `7/20`。

3. **免费 Key 有 QPS 限制**：连续请求会返回 `CGQPS_HAS_EXCEEDED_THE_LIMIT`，
   客户端已按 `REQUEST_PAUSE=0.5s` 逐条节流，并**逐条隔离异常**——单条查不到不再中断整轮。
   2026-09-29 20:39 再次调用返回 `USER_DAILY_QUERY_OVER_LIMIT (10044)`，当时道路观测为 0/20；
   客户端现在遇此码即停止本轮请求，报告标为失败而不冒称实时车速。额度重置前只能使用模型预测。

> 提示：`v3/traffic/status/circle` 与 `rectangle` 实测返回 `roads` 为空 / `INVALID_PARAMS`，
> 无法作为高速路况的替代入口。

## F. OTA / 社交信号的合规取数进度（2026-09-29）

- **高德住宿供给代理已接入并实测**：`v3/place/around` 使用 `types=100000`，统一查询各目的地中心周边 5km；41/41 地点返回，18 地点恰好为 600，疑似接口计数上限，已标记 `possibly_capped`。这是住宿地点检索数，**不是**携程等 OTA 的客房数、可订库存、价格或预订热度；不会用于改写模型。低频缓存 24h。
- **百度地图 AK 已验证并接入地点搜索**：`place/v2/search` 指定 `coord_type=1`（项目使用近似 WGS84）、严格半径 5km、`query=酒店$民宿$宾馆`，`page_size=1` 仅取 `total`；41/41 地点有数值、无地点触及官方文档的 150 条上限。单独缓存 24h，输出 `baidu_lodging_supply.csv` 和 `/observations/baidu-lodging-pois`；关键词检索口径不同且 `radius_limit=true` 会降低计数准确性，不应与高德数量相加。此 AK 不等于百度指数或慧眼商业客流权限，亦不提供 OTA 实时房价/预订量。
- **腾讯位置服务 Key 已验证，但已达当日调用上限**：官方 `ws/direction/v1/driving/` 首次实测深圳中心→虎门镇返回 `status=0`、路程约 64km、ETA 约 70 分钟及路况分段；WGS84→GCJ-02 官方坐标转换也返回 `status=0`。随后驾车接口返回 `status=121`（该 Key 每日调用量已达上限）。现已接入 **41 地点当前驾车 ETA 的独立采集流程**（1h TTL），本日写入停止调用标记，今日输出均为无可用观测，次日本地时区日期变化后自动尝试；如控制台提升额度可手动 `--force`。路况限额恢复前不宣称已采得全量数据；路线 ETA 不代替指定高速的实测车速、未来国庆预测、OTA 或社交数据。
- **OTA 合作渠道**：携程联盟/商旅开发者入口存在，但无法从公开页面核实全量酒店即时价格/库存、全平台成交量或免费调用权限；美团/飞猪/去哪儿亦须向平台申请符合用途的授权。只有取得接口文档、可用权限和用途许可后才能接入，不能用网站内隐式接口或登录态绕过限制。
- **社交合作渠道**：小红书营销开放平台偏向授权账户数据，未核实任意旅游目的地的全站热度接口；[微博话题搜索](https://open.weibo.com/wiki/2/search/topics)为需 OAuth 和高级权限的接口，并非免授权全站搜索；抖音垂搜是否获批和免费额度待官方控制台核实。不能把高德 POI 数量冒充社交热度。
- 获得许可的数据可先通过项目 `data/partner_observations.csv`（字段与范围见 `README.md`）单独导入展示；用户导入未经平台真实性核验，不会自动修改先验。`metrics.json` 中 OTA 与社交预测仍标为 `prior`。

## 立即行动清单

1. **高德 Web 服务 Key**（5 分钟，免审核）：注册 → 创建应用 → 选「Web服务」→ `cp .env.example .env` 并填入 `AMAP_KEY=xxx`（推荐，`.env` 已 gitignore）或直接 `export AMAP_KEY=xxx` → 运行 `scripts/refresh_live.py` 验证。优先级：环境变量 > `.env` > `config.yaml: amap.key`
2. 验证命令：`python -c "import sys; sys.path.insert(0,'src'); from holiday_traffic.data.amap import AmapTrafficClient; print(AmapTrafficClient('你的KEY').all_tti())"`（预期 7/20 条走廊，见 §E）
3. OTA/社媒如需观测数据，先取得平台授权并确认可导出字段、使用范围及期限，按 `README.md` 五列格式导入 `data/partner_observations.csv`；验证代表性后再决定是否单独校准模型，勿直接覆盖模拟先验。
