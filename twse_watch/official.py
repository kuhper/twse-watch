"""官方注意累計次數（可靠來源）。

修正兩個 bug：
1. 改用即時的 RWD 端點 rwd/zh/announcement/notetrans（OpenAPI 版更新有延遲），
   OpenAPI 版作為後備。
2. 補上「連續N次」寫法的解析（原本只認「已有N次／連續N個營業日」）。

官方文字兩種格式：
  「115年7月1日至115年7月2日連續二次」            → 連續、count=2
  「115年6月22日至115年7月2日等九個營業日已有五次」 → window=9、count=5
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

import requests

from .source import cjk_to_int, roc_to_date

RWD_NOTE = "https://www.twse.com.tw/rwd/zh/announcement/notetrans"
OPENAPI_NOTE = "https://openapi.twse.com.tw/v1/announcement/notetrans"
# 個股注意歷史（含累計次數與各款文字，即截圖那個查詢頁的後端）
NOTICE_HISTORY = "https://www.twse.com.tw/rwd/zh/announcement/notice"
_H = {"User-Agent": "Mozilla/5.0 (twse-watch)"}
_N = r"([零一二兩三四五六七八九十\d]+)"

# 依「注意交易資訊暨處置作業要點」，第九款至第十三款單獨觸發之注意
# 不計入處置累計（官方公告以紅字標示）。以下為「計入處置」的款別。
DISPO_COUNTING_CLAUSES = set(range(1, 9))  # 第 1~8 款計入處置


def _clause_numbers(text: str):
    """從注意文字擷取所有『第X款』的款號（int list）。"""
    out = []
    for m in re.findall(r"第(" + _N.strip("()") + r")款", text):
        n = cjk_to_int(m)
        if n:
            out.append(n)
    return sorted(set(out))


def fetch_attention_history(code: str, days: int = 45) -> Optional[dict]:
    """抓個股近 `days` 日的官方注意歷史。

    回傳 {code, name, entries:[{date, date_str, text, clauses:[int],
    counts_toward_dispo:bool}, ...]}，entries 由舊到新排序。
    counts_toward_dispo = 當日觸發款別含第 1~8 款（即計入處置累計）。
    """
    today = _dt.date.today()
    start = (today - _dt.timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    try:
        j = requests.get(NOTICE_HISTORY,
                         params={"startDate": start, "endDate": end,
                                 "stockNo": code, "response": "json"},
                         headers=_H, timeout=25).json()
    except Exception:
        return None
    rows = [r for r in j.get("data", [])
            if len(r) >= 6 and str(r[1]).strip() == code]
    if not rows:
        return None
    # fields: 編號,證券代號,證券名稱,累計次數,注意交易資訊,日期,收盤價,本益比
    entries = []
    for r in rows:
        text = str(r[4])
        clauses = _clause_numbers(text)
        entries.append({
            "date": roc_to_date(str(r[5])),
            "date_str": str(r[5]),
            "text": re.sub(r"<[^>]+>", "", text),  # 去掉 <font> 標籤
            "clauses": clauses,
            "counts_toward_dispo": any(c in DISPO_COUNTING_CLAUSES for c in clauses),
        })
    entries.sort(key=lambda e: e["date"] or _dt.date.min)
    return {"code": code, "name": str(rows[0][2]), "entries": entries}


def disposition_progress(entries: list, trading_dates: list) -> dict:
    """依官方注意歷史與交易日曆，計算距各處置門檻的進度。

    只計入 counts_toward_dispo=True 的注意日（排除純 9-13 款）。
    trading_dates：由舊到新的交易日 date list（來自個股日 K）。
    """
    dispo_days = {e["date"] for e in entries if e["counts_toward_dispo"] and e["date"]}
    all_att_days = {e["date"] for e in entries if e["date"]}
    if not trading_dates:
        return {"available": False}
    td = sorted(trading_dates)

    def count_in_window(day_set, n):
        window = td[-n:] if n <= len(td) else td
        return sum(1 for d in window if d in day_set)

    # 末端連續處置注意日（以交易日曆為準）
    consec = 0
    for d in reversed(td):
        if d in dispo_days:
            consec += 1
        else:
            break

    rules = {
        "連續3個營業日": max(3 - consec, 0),
        "最近10日內6次": max(6 - count_in_window(dispo_days, 10), 0),
        "最近30日內12次": max(12 - count_in_window(dispo_days, 30), 0),
    }
    binding, remaining = min(rules.items(), key=lambda kv: kv[1])
    return {
        "available": True,
        "official_cum_attention": len(all_att_days),      # 全部款別注意天數
        "dispo_counting_days": len(dispo_days),           # 計入處置的注意天數
        "consecutive_dispo_days": consec,
        "count_10d": count_in_window(dispo_days, 10),
        "count_30d": count_in_window(dispo_days, 30),
        "distance": rules,
        "binding_rule": binding,
        "remaining": remaining,
        "disposed": remaining <= 0,
    }


def parse_attention_text(text: str):
    """回傳 (count, window, consecutive)。"""
    if not text:
        return None, None, False
    count = window = None
    consecutive = "連續" in text
    m = re.search(r"連續" + _N + r"次", text)
    if m:
        count = cjk_to_int(m.group(1))
        window = window or count
    m = re.search(r"已有" + _N + r"次", text)
    if m:
        count = cjk_to_int(m.group(1))
    m = re.search(r"等" + _N + r"個營業日", text)
    if m:
        window = cjk_to_int(m.group(1))
    m = re.search(r"連續" + _N + r"個營業日", text)
    if m and count is None:
        count = cjk_to_int(m.group(1))
        window = window or count
    return count, window, consecutive


def fetch_official_attention(code: str) -> Optional[dict]:
    text = None
    # 主：即時 RWD
    try:
        d = _dt.date.today().strftime("%Y%m%d")
        j = requests.get(RWD_NOTE, params={"date": d, "response": "json"},
                         headers=_H, timeout=20).json()
        for r in j.get("data", []):
            if str(r[1]).strip() == code:
                text = str(r[3])
                break
    except Exception:
        pass
    # 後備：OpenAPI
    if text is None:
        try:
            for r in requests.get(OPENAPI_NOTE, headers=_H, timeout=20).json():
                if r.get("Code") == code:
                    text = r.get("RecentlyMetAttentionSecuritiesCriteria") or ""
                    break
        except Exception:
            pass
    if not text:
        return None
    count, window, consecutive = parse_attention_text(text)
    return {"code": code, "text": text, "count": count,
            "window": window, "consecutive": consecutive}


def distance_to_disposition(count: Optional[int], window: Optional[int],
                            consecutive: bool) -> dict:
    """依官方累計次數，算距各處置門檻還差幾次（僅計第4條1~8款；此為官方累計，已含之）。"""
    d = {}
    if count is None:
        return d
    if consecutive:
        d["連續3個營業日"] = max(3 - count, 0)
    if window is None or window <= 10:
        d["最近10日內6次"] = max(6 - count, 0)
    if window is None or window <= 30:
        d["最近30日內12次"] = max(12 - count, 0)
    return d
