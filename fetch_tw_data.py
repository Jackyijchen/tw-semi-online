#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股半導體通路儀表板 — 多來源資料抓取與交叉驗證（本機執行，僅用 Python 標準函式庫）

資料來源（全部免 API Key；FinMind Token 選填）
  股價   ：TWSE 個股日成交 / TPEx 個股日成交（官方）、TWSE/TPEx OpenAPI 當日行情、Yahoo Finance、FinMind
  法人   ：TWSE T86 / TPEx 三大法人（官方）、FinMind
  月營收 ：MOPS 公開資訊觀測站月報（官方）、TWSE/TPEx OpenAPI 月營收、FinMind
  存貨   ：Yahoo Finance 財報時序、FinMind 資產負債表 + 損益表

輸出
  data/dashboard_data.js   ← 儀表板 HTML 直接以 <script> 讀取（file:// 開啟無 CORS 問題）
  data/dashboard_data.json ← 同內容 JSON
  data/db.json             ← 累積資料庫（增量更新用）
  logs/fetch_YYYYMMDD.log  ← 執行紀錄

用法
  python fetch_tw_data.py                 # 一般執行（排程用）
  python fetch_tw_data.py --add 2454 2330 # 加入觀察名單後抓取
  python fetch_tw_data.py --remove 2454   # 移出觀察名單
  python fetch_tw_data.py --full          # 忽略快取，完整重抓
  python fetch_tw_data.py --no-finmind    # 不使用 FinMind
  python fetch_tw_data.py --vendor        # 下載圖表函式庫到 vendor/（離線可用）
