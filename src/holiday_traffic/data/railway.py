"""铁路 12306 余票客户端 (免 Key, 需会话 Cookie, 显示性偏好数据).

12306 余票是真实订票行为: 售罄率 = 该方向当日的需求强度.
用途:
  1. 方向级 D1 售罄率 -> 对参考年客流做「当年热度」修正(nowcast);
  2. 深圳北->广州南逐日售罄曲线 -> 与迁徙剖面交叉验证;
  3. 报告「抢票热度」榜(用户最关心).

注意: 余票仅开售预售期内(当前 15 天)日期; 售罄判定 = 无座/一等/二等/商务 全为 '无'/空/0.
座席字段(官方记录格式): 26=无座 29=一等 30=二等 31=商务/特等.

一轮全量查询约 33 次请求(20 城 × 1~2 站 + 深穗逐日曲线), 单请求往返常达数秒,
串行会拖慢引擎到 2 分钟级。故按线程建独立会话(CookieJar 非线程安全)并以
MAX_WORKERS 路并发, 单轮降到 ~30s 量级。
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .http import FetchError

logger = logging.getLogger("holiday_traffic.railway")

BASE = "https://kyfw.12306.cn/otn"
STATIONS_JS = BASE + "/resources/js/framework/station_name.js"
QUERY = BASE + "/leftTicket/queryZ"
SEAT_FIELDS = (26, 29, 30, 31)
MAX_WORKERS = 4          # 并发查询路数(兼顾礼貌抓取)
_QUERY_ERRORS = (FetchError, OSError, ValueError, KeyError)

# 城市 -> 主要客运站(按热度序, 运行时解析为电报码)
CITY_STATIONS: dict[str, list[str]] = {
    "广州": ["广州南", "广州"], "佛山": ["佛山西"], "东莞": ["虎门", "东莞南"],
    "惠州": ["惠州", "惠州南"], "汕尾": ["汕尾"], "汕头": ["汕头"], "潮州": ["潮汕"],
    "梅州": ["梅州"], "清远": ["清远"], "河源": ["河源"], "韶关": ["韶关"],
    "阳江": ["阳江"], "肇庆": ["肇庆东"], "云浮": ["云浮东"], "茂名": ["茂名"],
    "郴州": ["郴州西"], "贺州": ["贺州"], "珠海": ["珠海"], "中山": ["中山"],
    "江门": ["江门东", "江门"],
}
ORIGIN_STATIONS = ["深圳北", "深圳", "深圳东", "福田"]


class RailwayClient:
    def __init__(self, pause: float = 0.2, workers: int = MAX_WORKERS):
        self.pause = pause
        self.workers = max(1, workers)
        self._local = threading.local()
        self._codes: dict[str, str] | None = None
        self._codes_lock = threading.Lock()
        self._opener().open(BASE + "/leftTicket/init", timeout=15).read()  # 建立会话

    # ---------- 会话(每线程独立) ----------
    def _opener(self):
        """12306 需要会话 Cookie; CookieJar 非线程安全, 故每线程一个 opener."""
        op = getattr(self._local, "opener", None)
        if op is None:
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = [("User-Agent", "Mozilla/5.0"),
                             ("Referer", BASE + "/leftTicket/init")]
            self._local.opener = op
        return op

    def _map(self, fn, items: list) -> list:
        """有限并发 + 失败项串行重试一次.

        12306 对并发较敏感, 并发轮会随机丢少量请求; 丢掉的项改用单发重试,
        成功率显著更高。返回成功项的结果列表(顺序无关, 调用方按 key 聚合)。
        """
        idx = list(enumerate(items))

        def safe(p):
            i, x = p
            try:
                return i, fn(x)
            except _QUERY_ERRORS as e:
                logger.warning("12306 查询失败(第 %d 项 %r): %s", i, x, e)
                return i, None

        res: dict[int, object] = {}
        if self.workers > 1 and len(idx) > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                for i, r in ex.map(safe, idx):
                    if r is not None:
                        res[i] = r
        for i, x in idx:                      # 并发未命中的项串行补抓
            if i not in res:
                try:
                    res[i] = fn(x)
                except _QUERY_ERRORS:
                    pass
        return [res[i] for i in sorted(res)]

    # ---------- 车站码 ----------
    def _load_codes(self) -> dict[str, str]:
        if self._codes is None:
            with self._codes_lock:
                if self._codes is None:
                    # utf-8-sig: 12306 部分响应带 BOM, 用 utf-8 解码会让 json 解析失败
                    text = self._opener().open(STATIONS_JS, timeout=15).read() \
                        .decode("utf-8-sig", "ignore")
                    codes: dict[str, str] = {}
                    for rec in text.split("@")[1:]:
                        p = rec.split("|")
                        if len(p) > 2:
                            codes.setdefault(p[1], p[2])
                    self._codes = codes
        return self._codes

    def code(self, name: str) -> str:
        c = self._load_codes().get(name)
        if not c:
            raise FetchError(f"未知车站: {name}")
        return c

    # ---------- 余票 ----------
    def query(self, frm: str, to: str, date: str) -> dict:
        """返回 {'trains': n, 'soldout': m, 'rate': 售罄率}."""
        params = {"leftTicketDTO.train_date": date, "leftTicketDTO.from_station": frm,
                  "leftTicketDTO.to_station": to, "purpose_codes": "ADULT"}
        # utf-8-sig: 12306 响应常带 UTF-8 BOM。用 utf-8 解码后 json.loads 会抛
        # "Unexpected UTF-8 BOM", 导致并发查询 100% 失败、全部退化到串行重试,
        # 一轮从 ~20s 拖到 115s。用 utf-8-sig 同时兼容带/不带 BOM 的响应。
        data = json.loads(self._opener().open(QUERY + "?" + urllib.parse.urlencode(params),
                                              timeout=15).read().decode("utf-8-sig"))
        if not data.get("status"):
            raise FetchError(f"12306 查询失败: {data.get('messages')}")
        rows = [r.split("|") for r in data["data"]["result"]]
        sold = sum(1 for f in rows if all(f[i] in ("", "无", "0") for i in SEAT_FIELDS))
        time.sleep(self.pause)
        return {"trains": len(rows), "soldout": sold,
                "rate": round(sold / len(rows), 3) if rows else 0.0}

    def origin(self) -> str:
        """始发站: 取 ORIGIN_STATIONS 中第一个在官方站名表里存在的站(默认深圳北).

        只用单一始发站而非合并多站: 深圳北是高铁主站, 与假期城际出行口径一致;
        合并普铁站(深圳/深圳东)会把大量长途普速车次混入, 稀释城际需求信号。
        """
        for s in ORIGIN_STATIONS:
            code = self._load_codes().get(s)
            if code:
                return code
        raise FetchError("未解析到深圳始发站电报码")

    def demand_by_city(self, date: str, cities: list[str] | None = None) -> dict[str, dict]:
        """各方向当日售罄率(同方向多车站合并, 如 广州南 + 广州); 并发查询."""
        cities = cities or list(CITY_STATIONS)
        frm = self.origin()
        codes = self._load_codes()
        pairs = [(city, codes[name]) for city in cities for name in CITY_STATIONS.get(city, [])
                 if name in codes]

        def one(p):
            city, code = p
            return city, self.query(frm, code, date)

        out: dict[str, dict] = {}
        for city, r in self._map(one, pairs):
            if not r:
                continue
            d = out.setdefault(city, {"trains": 0, "soldout": 0})
            d["trains"] += r["trains"]
            d["soldout"] += r["soldout"]
        return {c: {"trains": v["trains"], "soldout": v["soldout"],
                    "rate": round(v["soldout"] / v["trains"], 3)}
                for c, v in out.items() if v["trains"]}

    def daily_curve(self, frm: str, to: str, dates: list[str]) -> dict[str, dict]:
        """某方向逐日售罄率; 并发查询, 失败的日期直接省略."""
        f, t = self.code(frm), self.code(to)

        def one(d):
            return d, self.query(f, t, d)

        return {d: r for d, r in self._map(one, list(dates)) if r}
