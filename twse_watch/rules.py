"""注意標準規則引擎。

依據「臺灣證券交易所公布或通知注意交易資訊暨處置作業要點第四條」及其
「異常標準之詳細數據及除外情形」(113/12/31 版) 將各款公式化。櫃買中心
(TPEx) 要點之數據與 TWSE 幾乎一致，故共用同一組門檻。

重要近似（已在輸出標示）：
* 「與全體有價證券平均值之差幅」以 market_avg 代理（預設 0，即近似為個股
  自身漲跌幅）；6 日市場平均通常接近 0，故絕對門檻為主要拘束條件。
* 「與同類有價證券(類股)平均值之差幅」因需全類股資料，本工具不檢核，視為
  條件成立。這會使結果偏「容易觸發」(保守示警)，請以官方公告為準。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .indicators import Metrics


@dataclass
class Thresholds:
    # 第1款
    k1_cum6_a: float = 30.0          # 6日累積漲跌 > 30%
    k1_cum6_b: float = 25.0          # 或 > 25% 且起迄價差≥50元
    k1_diff: float = 20.0
    k1_span_price: float = 50.0
    k1_min_close: float = 5.0        # 收盤<5元不適用
    # 第2款 起迄兩日(長區間)
    k2_30: float = 100.0
    k2_30_diff: float = 85.0
    k2_60: float = 130.0
    k2_60_diff: float = 110.0
    k2_90: float = 160.0
    k2_90_diff: float = 135.0
    # 第3款 量放大
    k3_cum6: float = 25.0
    k3_diff: float = 20.0
    k3_amp: float = 5.0
    k3_min_turnover: float = 0.1     # 週轉率<0.1%不適用
    k3_min_vol_lots: float = 500     # 量<500交易單位(=500張)不適用
    # 第4款 週轉率
    k4_cum6: float = 25.0
    k4_diff: float = 20.0
    k4_turnover: float = 10.0
    # 第10款(累積週轉率)
    k10_cum6_turnover: float = 50.0
    k10_turnover: float = 10.0
    k10_min_amount: float = 5e8      # 成交金額<5億不適用
    # 第9款(量能放大)
    k9_avg6_amp: float = 5.0
    k9_day_amp: float = 5.0
    k9_min_amount: float = 3e7
    # 第11款 起迄兩日價差
    k11_price_diff: float = 100.0
    k11_tier_base: float = 500.0
    k11_tier_add: float = 25.0
    # 共同
    pe_high: float = 60.0            # 本益比負或≥60 → 不檢核類股(視為成立)

LOTS_TO_SHARES = 1000                 # 1 交易單位 = 1000 股


@dataclass
class CriterionResult:
    key: str                  # 'k1'...
    no: str                   # '第一款'
    name: str                 # 簡述
    triggered: bool
    status: str               # 'triggered' | 'not_triggered' | 'insufficient_data' | 'excluded'
    detail: str = ""
    metrics: dict = field(default_factory=dict)


def _diff_ok(value: Optional[float], market_avg: float, need: float) -> bool:
    if value is None:
        return False
    return abs(value - market_avg) >= need


def evaluate(m: Metrics, t: Thresholds = Thresholds(), market_avg: float = 0.0,
             shares_outstanding: Optional[float] = None) -> list:
    """回傳 13 款 CriterionResult。market_avg 為 6 日市場平均漲跌% 之代理。"""
    res: list[CriterionResult] = []

    # ---- 第1款：6日累積漲跌幅 ----
    if m.cum6_return_pct is None or m.close is None:
        res.append(CriterionResult("k1", "第一款", "近6日累積漲跌幅異常", False,
                                    "insufficient_data", "缺少近6日收盤資料"))
    elif m.close < t.k1_min_close:
        res.append(CriterionResult("k1", "第一款", "近6日累積漲跌幅異常", False,
                                    "excluded", f"收盤價{m.close:.2f}<5元，不適用"))
    else:
        cum = m.cum6_return_pct
        a = abs(cum) > t.k1_cum6_a and _diff_ok(cum, market_avg, t.k1_diff)
        span_ok = (m.span2_diff_in6 is not None and abs(m.span2_diff_in6) >= t.k1_span_price)
        b = abs(cum) > t.k1_cum6_b and _diff_ok(cum, market_avg, t.k1_diff) and span_ok
        trig = a or b
        which = "情形一(>30%)" if a else ("情形二(>25%且起迄價差≥50元)" if b else "")
        res.append(CriterionResult(
            "k1", "第一款", "近6日累積漲跌幅異常", trig,
            "triggered" if trig else "not_triggered",
            f"近6日累積漲跌 {cum:+.2f}%（門檻>±30%；或>±25%且起迄價差≥50元）{which}",
            {"cum6_return_pct": round(cum, 2),
             "span2_diff": round(m.span2_diff_in6, 2) if m.span2_diff_in6 is not None else None}))

    # ---- 第2款：起迄兩日(30/60/90日)漲跌幅 ----
    def k2_one(ret, base_thr, diff_thr, win):
        if ret is None:
            return None
        up = ret > base_thr and _diff_ok(ret, market_avg, diff_thr) and (
            m.close is not None and m.open_ref is not None and m.close > m.open_ref)
        dn = ret < -base_thr and _diff_ok(ret, market_avg, diff_thr) and (
            m.close is not None and m.open_ref is not None and m.close < m.open_ref)
        return (up or dn, ret, win, base_thr)

    k2_hits = [x for x in (
        k2_one(m.ret30_pct, t.k2_30, t.k2_30_diff, 30),
        k2_one(m.ret60_pct, t.k2_60, t.k2_60_diff, 60),
        k2_one(m.ret90_pct, t.k2_90, t.k2_90_diff, 90)) if x is not None]
    if not k2_hits:
        res.append(CriterionResult("k2", "第二款", "近30/60/90日起迄漲跌幅異常", False,
                                    "insufficient_data", "長區間資料不足"))
    else:
        trig = any(h[0] for h in k2_hits)
        parts = [f"{h[2]}日起迄 {h[1]:+.1f}%(門檻±{h[3]:.0f}%)" for h in k2_hits]
        res.append(CriterionResult("k2", "第二款", "近30/60/90日起迄漲跌幅異常", trig,
                                    "triggered" if trig else "not_triggered",
                                    "；".join(parts),
                                    {"ret30": _r(m.ret30_pct), "ret60": _r(m.ret60_pct),
                                     "ret90": _r(m.ret90_pct)}))

    # ---- 第3款：6日漲跌>25% + 當日量放大5倍 ----
    cum_ok3 = (m.cum6_return_pct is not None and abs(m.cum6_return_pct) > t.k3_cum6
               and _diff_ok(m.cum6_return_pct, market_avg, t.k3_diff))
    if m.vol_amp is None or m.cum6_return_pct is None:
        res.append(CriterionResult("k3", "第三款", "近6日漲跌異常且當日爆量", False,
                                    "insufficient_data", "缺少量能或漲跌資料"))
    else:
        excluded = ((m.turnover_pct is not None and m.turnover_pct < t.k3_min_turnover)
                    or (m.volume_shares is not None and m.volume_shares < t.k3_min_vol_lots * LOTS_TO_SHARES))
        trig = cum_ok3 and (m.vol_amp >= t.k3_amp) and not excluded
        st = "excluded" if excluded and not trig else ("triggered" if trig else "not_triggered")
        res.append(CriterionResult("k3", "第三款", "近6日漲跌異常且當日爆量", trig, st,
                                    f"近6日漲跌 {m.cum6_return_pct:+.2f}%、當日量為60日均量 {m.vol_amp:.2f} 倍"
                                    f"（門檻 漲跌>±25% 且 量≥5倍）",
                                    {"vol_amp": _r(m.vol_amp), "cum6": _r(m.cum6_return_pct)}))

    # ---- 第4款：6日漲跌>25% + 當日週轉率≥10% ----
    if m.turnover_pct is None or m.cum6_return_pct is None:
        res.append(CriterionResult("k4", "第四款", "近6日漲跌異常且週轉率過高", False,
                                    "insufficient_data", "缺少週轉率(需流通股數)或漲跌資料"))
    else:
        cum_ok4 = abs(m.cum6_return_pct) > t.k4_cum6 and _diff_ok(m.cum6_return_pct, market_avg, t.k4_diff)
        trig = cum_ok4 and m.turnover_pct >= t.k4_turnover
        res.append(CriterionResult("k4", "第四款", "近6日漲跌異常且週轉率過高", trig,
                                    "triggered" if trig else "not_triggered",
                                    f"近6日漲跌 {m.cum6_return_pct:+.2f}%、當日週轉率 {m.turnover_pct:.2f}%"
                                    f"（門檻 漲跌>±25% 且 週轉率≥10%）",
                                    {"turnover_pct": _r(m.turnover_pct), "cum6": _r(m.cum6_return_pct)}))

    # ---- 第9款：量能放大(6日均量及當日量較60日均量放大5倍) ----
    if m.avg6_vol_amp is None or m.vol_amp is None:
        res.append(CriterionResult("k9", "第九款", "近一段期間量能明顯放大", False,
                                    "insufficient_data", "量能資料不足"))
    else:
        excluded9 = (m.amount is not None and m.amount < t.k9_min_amount)
        trig = (m.avg6_vol_amp >= t.k9_avg6_amp and m.vol_amp >= t.k9_day_amp) and not excluded9
        st = "excluded" if excluded9 and not trig else ("triggered" if trig else "not_triggered")
        res.append(CriterionResult("k9", "第九款", "近一段期間量能明顯放大", trig, st,
                                    f"6日均量為60日均量 {m.avg6_vol_amp:.2f} 倍、當日量 {m.vol_amp:.2f} 倍"
                                    f"（門檻 皆≥5倍）",
                                    {"avg6_vol_amp": _r(m.avg6_vol_amp), "vol_amp": _r(m.vol_amp)}))

    # ---- 第10款：累積週轉率 ----
    if m.cum6_turnover_pct is None or m.turnover_pct is None:
        res.append(CriterionResult("k10", "第十款", "近6日累積週轉率明顯過高", False,
                                    "insufficient_data", "缺少週轉率(需流通股數)"))
    else:
        excluded10 = (m.amount is not None and m.amount < t.k10_min_amount)
        trig = (m.cum6_turnover_pct > t.k10_cum6_turnover and m.turnover_pct >= t.k10_turnover) and not excluded10
        st = "excluded" if excluded10 and not trig else ("triggered" if trig else "not_triggered")
        res.append(CriterionResult("k10", "第十款", "近6日累積週轉率明顯過高", trig, st,
                                    f"近6日累積週轉率 {m.cum6_turnover_pct:.2f}%、當日 {m.turnover_pct:.2f}%"
                                    f"（門檻 累積>50% 且 當日≥10%）",
                                    {"cum6_turnover_pct": _r(m.cum6_turnover_pct)}))

    # ---- 第11款：起迄兩日收盤價差 ----
    if m.span2_diff_in6 is None or m.close is None:
        res.append(CriterionResult("k11", "第十一款", "近6日起迄兩日收盤價差異常", False,
                                    "insufficient_data", "缺少收盤資料"))
    else:
        thr = t.k11_price_diff
        if m.close >= t.k11_tier_base:
            thr += (int(m.close // t.k11_tier_base)) * t.k11_tier_add
        up = m.span2_diff_in6 >= thr and bool(m.is6_high)
        dn = (-m.span2_diff_in6) >= thr and bool(m.is6_low)
        trig = up or dn
        res.append(CriterionResult("k11", "第十一款", "近6日起迄兩日收盤價差異常", trig,
                                    "triggered" if trig else "not_triggered",
                                    f"近6日起迄收盤價差 {m.span2_diff_in6:+.2f} 元（門檻 ±{thr:.0f} 元，且須為近6日最高/最低收盤）",
                                    {"span2_diff": _r(m.span2_diff_in6), "threshold": thr}))

    # ---- 其餘需額外資料來源的款別：標示「需額外資料」----
    for key, no, name, note in [
        ("k5", "第五款", "單一券商成交占比過高", "需券商分公司買賣明細"),
        ("k6", "第六款", "本益比/淨值比異常且週轉率高", "需全市場加權本益比/淨值比平均"),
        ("k7", "第七款", "近6日漲跌異常且券資比放大", "需融資融券明細(MI_MARGN)"),
        ("k8", "第八款", "TDR 溢折價異常", "僅適用台灣存託憑證"),
        ("k12", "第十二款", "借券賣出占比過高", "需借券賣出明細"),
        ("k13", "第十三款", "當沖比過高", "需當日沖銷明細"),
    ]:
        res.append(CriterionResult(key, no, name, False, "insufficient_data",
                                   f"{note}（本工具未串接，請參官方公告）"))
    return res


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else x
