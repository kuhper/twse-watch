"""當沖（當日沖銷）資料串接 + 第十三款（當沖比過高）判定。

資料源（實測）：TWSE 上市每日當日沖銷交易標的及統計
  https://www.twse.com.tw/exchangeReport/TWTB4U?date=YYYYMMDD&selectType=All&response=json
  逐檔欄位『當日沖銷交易成交股數』。

第十三款（要點第4條第1項第13款；詳細數據第十四條）— 於『當日之前一個營業日』判定，
本工具以最新一個營業日 D 為基準：
  一、最近6營業日(到 D) 當沖量占總量比率 > 60%。
  二、D 當日 當沖量占該日總量比率 > 60%。
  除外：D 之週轉率≤5% / 成交金額≤5億 / 當沖量≤5000交易單位(=5,000,000股) → 不適用。

校準（凱美 2375，2026/6/29）：當日 69.44%（官方69.43%）、近6日 60.13%（官方60.13%）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

import requests

TWTB4U = "https://www.twse.com.tw/exchangeReport/TWTB4U"
_H = {"User-Agent": "Mozilla/5.0 (twse-watch)"}
_CACHE: dict = {}   # (yyyymmdd) -> {code: 當沖股數}
LOTS = 1000


def _day_map(yyyymmdd: str) -> dict:
    if yyyymmdd in _CACHE:
        return _CACHE[yyyymmdd]
    out = {}
    try:
        j = requests.get(TWTB4U, params={"date": yyyymmdd, "selectType": "All",
                                         "response": "json"}, headers=_H, timeout=25).json()
        for t in j.get("tables", []):
            if any("證券代號" in f for f in t.get("fields", [])):
                for row in t.get("data", []):
                    code = row[0].strip()
                    try:
                        out[code] = float(str(row[3]).replace(",", ""))
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass
    _CACHE[yyyymmdd] = out
    return out


def daytrade_shares(code: str, d: _dt.date) -> Optional[float]:
    return _day_map(d.strftime("%Y%m%d")).get(code)


def evaluate_k13(bars, code: str, shares: Optional[float]) -> dict:
    """以最新一個營業日 D 判定第十三款。回傳與其他款一致的結果 dict。"""
    res = {"key": "k13", "no": "第十三款", "name": "當沖比過高",
           "triggered": False, "status": "insufficient_data", "detail": "", "metrics": {}}
    win = [b for b in bars[-6:] if b.close is not None and b.volume_shares]
    if len(win) < 6:
        res["detail"] = "近6營業日資料不足"
        return res
    D = win[-1]
    dt_shares = {}
    for b in win:
        s = daytrade_shares(code, b.date)
        if s is None:
            res["detail"] = "查無 %s 當沖資料（非上市或當日無資料）" % b.date
            return res
        dt_shares[b.date] = s
    day_ratio = dt_shares[D.date] / D.volume_shares * 100 if D.volume_shares else None
    sum_dt = sum(dt_shares.values())
    sum_vol = sum(b.volume_shares for b in win)
    cum6_ratio = sum_dt / sum_vol * 100 if sum_vol else None

    # 除外
    excl = []
    if shares and D.volume_shares and (D.volume_shares / shares * 100) <= 5:
        excl.append("週轉率≤5%")
    if D.amount is not None and D.amount < 5e8:
        excl.append("成交金額<5億")
    if dt_shares[D.date] <= 5000 * LOTS:
        excl.append("當沖量<5000張")

    trig = (day_ratio is not None and cum6_ratio is not None
            and day_ratio > 60 and cum6_ratio > 60 and not excl)
    res["triggered"] = trig
    res["status"] = "triggered" if trig else ("excluded" if excl else "not_triggered")
    res["detail"] = ("基準日 %s：當日當沖比 %.2f%%、近6日當沖比 %.2f%%（門檻皆>60%%）%s"
                     % (D.date, day_ratio or 0, cum6_ratio or 0,
                        ("；除外：" + "、".join(excl)) if excl else ""))
    res["metrics"] = {"as_of": D.date.isoformat(),
                      "day_ratio": round(day_ratio, 2) if day_ratio else None,
                      "cum6_ratio": round(cum6_ratio, 2) if cum6_ratio else None,
                      "daytrade_shares": dt_shares[D.date]}
    return res


def consecutive_k13_days(bars, code: str, shares: Optional[float], lookback: int = 4) -> int:
    """從最新日往回，連續達第十三款的營業日數（供『連續3日』處置投影）。"""
    n = 0
    for end in range(len(bars), len(bars) - lookback, -1):
        sub = bars[:end]
        if len(sub) < 6:
            break
        r = evaluate_k13(sub, code, shares)
        if r["triggered"]:
            n += 1
        else:
            break
    return n
