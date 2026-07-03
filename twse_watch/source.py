"""資料層：抓取證交所(TWSE)與櫃買中心(TPEx)官方資料、市場別偵測與格式解析。

所有端點皆為官方公開資料 / OpenAPI，已於 2026-06 實測可用。若官方改版，
請只需調整本檔的 URL 常數，其餘模組不受影響。
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

# --------------------------------------------------------------------------
# 端點常數（官方來源）
# --------------------------------------------------------------------------
TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_BWIBBU = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU"          # 本益比/殖利率/股價淨值比
TWSE_T187 = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"            # 上市公司基本資料(含已發行股數)
TWSE_NOTE = "https://openapi.twse.com.tw/v1/announcement/notetrans"         # 注意
TWSE_PUNISH = "https://openapi.twse.com.tw/v1/announcement/punish"          # 處置
TWSE_PUNISH_RWD = "https://www.twse.com.tw/rwd/zh/announcement/punish"      # 處置(即時 RWD，優先)

TPEX_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"            # 上櫃當日報價快照
TPEX_PE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"      # 上櫃本益比/淨值比
TPEX_DISPOSAL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"       # 上櫃處置
# 上櫃注意 OpenAPI 路徑官方多次改版，下列為候選；皆失敗時改由規則引擎自行判定
TPEX_ATTENTION_CANDIDATES = [
    "https://www.tpex.org.tw/openapi/v1/tpex_attention_information",
    "https://www.tpex.org.tw/openapi/v1/tpex_attention_stocks",
    "https://www.tpex.org.tw/openapi/v1/tpex_attention",
]
# 上櫃個股歷史日成交（新站，回傳近一個月）
TPEX_HISTORY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"

_HEADERS = {"User-Agent": "Mozilla/5.0 (twse-watch research tool)"}
_TIMEOUT = 25


# --------------------------------------------------------------------------
# 工具：民國年/西元年、中文數字
# --------------------------------------------------------------------------
def roc_to_date(s: str) -> Optional[_dt.date]:
    """支援 '115/06/26'、'1150626'、'115年06月26日' 等民國格式 → date。"""
    s = s.strip()
    m = re.search(r"(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})", s)
    if not m:
        m = re.fullmatch(r"(\d{3})(\d{2})(\d{2})", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return _dt.date(y + 1911, mo, d)
    except ValueError:
        return None


_CJK_NUM = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cjk_to_int(s: str) -> Optional[int]:
    """將 '十二'、'六'、'三十' 等中文數字轉為 int（支援到 99，足夠本用途）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if "十" not in s:
        if len(s) == 1 and s in _CJK_NUM:
            return _CJK_NUM[s]
        # 連續數字如 "九十五" 已含十，這裡處理純位數連寫
        try:
            return int("".join(str(_CJK_NUM[c]) for c in s))
        except KeyError:
            return None
    # 含 "十"
    parts = s.split("十")
    tens = _CJK_NUM.get(parts[0], 1) if parts[0] else 1
    ones = _CJK_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
    return tens * 10 + ones


def _f(x) -> Optional[float]:
    """容錯轉 float：去除千分位逗號、處理 '--'、'X' 等。"""
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "X", "x", "N/A", "null", "－"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------
@dataclass
class DayBar:
    date: _dt.date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume_shares: Optional[float]   # 成交股數
    amount: Optional[float]          # 成交金額(元)
    pe: Optional[float] = None       # 本益比
    pb: Optional[float] = None       # 股價淨值比


@dataclass
class StockData:
    code: str
    name: str
    market: str                       # 'TWSE' | 'TPEX' | 'EMERGING' | 'UNKNOWN'
    bars: list = field(default_factory=list)   # list[DayBar]，由舊到新
    shares_outstanding: Optional[float] = None
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------
# 抓取：TWSE 上市
# --------------------------------------------------------------------------
def _get_json(url: str, params: dict | None = None):
    r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _twse_month(stock_no: str, yyyymmdd: str) -> list:
    """抓 TWSE 某月日成交（STOCK_DAY 一次回傳整月）。"""
    out = []
    try:
        j = _get_json(TWSE_STOCK_DAY, {"date": yyyymmdd, "stockNo": stock_no, "response": "json"})
    except Exception:
        return out
    if j.get("stat") != "OK":
        return out
    pe_map = {}
    try:
        jb = _get_json(TWSE_BWIBBU, {"date": yyyymmdd, "stockNo": stock_no, "response": "json"})
        if jb.get("stat") == "OK":
            for row in jb.get("data", []):
                d = roc_to_date(row[0])
                if d:
                    pe_map[d] = (_f(row[1]), _f(row[3]))  # 本益比, 股價淨值比
    except Exception:
        pass
    for row in j.get("data", []):
        d = roc_to_date(row[0])
        if not d:
            continue
        pe, pb = pe_map.get(d, (None, None))
        out.append(DayBar(
            date=d, volume_shares=_f(row[1]), amount=_f(row[2]),
            open=_f(row[3]), high=_f(row[4]), low=_f(row[5]), close=_f(row[6]),
            pe=pe, pb=pb,
        ))
    return out