"""
import argparse, gzip, html, json, logging, os, re, ssl, sys, time, zlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VERSION = "3.0"
TPE = timezone(timedelta(hours=8))
# TWSEMI_HOME：Android App 或其他環境可指定資料夾（預設為程式所在資料夾）
BASE = os.environ.get("TWSEMI_HOME") or os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")
VENDOR_DIR = os.path.join(BASE, "vendor")
CONFIG_PATH = os.path.join(BASE, "config.json")
DB_PATH = os.path.join(DATA_DIR, "db.json")

DEFAULT_CONFIG = {
    "watchlist": ["3702", "3036", "8112"],
    "names": {"3702": "大聯大", "3036": "文曄", "8112": "至上"},
    "market_override": {},
    "price_months": 24,
    "inst_days": 60,
    "revenue_months": 26,
    "schedule": {"time_taipei": "19:30", "weekdays": "Mon-Fri", "note": "台股 13:30 收盤後 6 小時"},
    "finmind": {"enabled": True, "token": ""},
    "yahoo": {"enabled": True},
    "tolerance": {"price_pct": 0.5, "volume_pct": 3.0, "revenue_pct": 0.1, "inst_lots": 1, "inventory_pct": 1.0},
    "request_interval_sec": {"TWSE": 3.5, "TPEx": 2.5, "MOPS": 2.0, "OpenAPI": 0.8, "Yahoo": 0.8, "FinMind": 0.6},
}

VENDOR_FILES = {
    "chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
    "lightweight-charts.js": "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js",
    "tailwind.js": "https://cdn.tailwindcss.com/3.4.5",
}

log = logging.getLogger("fetch")


# ───────────────────────────── 共用工具 ─────────────────────────────
def now_tpe():
    return datetime.now(TPE)


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"<[^>]+>", "", str(s)).strip().replace(",", "").replace("+", "")
    if s in ("", "-", "--", "---", "X", "N/A", "除權息", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group()) if m else None


def roc_to_iso(s):
    """'115/10/02' 或 '1151002' → '2026-10-02'"""
    s = str(s).strip().replace("*", "")
    m = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{3})(\d{2})(\d{2})$", s)
    if m:
        return f"{int(m.group(1)) + 1911:04d}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def ym_add(y, m, k):
    t = y * 12 + (m - 1) + k
    return t // 12, t % 12 + 1


def field_idx(fields, *cands, exact=False, default=None):
    for c in cands:
        for i, f in enumerate(fields):
            f2 = re.sub(r"\s", "", str(f))
            if (f2 == c) if exact else (c in f2):
                return i
    return default


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # 損毀時保留備份
        log.warning("讀取 %s 失敗（%s），改用預設值並備份原檔", path, e)
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        return default


def save_json(path, obj, compact=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ───────────────────────────── HTTP（節流 + 重試 + 健康統計）─────────────────────────────
class Http:
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    def __init__(self, intervals):
        self.ctx = ssl.create_default_context()
        # Python 3.13 起預設嚴格 X509 檢查，部分政府網站憑證會失敗；仍保留憑證驗證，只關閉嚴格模式
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            self.ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        try:  # Android / 精簡版 Python 可能缺系統憑證，補上 certifi
            import certifi
            self.ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        self.intervals = intervals
        self.last = {}
        self.health = {}
        self.disabled = set()

    def _h(self, src):
        return self.health.setdefault(src, {"ok": 0, "fail": 0, "empty": 0, "lastError": ""})

    def get(self, url, src, kind="json", headers=None, retries=3, timeout=25):
        if src in self.disabled:
            return None
        wait = self.intervals.get(src, 1.0) - (time.time() - self.last.get(src, 0))
        if wait > 0:
            time.sleep(wait)
        h = self._h(src)
        for attempt in range(1, retries + 1):
            self.last[src] = time.time()
            try:
                req = Request(url, headers={"User-Agent": self.UA, "Accept-Encoding": "gzip, deflate",
                                            "Accept": "application/json, text/html, */*",
                                            "Accept-Language": "zh-TW,zh;q=0.9", **(headers or {})})
                with urlopen(req, timeout=timeout, context=self.ctx) as r:
                    raw = r.read()
                    enc = (r.headers.get("Content-Encoding") or "").lower()
                    ctype = r.headers.get("Content-Type") or ""
                if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                if kind == "bytes":
                    h["ok"] += 1
                    return raw
                text = self._decode(raw, ctype)
                if kind == "text":
                    h["ok"] += 1
                    return text
                data = json.loads(text)
                h["ok"] += 1
                return data
            except HTTPError as e:
                err = f"HTTP {e.code}"
                if e.code in (401, 402, 403) and src == "FinMind":
                    h["fail"] += 1
                    h["lastError"] = err + "（需註冊 Token 或額度用盡）"
                    self.disabled.add(src)
                    log.warning("FinMind 拒絕存取（%s），本次執行停用 FinMind", err)
                    return None
                if e.code == 404:
                    h["empty"] += 1
                    return None
            except json.JSONDecodeError:
                err = "回應不是 JSON（可能被暫時封鎖或改版）"
            except (URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError) as e:
                err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            except Exception as e:  # 任何非預期錯誤都視為該來源失敗，不中斷整體流程
                err = f"{type(e).__name__}: {e}"
            log.debug("  %s 第 %d 次失敗：%s → %s", src, attempt, err, url)
            if attempt < retries:
                time.sleep(4 * attempt + (10 if src == "TWSE" else 0))
        h["fail"] += 1
        h["lastError"] = err
        log.warning("  [%s] 失敗：%s  %s", src, err, url[:140])
        return None

    @staticmethod
    def _decode(raw, ctype):
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        cands = [m.group(1)] if m else []
        head = raw[:2000].decode("ascii", "ignore")
        m2 = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
        if m2:
            cands.append(m2.group(1))
        cands += ["utf-8", "cp950", "big5-hkscs"]
        for c in cands:
            c = {"big5": "cp950"}.get(c.lower(), c)
            try:
                return raw.decode(c)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "replace")


# ───────────────────────────── 各資料源抓取 ─────────────────────────────
class Sources:
    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg

    # ---------- 官方：當日行情（同時用於判斷上市/上櫃） ----------
    def openapi_quotes(self):
        out = {"TWSE": {}, "TPEx": {}}
        j = self.http.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", "OpenAPI")
        if isinstance(j, list):
            for r in j:
                code = str(r.get("Code", "")).strip()
                c = num(r.get("ClosingPrice"))
                d = roc_to_iso(r.get("Date", "")) if r.get("Date") else None
                out["TWSE"][code] = {"name": r.get("Name", "").strip(), "date": d,
                                     "ohlcv": [num(r.get("OpeningPrice")), num(r.get("HighestPrice")),
                                               num(r.get("LowestPrice")), c, num(r.get("TradeVolume"))]}
        j = self.http.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", "OpenAPI")
        if isinstance(j, list):
            for r in j:
                code = str(r.get("SecuritiesCompanyCode", "")).strip()
                out["TPEx"][code] = {"name": str(r.get("CompanyName", "")).strip(),
                                     "date": roc_to_iso(r.get("Date", "")),
                                     "ohlcv": [num(r.get("Open")), num(r.get("High")), num(r.get("Low")),
                                               num(r.get("Close")), num(r.get("TradingShares"))]}
        return out

    # ---------- 官方：個股月份日成交 ----------
    def twse_month(self, code, y, m):
        for url in (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={y}{m:02d}01&stockNo={code}&response=json",
                    f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={y}{m:02d}01&stockNo={code}"):
            j = self.http.get(url, "TWSE")
            if not isinstance(j, dict):
                continue
            if j.get("stat") != "OK":
                if "沒有符合" in str(j.get("stat", "")) or "查詢日期" in str(j.get("stat", "")):
                    self.http._h("TWSE")["empty"] += 1
                    return {}
                continue
            f = j.get("fields", [])
            i = [field_idx(f, "日期", default=0), field_idx(f, "開盤", default=3), field_idx(f, "最高", default=4),
                 field_idx(f, "最低", default=5), field_idx(f, "收盤", default=6), field_idx(f, "成交股數", default=1)]
            return self._rows(j.get("data", []), i, vol_mul=1)
        return None

    def tpex_month(self, code, y, m):
        j = self.http.get(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={code}"
                          f"&date={y}/{m:02d}/01&id=&response=json", "TPEx")
        if isinstance(j, dict) and j.get("tables"):
            t = j["tables"][0]
            f = t.get("fields", [])
            mul = 1000 if (not f or any("仟股" in str(x) for x in f)) else 1
            i = [field_idx(f, "日期", "日 期", default=0), field_idx(f, "開盤", default=3), field_idx(f, "最高", default=4),
                 field_idx(f, "最低", default=5), field_idx(f, "收盤", default=6), field_idx(f, "成交仟股", "成交股數", default=1)]
            return self._rows(t.get("data", []), i, vol_mul=mul)
        j = self.http.get(f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
                          f"?l=zh-tw&d={y - 1911}/{m:02d}&stkno={code}", "TPEx")
        if isinstance(j, dict) and "aaData" in j:
            return self._rows(j["aaData"], [0, 3, 4, 5, 6, 1], vol_mul=1000)
        return None

    @staticmethod
    def _rows(data, idx, vol_mul):
        rows = {}
        for r in data:
            try:
                d = roc_to_iso(r[idx[0]])
                o, h, l, c, v = (num(r[k]) for k in idx[1:])
            except (IndexError, TypeError):
                continue
            if not d or None in (o, h, l, c) or c <= 0:
                continue
            rows[d] = [o, h, l, c, (v or 0) * vol_mul]
        return rows

    # ---------- Yahoo ----------
    def yahoo_chart(self, code, market, rng="2y"):
        sym = code + (".TW" if market == "TWSE" else ".TWO")
        for host in ("query1", "query2"):
            j = self.http.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
                              f"&includePrePost=false&events=div%2Csplit", "Yahoo")
            try:
                res = j["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                rows = {}
                for k, t in enumerate(res.get("timestamp") or []):
                    vals = [q.get(x, [None])[k] for x in ("open", "high", "low", "close", "volume")]
                    if vals[3] is None or vals[0] is None:
                        continue
                    d = datetime.fromtimestamp(t, TPE).strftime("%Y-%m-%d")
                    rows[d] = [round(vals[0], 2), round(vals[1], 2), round(vals[2], 2), round(vals[3], 2), vals[4] or 0]
                return rows
            except (TypeError, KeyError, IndexError):
                continue
        return None

    def yahoo_fundamentals(self, code, market):
        sym = code + (".TW" if market == "TWSE" else ".TWO")
        p2 = int(time.time())
        j = self.http.get(f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
                          f"?symbol={sym}&type=quarterlyInventory,quarterlyCostOfRevenue,quarterlyTotalRevenue"
                          f"&merge=false&padTimeSeries=true&period1=1577836800&period2={p2}", "Yahoo")
        try:
            out = {}
            for res in j["timeseries"]["result"]:
                typ = res["meta"]["type"][0]
                for it in res.get(typ) or []:
                    if not it or not it.get("reportedValue"):
                        continue
                    o = out.setdefault(it["asOfDate"], {})
                    o[typ] = float(it["reportedValue"]["raw"])
            rows = {}
            for d, o in out.items():
                inv = o.get("quarterlyInventory")
                cogs = o.get("quarterlyCostOfRevenue") or (o.get("quarterlyTotalRevenue") and o["quarterlyTotalRevenue"] * 0.955)
                if inv:
                    rows[d] = [inv, cogs]
            return rows
        except (TypeError, KeyError, IndexError):
            return None

    # ---------- FinMind ----------
    def finmind(self, dataset, code, start):
        fm = self.cfg["finmind"]
        if not fm.get("enabled"):
            return None
        p = {"dataset": dataset, "data_id": code, "start_date": start}
        hdr = {}
        if fm.get("token"):
            p["token"] = fm["token"]
            hdr["Authorization"] = "Bearer " + fm["token"]
        j = self.http.get("https://api.finmindtrade.com/api/v4/data?" + urlencode(p), "FinMind", headers=hdr, retries=2)
        if isinstance(j, dict) and j.get("status") == 200:
            return j.get("data") or []
        if isinstance(j, dict):
            h = self.http._h("FinMind")
            h["fail"] += 1
            h["lastError"] = str(j.get("msg", "status " + str(j.get("status"))))[:120]
            if "level" in h["lastError"].lower() or "token" in h["lastError"].lower() or j.get("status") in (401, 402):
                self.http.disabled.add("FinMind")
        return None

    # ---------- 官方：三大法人 ----------
    def twse_t86(self, d):
        j = self.http.get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d.replace('-', '')}"
                          f"&selectType=ALLBUT0999&response=json", "TWSE")
        if not isinstance(j, dict):
            return None
        if j.get("stat") != "OK":
            return {} if ("沒有符合" in str(j.get("stat", "")) or not j.get("data")) else None
        f = j.get("fields", [])
        i_f1 = field_idx(f, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數", "外陸資買賣超股數")
        i_f2 = field_idx(f, "外資自營商買賣超股數")
        i_t = field_idx(f, "投信買賣超股數")
        i_d = field_idx(f, "自營商買賣超股數", exact=True)
        i_tot = field_idx(f, "三大法人買賣超股數")
        if i_d is None:
            i_d = field_idx(f, "自營商買賣超股數")
        out = {}
        for r in j.get("data", []):
            code = str(r[0]).strip()
            fo = (num(r[i_f1]) or 0) + ((num(r[i_f2]) or 0) if i_f2 is not None else 0)
            tr = num(r[i_t]) or 0 if i_t is not None else 0
            de = num(r[i_d]) or 0 if i_d is not None else 0
            tot = num(r[i_tot]) if i_tot is not None else None
            out[code] = [fo, tr, de, tot if tot is not None else fo + tr + de]
        return out

    def tpex_inst(self, d):
        y, m, dd = d.split("-")
        rows = None
        j = self.http.get(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW"
                          f"&date={y}/{m}/{dd}&response=json", "TPEx")
        if isinstance(j, dict) and j.get("tables"):
            rows = j["tables"][0].get("data", [])
        else:
            j = self.http.get(f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
                              f"?l=zh-tw&se=EW&t=D&d={int(y) - 1911}/{m}/{dd}", "TPEx")
            if isinstance(j, dict) and "aaData" in j:
                rows = j["aaData"]
        if rows is None:
            return None
        out = {}
        for r in rows:
            if len(r) < 24:
                continue
            code = str(r[0]).strip()
            fo, tr, de, tot = num(r[10]) or 0, num(r[13]) or 0, num(r[22]) or 0, num(r[23])
            if tot is not None and abs(fo + tr + de - tot) > 1000:  # 欄位順序與預期不同時改用分項加總
                fo = (num(r[4]) or 0) + (num(r[7]) or 0)
            out[code] = [fo, tr, de, tot if tot is not None else fo + tr + de]
        return out

    # ---------- 官方：月營收 ----------
    def mops_month(self, market, y, m, kind=0):
        mk = "sii" if market == "TWSE" else "otc"
        for host in ("mopsov.twse.com.tw", "mops.twse.com.tw"):
            txt = self.http.get(f"https://{host}/nas/t21/{mk}/t21sc03_{y - 1911}_{m}_{kind}.html", "MOPS", kind="text", retries=2)
            if txt and "公司代號" in txt:
                return self._parse_mops(txt)
        return None

    @staticmethod
    def _parse_mops(txt):
        out = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", txt, flags=re.S | re.I):
            tds = [html.unescape(re.sub(r"<[^>]+>", "", x)).replace("\xa0", " ").strip()
                   for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
            if len(tds) >= 10 and re.fullmatch(r"\d{4,6}[A-Z]?", tds[0]):
                rev = num(tds[2])
                if rev is None:
                    continue
                out[tds[0]] = {"name": tds[1], "rev": rev * 1000, "prev": (num(tds[3]) or 0) * 1000,
                               "ly": (num(tds[4]) or 0) * 1000, "mom": num(tds[5]), "yoy": num(tds[6]),
                               "cum": (num(tds[7]) or 0) * 1000, "cumYoy": num(tds[9])}
        return out

    def openapi_revenue(self):
        out = {}
        for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
            j = self.http.get(url, "OpenAPI")
            if not isinstance(j, list):
                continue
            for r in j:
                k = {re.sub(r"\s", "", key): v for key, v in r.items()}
                code = str(k.get("公司代號", "")).strip()
                ymr = str(k.get("資料年月", "")).strip()
                rev = next((num(v) for kk, v in k.items() if kk.endswith("當月營收") and "累計" not in kk and "去年" not in kk), None)
                ly = next((num(v) for kk, v in k.items() if "去年當月營收" in kk), None)
                yoy = next((num(v) for kk, v in k.items() if "去年同月增減" in kk), None)
                if code and len(ymr) >= 5 and rev is not None:
                    ym = (int(ymr[:-2]) + 1911) * 100 + int(ymr[-2:])
                    out.setdefault(code, {})[str(ym)] = {"rev": rev * 1000, "ly": (ly or 0) * 1000, "yoy": yoy}
        return out


def _guard(cls):
    import functools
    for name, fn in list(vars(cls).items()):
        if callable(fn) and not name.startswith("_"):
            def wrap(fn=fn, name=name):
                @functools.wraps(fn)
                def inner(self, *a, **k):
                    try:
                        return fn(self, *a, **k)
                    except Exception as e:
                        log.warning("  解析 %s%s 失敗：%s: %s（可能是來源改版，已略過）", name, a[:3], type(e).__name__, e)
                        return None
                return inner
            setattr(cls, name, wrap())
    return cls


_guard(Sources)


# ───────────────────────────── 交叉驗證與合併 ─────────────────────────────
PRICE_PRI = ["official", "openapi", "yahoo", "finmind"]


def close_enough(a, b, pct):
    return a is not None and b is not None and abs(a - b) <= abs(b) * pct / 100 + 1e-9


def vote(vals, key, pri, tol_fn):
    """vals: {src: value}；以容差分群，取最多來源支持的群組（同票數依優先序）"""
    groups = []
    for s in sorted(vals, key=lambda x: pri.index(x) if x in pri else 99):
        for g in groups:
            if tol_fn(key(vals[s]), key(vals[g[0]])):
                g.append(s)
                break
        else:
            groups.append([s])
    groups.sort(key=lambda g: -len(g))
    best = groups[0]
    status = "S" if len(vals) == 1 else ("V" if len(groups) == 1 else "X")
    return best[0], best, status


def merge_prices(srcs, tol):
    dates = sorted(set().union(*[set(v) for v in srcs.values()])) if srcs else []
    out, cnt, conflicts = [], Counter(), []
    for d in dates:
        vals = {s: srcs[s][d] for s in srcs if d in srcs[s]}
        pick, agree, st = vote(vals, lambda x: x[3], PRICE_PRI, lambda a, b: close_enough(a, b, tol["price_pct"]))
        o, h, l, c, v = vals[pick]
        out.append([d, o, h, l, c, round((v or 0) / 1000), st, "+".join(sorted(vals))])
        cnt[st] += 1
        if st == "X":
            conflicts.append({"date": d, "values": {s: vals[s][3] for s in vals}, "used": pick})
    return out, summarize(srcs, cnt, conflicts, dates[-1] if dates else None)


def merge_revenue(srcs, tol):
    yms = sorted(set().union(*[set(v) for v in srcs.values()])) if srcs else []
    out, cnt, conflicts = [], Counter(), []
    pri = ["mops", "openapi", "finmind"]
    prev_rev = None
    for ym in yms:
        vals = {s: srcs[s][ym] for s in srcs if ym in srcs[s]}
        pick, agree, st = vote(vals, lambda x: x["rev"], pri, lambda a, b: close_enough(a, b, tol["revenue_pct"]))
        r = vals[pick]
        yoy = r.get("yoy")
        if yoy is None and r.get("ly"):
            yoy = (r["rev"] / r["ly"] - 1) * 100
        mom = r.get("mom")
        if mom is None and prev_rev:
            mom = (r["rev"] / prev_rev - 1) * 100
        prev_rev = r["rev"]
        out.append([int(ym), r["rev"], None if yoy is None else round(yoy / 100, 6),
                    None if mom is None else round(mom / 100, 6), st, r.get("ly") or None, "+".join(sorted(vals))])
        cnt[st] += 1
        if st == "X":
            conflicts.append({"date": ym, "values": {s: vals[s]["rev"] for s in vals}, "used": pick})
    # 若年增率缺漏（例如只有 FinMind），以 12 個月前營收補算
    by = {row[0]: row[1] for row in out}
    for row in out:
        if row[2] is None:
            ly = by.get(row[0] - 100)
            if ly:
                row[2] = round(row[1] / ly - 1, 6)
    return out, summarize(srcs, cnt, conflicts, yms[-1] if yms else None)


def merge_inst(srcs, tol):
    dates = sorted(set().union(*[set(v) for v in srcs.values()])) if srcs else []
    out, cnt, conflicts = [], Counter(), []
    lim = tol["inst_lots"] * 1000 + 1
    for d in dates:
        vals = {s: srcs[s][d] for s in srcs if d in srcs[s]}
        pick, agree, st = vote(vals, lambda x: tuple(x[:3]), ["official", "finmind"],
                               lambda a, b: all(abs(p - q) <= lim for p, q in zip(a, b)))
        fo, tr, de, tot = vals[pick]
        if tot is not None and abs(fo + tr + de - tot) > 2000 and st != "X":
            st = "X"  # 官方資料內部加總不符
        out.append([d, round(fo / 1000), round(tr / 1000), round(de / 1000),
                    round((tot if tot is not None else fo + tr + de) / 1000), st, "+".join(sorted(vals))])
        cnt[st] += 1
        if st == "X":
            conflicts.append({"date": d, "values": {s: [round(x / 1000) for x in vals[s][:3]] for s in vals}, "used": pick})
    return out, summarize(srcs, cnt, conflicts, dates[-1] if dates else None)


def merge_inventory(srcs, tol):
    # 單位自動校正：兩來源若相差約 1000 倍（元 vs 仟元）則對齊
    if "yahoo" in srcs and "finmind" in srcs:
        common = set(srcs["yahoo"]) & set(srcs["finmind"])
        ratios = [srcs["yahoo"][d][0] / srcs["finmind"][d][0] for d in common if srcs["finmind"][d][0]]
        if ratios:
            r = sorted(ratios)[len(ratios) // 2]
            if 900 < r < 1100:
                srcs["finmind"] = {d: [v[0] * 1000, (v[1] or 0) * 1000] for d, v in srcs["finmind"].items()}
    dates = sorted(set().union(*[set(v) for v in srcs.values()])) if srcs else []
    out, cnt, conflicts = [], Counter(), []
    for d in dates:
        vals = {s: srcs[s][d] for s in srcs if d in srcs[s]}
        pick, agree, st = vote(vals, lambda x: x[0], ["yahoo", "finmind"], lambda a, b: close_enough(a, b, tol["inventory_pct"]))
        inv, cogs = vals[pick]
        if not cogs:
            cogs = next((vals[s][1] for s in vals if vals[s][1]), None)
        days = inv / cogs * 91.25 if cogs else None
        out.append([d, inv, cogs, None if days is None else round(days, 2), st, "+".join(sorted(vals))])
        cnt[st] += 1
        if st == "X":
            conflicts.append({"date": d, "values": {s: vals[s][0] for s in vals}, "used": pick})
    return out, summarize(srcs, cnt, conflicts, dates[-1] if dates else None)


def summarize(srcs, cnt, conflicts, last):
    total = sum(cnt.values())
    multi = cnt["V"] + cnt["X"]
    return {"sources": {s: len(v) for s, v in srcs.items() if v}, "V": cnt["V"], "S": cnt["S"], "X": cnt["X"],
            "total": total, "agreeRate": round(cnt["V"] / multi, 4) if multi else None,
            "lastDate": last, "conflicts": conflicts[-15:]}


# ───────────────────────────── 主流程 ─────────────────────────────
class Runner:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.http = Http(cfg["request_interval_sec"])
        self.src = Sources(self.http, cfg)
        self.db = load_json(DB_PATH, {"version": 2, "stocks": {}, "meta": {}})
        self.db.setdefault("meta", {}).setdefault("revMiss", {})
        self.warnings = []
        self.today = now_tpe().date()

    def st(self, code):
        s = self.db["stocks"].setdefault(code, {})
        for k in ("prices", "revenue", "inst", "inventory"):
            s.setdefault(k, {})
        return s

    # 判斷上市 / 上櫃
    def resolve_markets(self, quotes):
        for code in self.cfg["watchlist"]:
            s = self.st(code)
            mk = self.cfg["market_override"].get(code)
            if not mk:
                if code in quotes["TWSE"]:
                    mk = "TWSE"
                elif code in quotes["TPEx"]:
                    mk = "TPEx"
                else:
                    mk = s.get("market")
            if not mk:
                y, m = self.today.year, self.today.month
                mk = "TWSE" if self.src.twse_month(code, y, m) else ("TPEx" if self.src.tpex_month(code, y, m) else "TWSE")
            s["market"] = mk
            nm = self.cfg["names"].get(code) or (quotes.get(mk, {}).get(code) or {}).get("name") or s.get("name") or code
            s["name"] = nm
            log.info("  %s %s → %s", code, nm, "上市 TWSE" if mk == "TWSE" else "上櫃 TPEx")

    def step_prices(self, quotes):
        tol_months = self.cfg["price_months"]
        cur = (self.today.year, self.today.month)
        for code in self.cfg["watchlist"]:
            s = self.st(code)
            mk = s["market"]
            off = s["prices"].setdefault("official", {})
            done = set(s.setdefault("priceMonthsDone", []))
            fetched = 0
            for k in range(tol_months, -1, -1):
                y, m = ym_add(*cur, -k)
                key = f"{y}{m:02d}"
                recent = k == 0 or (k == 1 and self.today.day <= 7)
                if key in done and not recent and not self.args.full:
                    continue
                rows = self.src.twse_month(code, y, m) if mk == "TWSE" else self.src.tpex_month(code, y, m)
                if rows is None:
                    continue
                off.update(rows)
                fetched += 1
                if not recent:
                    done.add(key)
            s["priceMonthsDone"] = sorted(done)
            # OpenAPI 當日行情
            q = quotes.get(mk, {}).get(code)
            if q and q["ohlcv"][3]:
                d = q["date"]  # 無日期欄位時不採用，避免錯置日期
                if d:
                    s["prices"].setdefault("openapi", {})[d] = q["ohlcv"]
            # Yahoo
            if self.cfg["yahoo"]["enabled"]:
                y = self.src.yahoo_chart(code, mk, "2y" if (self.args.full or not s["prices"].get("yahoo")) else "3mo")
                if y:
                    s["prices"].setdefault("yahoo", {}).update(y)
            # FinMind（交叉驗證近 120 天）
            fm = self.src.finmind("TaiwanStockPrice", code, (self.today - timedelta(days=180)).isoformat())
            if fm:
                dst = s["prices"].setdefault("finmind", {})
                for r in fm:
                    if num(r.get("close")):
                        dst[r["date"]] = [num(r["open"]), num(r["max"]), num(r["min"]), num(r["close"]),
                                          num(r.get("Trading_Volume")) or 0]
            log.info("  股價 %s：官方 %d 筆（本次抓 %d 個月）、Yahoo %d、FinMind %d、OpenAPI %d", code, len(off), fetched,
                     len(s["prices"].get("yahoo", {})), len(s["prices"].get("finmind", {})), len(s["prices"].get("openapi", {})))
            if not off and not s["prices"].get("yahoo"):
                self.warnings.append(f"{code} 無法取得任何股價資料")

    def trading_days(self, market, n):
        days = set()
        for code in self.cfg["watchlist"]:
            s = self.st(code)
            if s.get("market") == market:
                days |= set(s["prices"].get("official", {})) | set(s["prices"].get("yahoo", {}))
        return sorted(days)[-n:]

    def step_inst(self):
        n = self.cfg["inst_days"]
        for mk in ("TWSE", "TPEx"):
            codes = [c for c in self.cfg["watchlist"] if self.st(c).get("market") == mk]
            if not codes:
                continue
            days = self.trading_days(mk, n)
            need = [d for d in days if self.args.full or any(d not in self.st(c)["inst"].get("official", {}) for c in codes)]
            log.info("  三大法人 %s：需抓 %d 天", mk, len(need))
            for i, d in enumerate(need, 1):
                data = self.src.twse_t86(d) if mk == "TWSE" else self.src.tpex_inst(d)
                if data is None:
                    continue
                if not data:  # 該日無資料（休市）
                    continue
                for c in codes:
                    self.st(c)["inst"].setdefault("official", {})[d] = data.get(c, [0, 0, 0, 0])
                if i % 10 == 0:
                    log.info("    …%d/%d", i, len(need))
        start = (self.today - timedelta(days=int(n * 1.6) + 10)).isoformat()
        for code in self.cfg["watchlist"]:
            fm = self.src.finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
            if not fm:
                continue
            dst = self.st(code)["inst"].setdefault("finmind", {})
            agg = {}
            for r in fm:
                o = agg.setdefault(r["date"], [0, 0, 0])
                net = (num(r.get("buy")) or 0) - (num(r.get("sell")) or 0)
                nm = r.get("name", "")
                if "Foreign" in nm:
                    o[0] += net
                elif "Investment_Trust" in nm:
                    o[1] += net
                elif "Dealer" in nm:
                    o[2] += net
            for d, (a, b, c) in agg.items():
                dst[d] = [a, b, c, a + b + c]

    def step_revenue(self):
        n = self.cfg["revenue_months"]
        miss = self.db["meta"]["revMiss"]
        last_y, last_m = ym_add(self.today.year, self.today.month, -1)
        for mk in ("TWSE", "TPEx"):
            codes = [c for c in self.cfg["watchlist"] if self.st(c).get("market") == mk]
            if not codes:
                continue
            for k in range(n - 1, -1, -1):
                y, m = ym_add(last_y, last_m, -k)
                ym = str(y * 100 + m)
                recent = k <= 1
                need = [c for c in codes if (self.args.full or recent or ym not in self.st(c)["revenue"].get("mops", {}))
                        and ym not in miss.get(c, [])]
                if not need:
                    continue
                page = self.src.mops_month(mk, y, m, 0)
                if page is None:
                    continue
                if any(c not in page for c in need):
                    page.update(self.src.mops_month(mk, y, m, 1) or {})  # KY / 外國企業
                for c in need:
                    if c in page:
                        self.st(c)["revenue"].setdefault("mops", {})[ym] = page[c]
                    elif k >= 3:
                        miss.setdefault(c, []).append(ym)
        oa = self.src.openapi_revenue()
        for c in self.cfg["watchlist"]:
            if c in oa:
                self.st(c)["revenue"].setdefault("openapi", {}).update(oa[c])
            fm = self.src.finmind("TaiwanStockMonthRevenue", c, (self.today - timedelta(days=365 * 3)).isoformat())
            if fm:
                dst = self.st(c)["revenue"].setdefault("finmind", {})
                for r in fm:
                    yy, mm = int(r.get("revenue_year") or 0), int(r.get("revenue_month") or 0)
                    if yy and mm:
                        dst[str(yy * 100 + mm)] = {"rev": num(r["revenue"]), "ly": None, "yoy": None}
            log.info("  月營收 %s：MOPS %d 月、OpenAPI %d、FinMind %d", c, len(self.st(c)["revenue"].get("mops", {})),
                     len(self.st(c)["revenue"].get("openapi", {})), len(self.st(c)["revenue"].get("finmind", {})))

    def step_inventory(self):
        start = (self.today - timedelta(days=365 * 3)).isoformat()
        for c in self.cfg["watchlist"]:
            s = self.st(c)
            if self.cfg["yahoo"]["enabled"]:
                y = self.src.yahoo_fundamentals(c, s["market"])
                if y:
                    s["inventory"]["yahoo"] = y
            bs = self.src.finmind("TaiwanStockBalanceSheet", c, start)
            fs = self.src.finmind("TaiwanStockFinancialStatements", c, start) if bs else None
            if bs:
                cogs = {r["date"]: num(r["value"]) for r in (fs or []) if r.get("type") == "CostOfGoodsSold"}
                s["inventory"]["finmind"] = {r["date"]: [num(r["value"]), cogs.get(r["date"])]
                                             for r in bs if r.get("type") == "Inventories" and num(r.get("value"))}
            log.info("  存貨 %s：Yahoo %d 季、FinMind %d 季", c, len(s["inventory"].get("yahoo", {})),
                     len(s["inventory"].get("finmind", {})))

    def build_output(self):
        tol = self.cfg["tolerance"]
        out = {"version": VERSION, "generatedAt": now_tpe().isoformat(timespec="seconds"),
               "deploy": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
               "repo": os.environ.get("GITHUB_REPOSITORY", ""),
               "finmindToken": bool(self.cfg["finmind"].get("token")),
               "schedule": self.cfg["schedule"], "watchlist": self.cfg["watchlist"], "stocks": {},
               "health": self.http.health, "warnings": self.warnings}
        cut_px = (self.today - timedelta(days=31 * (self.cfg["price_months"] + 1))).isoformat()
        for c in self.cfg["watchlist"]:
            s = self.st(c)
            px_srcs = {k: {d: v for d, v in s["prices"].get(k, {}).items() if d >= cut_px} for k in PRICE_PRI if s["prices"].get(k)}
            prices, vp = merge_prices(px_srcs, tol)
            rev_srcs = {k: s["revenue"][k] for k in ("mops", "openapi", "finmind") if s["revenue"].get(k)}
            revenue, vr = merge_revenue(rev_srcs, tol)
            inst_srcs = {k: s["inst"][k] for k in ("official", "finmind") if s["inst"].get(k)}
            inst, vi = merge_inst(inst_srcs, tol)
            inv_srcs = {k: dict(s["inventory"][k]) for k in ("yahoo", "finmind") if s["inventory"].get(k)}
            inventory, vv = merge_inventory(inv_srcs, tol)
            out["stocks"][c] = {"id": c, "name": s.get("name", c), "market": s.get("market"),
                                "prices": prices, "revenue": revenue[-36:], "inst": inst[-self.cfg["inst_days"] * 2:],
                                "inventory": inventory[-8:],
                                "validation": {"price": vp, "revenue": vr, "inst": vi, "inventory": vv}}
            for name, v in (("股價", vp), ("營收", vr), ("法人", vi), ("存貨", vv)):
                if v["X"]:
                    self.warnings.append(f"{c} {name} 有 {v['X']} 筆來源不一致（已採多數決 / 官方優先）")
                if v["total"] and len(v["sources"]) < 2:
                    self.warnings.append(f"{c} {name} 只有單一來源 {list(v['sources'])}，無法互驗")
            log.info("  %s 驗證：股價 一致 %d / 單一 %d / 衝突 %d；營收 %d/%d/%d；法人 %d/%d/%d", c,
                     vp["V"], vp["S"], vp["X"], vr["V"], vr["S"], vr["X"], vi["V"], vi["S"], vi["X"])
        return out

    def run(self):
        t0 = time.time()
        n = now_tpe()
        log.info("=== 台股半導體通路資料抓取 v%s  %s（台北時間）===", VERSION, n.strftime("%Y-%m-%d %H:%M"))
        if n.weekday() < 5 and (n.hour, n.minute) < (19, 30):
            log.info("提醒：目前早於排程時間 19:30，當日法人 / 行情可能尚未完整，下次排程會自動補齊")
        log.info("[1/5] 判斷市場別")
        quotes = self.src.openapi_quotes()
        self.resolve_markets(quotes)
        log.info("[2/5] 股價（官方 + OpenAPI + Yahoo + FinMind）")
        self.step_prices(quotes)
        save_json(DB_PATH, self.db, compact=True)
        log.info("[3/5] 三大法人（官方 + FinMind）")
        self.step_inst()
        save_json(DB_PATH, self.db, compact=True)
        log.info("[4/5] 月營收（MOPS + OpenAPI + FinMind）")
        self.step_revenue()
        log.info("[5/5] 存貨（Yahoo + FinMind）")
        self.step_inventory()
        save_json(DB_PATH, self.db, compact=True)
        out = self.build_output()
        out["runSeconds"] = round(time.time() - t0, 1)
        save_json(os.path.join(DATA_DIR, "dashboard_data.json"), out, compact=True)
        with open(os.path.join(DATA_DIR, "dashboard_data.js"), "w", encoding="utf-8") as f:
            f.write("/* 由 fetch_tw_data.py 自動產生，請勿手動編輯 */\nwindow.DASH_DATA = ")
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
            f.write(";\n")
        for src, h in self.http.health.items():
            log.info("  來源 %-8s 成功 %3d  失敗 %3d  無資料 %3d  %s", src, h["ok"], h["fail"], h["empty"], h["lastError"])
        for w in self.warnings:
            log.warning("  ⚠ %s", w)
        log.info("完成，耗時 %.0f 秒 → data/dashboard_data.js", out["runSeconds"])
        ok = any(out["stocks"][c]["prices"] for c in self.cfg["watchlist"])  # 個別股票失敗只列警示
        return 0 if ok else 1


def download_vendor(http):
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for fn, url in VENDOR_FILES.items():
        path = os.path.join(VENDOR_DIR, fn)
        if os.path.exists(path) and os.path.getsize(path) > 10000:
            continue
        b = http.get(url, "CDN", kind="bytes")
        if b and len(b) > 10000:
            with open(path, "wb") as f:
                f.write(b)
            log.info("  已下載 vendor/%s（%d KB）", fn, len(b) // 1024)
        else:
            log.warning("  無法下載 %s（儀表板將改用線上 CDN）", fn)


def load_config():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_json(CONFIG_PATH, cfg)
    for k, v in DEFAULT_CONFIG.items():  # 補齊新版本欄位
        if isinstance(v, dict):
            cfg[k] = {**v, **cfg.get(k, {})}
        else:
            cfg.setdefault(k, v)
    cfg["watchlist"] = [str(x).strip().upper() for x in cfg["watchlist"] if str(x).strip()]
    # FinMind Token 優先順序：環境變數 FINMIND_TOKEN（GitHub Secret）> config.json
    env_tok = (os.environ.get("FINMIND_TOKEN") or "").strip()
    if env_tok:
        cfg["finmind"]["token"] = env_tok
    return cfg


def setup_logging(verbose):
    os.makedirs(LOG_DIR, exist_ok=True)
    for h in list(log.handlers):  # 同一程序重複呼叫時（Android App）避免重複輸出
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"fetch_{now_tpe():%Y%m%d}.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    # 清除 30 天前的 log
    cutoff = time.time() - 30 * 86400
    for fn in os.listdir(LOG_DIR):
        p = os.path.join(LOG_DIR, fn)
        if fn.startswith("fetch_") and os.path.getmtime(p) < cutoff:
            os.remove(p)


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(description="台股半導體通路儀表板資料抓取")
    ap.add_argument("--add", nargs="+", metavar="代號", help="加入觀察名單")
    ap.add_argument("--remove", nargs="+", metavar="代號", help="移出觀察名單")
    ap.add_argument("--full", action="store_true", help="忽略快取完整重抓")
    ap.add_argument("--no-finmind", action="store_true", help="不使用 FinMind")
    ap.add_argument("--no-yahoo", action="store_true", help="不使用 Yahoo")
    ap.add_argument("--vendor", action="store_true", help="下載圖表函式庫到 vendor/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging(args.verbose)
    cfg = load_config()
    if args.add or args.remove:
        wl = cfg["watchlist"]
        for c in args.add or []:
            if c.upper() not in wl:
                wl.append(c.upper())
        cfg["watchlist"] = [c for c in wl if c not in set(x.upper() for x in (args.remove or []))]
        save_json(CONFIG_PATH, cfg)
        log.info("觀察名單：%s", ", ".join(cfg["watchlist"]))
    if args.no_finmind:
        cfg["finmind"]["enabled"] = False
    if args.no_yahoo:
        cfg["yahoo"]["enabled"] = False

    lock = os.path.join(DATA_DIR, ".lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600:
        log.warning("另一個抓取程序執行中（%s），結束", lock)
        return 2
    open(lock, "w").close()
    try:
        r = Runner(cfg, args)
        if args.vendor or (not os.environ.get("TWSEMI_NO_VENDOR")
                           and not all(os.path.exists(os.path.join(VENDOR_DIR, f)) for f in VENDOR_FILES)):
            log.info("[0] 下載圖表函式庫（僅首次）")
            download_vendor(r.http)
        return r.run()
    except KeyboardInterrupt:
        log.warning("使用者中斷")
        return 130
    except Exception:
        log.exception("未預期錯誤")
        return 1
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
