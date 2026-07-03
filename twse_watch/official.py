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

from .source import cjk_to_int

RWD_NOTE = "https://www.twse.com.tw/rwd/zh/announcement/notetrans"
OPENAPI_NOTE = "https://openapi.twse.com.tw/v1/announcement/notetrans"
_H = {"User-Agent": "Mozilla/5.0 (twse-watch)"}
_N = r"([零一二兩三四五六七八九十\d]+)"


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