def _months_back(n_months: int) -> list[str]:
    today = _dt.date.today()
    res, y, m = [], today.year, today.month
    for _ in range(n_months):
        res.append(f"{y}{m:02d}01")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return res


def fetch_twse(stock_no: str, months: int = 6) -> Optional[StockData]:
    bars: list[DayBar] = []
    for ym in _months_back(months):
        bars.extend(_twse_month(stock_no, ym))
        time.sleep(0.4)  # 禮貌延遲，避免被限流
    if not bars:
        return None
    bars = _dedup_sort(bars)
    name = _twse_name(stock_no)
    sd = StockData(code=stock_no, name=name or stock_no, market="TWSE", bars=bars)
    sd.shares_outstanding = _twse_shares(stock_no)
    if sd.shares_outstanding is None:
        sd.warnings.append("查無已發行股數，週轉率相關款別(第四、十款)無法計算。")
    return sd


_T187_CACHE: dict | None = None


def _t187_index() -> dict:
    global _T187_CACHE
    if _T187_CACHE is None:
        _T187_CACHE = {}
        try:
            for row in _get_json(TWSE_T187):
                _T187_CACHE[row.get("公司代號")] = row
        except Exception:
            pass
    return _T187_CACHE


def _twse_name(code: str) -> Optional[str]:
    row = _t187_index().get(code)
    return row.get("公司簡稱") if row else None


def _twse_shares(code: str) -> Optional[float]:
    row = _t187_index().get(code)
    if not row:
        return None
    s = _f(row.get("已發行普通股數或TDR原股發行股數"))
    if s:
        return s
    # 退而求其次：實收資本額 / 面額
    cap = _f(row.get("實收資本額"))
    par = _f(row.get("普通股每股面額")) or 10.0
    return cap / par if cap else None


# --------------------------------------------------------------------------
# 抓取：TPEx 上櫃（歷史日成交 + 股數退化估算）
# --------------------------------------------------------------------------
def _tpex_month(stock_no: str, roc_ym: str) -> list:
    """roc_ym 形如 '115/06'。新站回傳該月日成交。"""
    out = []
    try:
        j = _get_json(TPEX_HISTORY, {"code": stock_no, "date": roc_ym.replace("/", "") + "01", "id": "", "response": "json"})
    except Exception:
        return out
    rows = j.get("tables", [{}])[0].get("data") if isinstance(j.get("tables"), list) else j.get("data")
    if not rows:
        return out
    for row in rows:
        d = roc_to_date(str(row[0]))
        if not d:
            continue
        # TPEx 欄位：日期,成交仟股,成交仟元,開,高,低,收,漲跌,筆數
        vol = _f(row[1])
        amt = _f(row[2])
        out.append(DayBar(
            date=d,
            volume_shares=vol * 1000 if vol is not None else None,
            amount=amt * 1000 if amt is not None else None,
            open=_f(row[3]), high=_f(row[4]), low=_f(row[5]), close=_f(row[6]),
        ))
    return out


def fetch_tpex(stock_no: str, months: int = 6) -> Optional[StockData]:
    bars: list[DayBar] = []
    today = _dt.date.today()
    y, m = today.year - 1911, today.month
    for _ in range(months):
        bars.extend(_tpex_month(stock_no, f"{y}/{m:02d}"))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        time.sleep(0.4)
    if not bars:
        return None
    bars = _dedup_sort(bars)
    sd = StockData(code=stock_no, name=stock_no, market="TPEX", bars=bars)
    # 上櫃股數來源較分散，這裡標示無法計算週轉率款，避免給出錯誤數字
    sd.warnings.append("上櫃個股流通股數未串接，週轉率相關款別(第四、十款)以區間估算，請以官方為準。")
    # 補本益比/淨值比（僅當日快照）
    try:
        for row in _get_json(TPEX_PE):
            if row.get("SecuritiesCompanyCode") == stock_no:
                if bars:
                    bars[-1].pe = _f(row.get("PriceEarningRatio"))
                    bars[-1].pb = _f(row.get("PriceBookRatio"))
                sd.name = row.get("CompanyName", stock_no)
                break
    except Exception:
        pass
    return sd


# --------------------------------------------------------------------------
# 市場別偵測
# --------------------------------------------------------------------------
def detect_market(stock_no: str) -> str:
    """回傳 'TWSE' / 'TPEX' / 'EMERGING' / 'UNKNOWN'。"""
    if stock_no in _t187_index():
        return "TWSE"
    # 試 TPEx 當日快照清單
    try:
        for row in _get_json(TPEX_QUOTES):
            if row.get("SecuritiesCompanyCode") == stock_no:
                return "TPEX"
    except Exception:
        pass
    # 興櫃判定：興櫃股票多為四碼且不在上市/上櫃清單；此處保守回 UNKNOWN
    return "UNKNOWN"


def fetch_stock(stock_no: str, months: int = 6) -> StockData:
    market = detect_market(stock_no)
    if market == "TWSE":
        sd = fetch_twse(stock_no, months)
        if sd:
            return sd
    if market in ("TPEX", "UNKNOWN"):
        sd = fetch_tpex(stock_no, months)
        if sd:
            return sd
        sd = fetch_twse(stock_no, months)
        if sd:
            return sd
    # 都抓不到
    sd = StockData(code=stock_no, name=stock_no, market=market, bars=[])
    sd.warnings.append(
        "查無此代號的上市/上櫃日成交資料。若為興櫃股票，興櫃採用不同的注意/處置標準"
        "（無漲跌幅限制、標準另訂），本工具尚未支援。")
    return sd


def _dedup_sort(bars: list) -> list:
    seen = {}
    for b in bars:
        seen[b.date] = b
    return [seen[d] for d in sorted(seen)]


# --------------------------------------------------------------------------
# 官方注意/處置公告
# --------------------------------------------------------------------------
@dataclass
class OfficialDisposition:
    code: str
    name: str
    reason: str
    period: str            # 處置期間原字串
    measures: str          # 第一次/第二次處置
    detail: str = ""
    start: Optional[_dt.date] = None
    end: Optional[_dt.date] = None


@dataclass
class OfficialAttention:
    code: str
    name: str
    criteria_text: str     # 例如 '115年6月16日至115年6月29日等九個營業日已有五次'


def _parse_disposition_period(period: str):
    """解析處置期間字串 → (start, end)。
    支援 '115/06/26～115/07/10'、'115年06月26日至115年07月10日' 等格式。
    若無法解析 end，設為 today+90 以確保 ongoing 過濾不遺漏。
    """
    start = end = None
    parts = re.split(r"[~～至\-－]", period)
    if len(parts) >= 2:
        start = roc_to_date(parts[0].strip())
        end = roc_to_date(parts[-1].strip())
    elif len(parts) == 1 and parts[0].strip():
        start = roc_to_date(parts[0].strip())
    # 若 end 解析失敗但有 start，保守設為 today+90，避免遺漏進行中處置
    if start and end is None:
        end = _dt.date.today() + _dt.timedelta(days=90)
    return start, end


def fetch_official_disposition(code: str) -> list:
    res = []
    seen_periods: set = set()

    def _add_twse_row(row):
        period = row.get("DispositionPeriod", "")
        if period in seen_periods:
            return
        seen_periods.add(period)
        start, end = _parse_disposition_period(period)
        res.append(OfficialDisposition(
            code=code, name=row.get("Name", ""),
            reason=row.get("ReasonsOfDisposition", ""),
            period=period, measures=row.get("DispositionMeasures", ""),
            detail=row.get("Detail", ""), start=start, end=end))

    # 主：即時 RWD（更新較快）
    try:
        d = _dt.date.today().strftime("%Y%m%d")
        rwd = requests.get(TWSE_PUNISH_RWD,
                           params={"date": d, "response": "json"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=20).json()
        for row in rwd.get("data", []):
            # RWD data 是 list：[代號, 名稱, 原因, 期間, 措施, ...]
            if len(row) >= 5 and str(row[0]).strip() == code:
                period = str(row[3])
                if period in seen_periods:
                    continue
                seen_periods.add(period)
                start, end = _parse_disposition_period(period)
                res.append(OfficialDisposition(
                    code=code, name=str(row[1]),
                    reason=str(row[2]), period=period,
                    measures=str(row[4]), detail="",
                    start=start, end=end))
    except Exception:
        pass

    # 後備：OpenAPI（有延遲但欄位明確）
    try:
        for row in _get_json(TWSE_PUNISH):
            if row.get("Code") == code:
                _add_twse_row(row)
    except Exception:
        pass

    # TPEx
    try:
        for row in _get_json(TPEX_DISPOSAL):
            if row.get("SecuritiesCompanyCode") == code:
                period = row.get("DispositionPeriod", "")
                if period in seen_periods:
                    continue
                seen_periods.add(period)
                start, end = _parse_disposition_period(period)
                res.append(OfficialDisposition(
                    code=code, name=row.get("CompanyName", ""),
                    reason=row.get("DispositionReasons", ""),
                    period=period, measures=row.get("DispositionMeasures", "上櫃處置"),
                    detail=row.get("DisposalCondition", ""), start=start, end=end))
    except Exception:
        pass
    return res


def fetch_official_attention(code: str) -> Optional[OfficialAttention]:
    try:
        for row in _get_json(TWSE_NOTE):
            if row.get("Code") == code:
                return OfficialAttention(
                    code=code, name=row.get("Name", ""),
                    criteria_text=row.get("RecentlyMetAttentionSecuritiesCriteria", ""))
    except Exception:
        pass
    for url in TPEX_ATTENTION_CANDIDATES:
        try:
            data = _get_json(url)
            for row in data:
                if row.get("SecuritiesCompanyCode") == code or row.get("Code") == code:
                    txt = (row.get("Description") or row.get("Note")
                           or row.get("AttentionReasons") or "")
                    return OfficialAttention(code=code,
                                             name=row.get("CompanyName", ""),
                                             criteria_text=txt)
        except Exception:
            continue
    return None
