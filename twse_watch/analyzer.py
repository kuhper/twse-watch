"""修正版三階段引擎（analyzer）。

修正重點（依力麗 1444 實例校準）：
1. 「累積漲跌百分比」＝最近 N 個營業日『每日漲跌幅的算術加總』，
   而非頭尾收盤相除（幾何）。力麗 6/23~6/30 六日單日漲跌幅相加 = 25.71%
   ≈ 官方公告 25.69%；幾何法會高估為 27.8%。
2. 門檻改為『市場別』：上市(TWSE)款三= 25%，上櫃(TPEx)款三= 27%
   （依各自「異常標準詳細數據」）。
3. 第三階反推改為精確的『滾動視窗』：次一營業日的 6 日視窗 = 丟掉最舊一日、
   納入明日，因此直接反推出『明日單日漲跌幅臨界值』與對應臨界收盤價。

法規鏈：官方公告(第4條各款) → 處置認定(第6條：連續3日/5日內5次/10日內6次/
30日內12次發布交易資訊) → 反推次日臨界值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import source as src
from .dispo import assess

LOTS = 1000          # 1 交易單位 = 1000 股
PRICE_LIMIT = 0.10   # 上市櫃普通股單日漲跌幅限制


# --------------------------------------------------------------------------
# 市場別門檻
# --------------------------------------------------------------------------
@dataclass
class Thresholds:
    k1_cum6_a: float = 30.0      # 款1 情形一：6日累積漲跌 > 30%
    k1_cum6_b: float = 25.0      # 款1 情形二：> 25% 且起迄價差≥50元
    k1_span_price: float = 50.0
    k1_min_close: float = 5.0
    k1_diff: float = 20.0
    k2_30: float = 100.0
    k2_60: float = 130.0
    k2_90: float = 160.0
    k3_cum6: float = 25.0        # 款3：6日累積漲跌（TWSE 25 / TPEx 27）
    k3_amp: float = 5.0
    k3_diff: float = 20.0
    k3_min_turnover: float = 0.1
    k3_min_vol_lots: float = 500
    k4_cum6: float = 25.0        # 款4
    k4_turnover: float = 10.0
    k9_amp: float = 5.0
    k9_min_amount: float = 3e7
    k10_cum6_turnover: float = 50.0
    k10_turnover: float = 10.0
    k10_min_amount: float = 5e8
    k11_price_diff: float = 100.0
    k11_tier_base: float = 500.0
    k11_tier_add: float = 25.0


def thresholds_for(market: str) -> Thresholds:
    t = Thresholds()
    if market == "TPEX":          # 櫃買中心「詳細數據」：款三為 27%
        t.k3_cum6 = 27.0
        t.k4_cum6 = 27.0          # 上櫃對應款別之累積門檻亦較上市高，保守同步
    return t


# --------------------------------------------------------------------------
# 指標（累積＝每日漲跌幅算術加總）
# --------------------------------------------------------------------------
@dataclass
class Metrics:
    close: Optional[float] = None
    daily_returns: list = field(default_factory=list)   # 每日漲跌幅% (對齊 bars[1:])
    cum6_sum: Optional[float] = None                    # 近6日累積漲跌% (算術加總)
    oldest6_return: Optional[float] = None              # 6日視窗中最舊一日的漲跌%
    span6_diff: Optional[float] = None                  # 近6日起迄收盤價差(元)=close - 視窗首日收盤
    base6_close: Optional[float] = None                 # 視窗首日收盤
    is6_high: Optional[bool] = None
    is6_low: Optional[bool] = None
    ret30: Optional[float] = None                       # 款2 起迄(端點)
    ret60: Optional[float] = None
    ret90: Optional[float] = None
    turnover_pct: Optional[float] = None
    cum6_turnover_pct: Optional[float] = None
    vol_amp: Optional[float] = None
    avg6_vol_amp: Optional[float] = None
    avg60_vol: Optional[float] = None
    amount: Optional[float] = None
    volume_shares: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None


def _endpoint_ret(bars, win):
    if len(bars) >= win and bars[-win].close and bars[-1].close:
        return (bars[-1].close / bars[-win].close - 1) * 100
    return None


def compute_metrics(bars, shares) -> Metrics:
    m = Metrics()
    closes = [b.close for b in bars]
    if not bars or closes[-1] is None:
        return m
    cur = bars[-1]
    m.close, m.amount, m.volume_shares = cur.close, cur.amount, cur.volume_shares
    m.pe, m.pb = cur.pe, cur.pb

    # 每日漲跌幅%
    dr = []
    for i in range(1, len(bars)):
        c0, c1 = closes[i - 1], closes[i]
        dr.append((c1 / c0 - 1) * 100 if (c0 and c1) else None)
    m.daily_returns = dr

    last6 = [x for x in dr[-6:] if x is not None]
    if len(last6) == len(dr[-6:]) and dr[-6:]:
        m.cum6_sum = sum(dr[-6:])
        m.oldest6_return = dr[-6]

    # 近6日視窗首日收盤＝bars[-6]；起迄價差
    if len(bars) >= 6 and bars[-6].close:
        m.base6_close = bars[-6].close
        m.span6_diff = m.close - bars[-6].close
    win6_closes = [b.close for b in bars[-6:] if b.close is not None]
    if win6_closes:
        m.is6_high = m.close >= max(win6_closes)
        m.is6_low = m.close <= min(win6_closes)

    # 款2 端點
    m.ret30, m.ret60, m.ret90 = _endpoint_ret(bars, 30), _endpoint_ret(bars, 60), _endpoint_ret(bars, 90)

    # 週轉率
    if shares and cur.volume_shares:
        m.turnover_pct = cur.volume_shares / shares * 100
        tw = [b.volume_shares for b in bars[-6:]]
        if all(v is not None for v in tw):
            m.cum6_turnover_pct = sum(v / shares * 100 for v in tw)

    # 量能
    vols = [b.volume_shares for b in bars[-60:] if b.volume_shares is not None]
    if len(vols) >= 5 and cur.volume_shares:
        m.avg60_vol = sum(vols) / len(vols)
        if m.avg60_vol > 0:
            m.vol_amp = cur.volume_shares / m.avg60_vol
            v6 = [b.volume_shares for b in bars[-6:] if b.volume_shares is not None]
            if v6:
                m.avg6_vol_amp = (sum(v6) / len(v6)) / m.avg60_vol
    return m


# --------------------------------------------------------------------------
# 規則評估
# --------------------------------------------------------------------------
def _crit(no, name, trig, status, detail, metrics=None):
    return {"key": no, "no": name[0], "name": name[1], "triggered": trig,
            "status": status, "detail": detail, "metrics": metrics or {}}


def evaluate(m: Metrics, t: Thresholds, market_avg: float = 0.0) -> list:
    out = []

    # 款1
    if m.cum6_sum is None or m.close is None:
        out.append(_crit("k1", ("第一款", "近6日累積漲跌幅異常"), False, "insufficient_data", "近6日資料不足"))
    elif m.close < t.k1_min_close:
        out.append(_crit("k1", ("第一款", "近6日累積漲跌幅異常"), False, "excluded",
                         "收盤價 %.2f<5元，不適用" % m.close))
    else:
        cum = m.cum6_sum
        diff = abs(cum - market_avg) >= t.k1_diff
        a = abs(cum) > t.k1_cum6_a and diff
        span_ok = m.span6_diff is not None and abs(m.span6_diff) >= t.k1_span_price
        b = abs(cum) > t.k1_cum6_b and diff and span_ok
        trig = a or b
        out.append(_crit("k1", ("第一款", "近6日累積漲跌幅異常"), trig,
                         "triggered" if trig else "not_triggered",
                         "近6日累積漲跌(每日加總) %+.2f%%（門檻>±30%%；或>±25%%且起迄價差≥50元）" % cum,
                         {"cum6_sum": round(cum, 2)}))

    # 款2 起迄端點
    hits = []
    for ret, thr, win in ((m.ret30, t.k2_30, 30), (m.ret60, t.k2_60, 60), (m.ret90, t.k2_90, 90)):
        if ret is not None:
            hits.append((abs(ret) > thr, ret, win, thr))
    if not hits:
        out.append(_crit("k2", ("第二款", "近30/60/90日起迄漲跌幅異常"), False, "insufficient_data", "長區間資料不足"))
    else:
        trig = any(h[0] for h in hits)
        out.append(_crit("k2", ("第二款", "近30/60/90日起迄漲跌幅異常"), trig,
                         "triggered" if trig else "not_triggered",
                         "；".join("%d日起迄 %+.1f%%(門檻±%.0f%%)" % (h[2], h[1], h[3]) for h in hits)))

    # 款3 量放大
    if m.cum6_sum is None or m.vol_amp is None:
        out.append(_crit("k3", ("第三款", "近6日漲跌異常且當日爆量"), False, "insufficient_data", "缺漲跌或量能資料"))
    else:
        cum_ok = abs(m.cum6_sum) > t.k3_cum6 and abs(m.cum6_sum - market_avg) >= t.k3_diff
        excl = ((m.turnover_pct is not None and m.turnover_pct < t.k3_min_turnover)
                or (m.volume_shares is not None and m.volume_shares < t.k3_min_vol_lots * LOTS))
        trig = cum_ok and m.vol_amp >= t.k3_amp and not excl
        st = "excluded" if excl and not trig else ("triggered" if trig else "not_triggered")
        out.append(_crit("k3", ("第三款", "近6日漲跌異常且當日爆量"), trig, st,
                         "近6日累積 %+.2f%%（門檻>±%.0f%%）、當日量為60日均量 %.2f 倍（門檻≥5倍）"
                         % (m.cum6_sum, t.k3_cum6, m.vol_amp),
                         {"cum6_sum": round(m.cum6_sum, 2), "vol_amp": round(m.vol_amp, 2)}))

    # 款4 週轉率
    if m.turnover_pct is None or m.cum6_sum is None:
        out.append(_crit("k4", ("第四款", "近6日漲跌異常且週轉率過高"), False, "insufficient_data",
                         "缺週轉率(需流通股數)或漲跌資料"))
    else:
        cum_ok = abs(m.cum6_sum) > t.k4_cum6 and abs(m.cum6_sum - market_avg) >= t.k1_diff
        trig = cum_ok and m.turnover_pct >= t.k4_turnover
        out.append(_crit("k4", ("第四款", "近6日漲跌異常且週轉率過高"), trig,
                         "triggered" if trig else "not_triggered",
                         "近6日累積 %+.2f%%（門檻>±%.0f%%）、當日週轉率 %.2f%%（門檻≥10%%）"
                         % (m.cum6_sum, t.k4_cum6, m.turnover_pct),
                         {"turnover_pct": round(m.turnover_pct, 2)}))

    # 款9 量能放大
    if m.avg6_vol_amp is None or m.vol_amp is None:
        out.append(_crit("k9", ("第九款", "近一段期間量能明顯放大"), False, "insufficient_data", "量能資料不足"))
    else:
        excl = m.amount is not None and m.amount < t.k9_min_amount
        trig = m.avg6_vol_amp >= t.k9_amp and m.vol_amp >= t.k9_amp and not excl
        st = "excluded" if excl and not trig else ("triggered" if trig else "not_triggered")
        out.append(_crit("k9", ("第九款", "近一段期間量能明顯放大"), trig, st,
                         "6日均量為60日均量 %.2f 倍、當日 %.2f 倍（門檻皆≥5倍）" % (m.avg6_vol_amp, m.vol_amp)))

    # 款10 累積週轉率
    if m.cum6_turnover_pct is None or m.turnover_pct is None:
        out.append(_crit("k10", ("第十款", "近6日累積週轉率明顯過高"), False, "insufficient_data", "缺週轉率(需流通股數)"))
    else:
        excl = m.amount is not None and m.amount < t.k10_min_amount
        trig = m.cum6_turnover_pct > t.k10_cum6_turnover and m.turnover_pct >= t.k10_turnover and not excl
        st = "excluded" if excl and not trig else ("triggered" if trig else "not_triggered")
        out.append(_crit("k10", ("第十款", "近6日累積週轉率明顯過高"), trig, st,
                         "近6日累積週轉率 %.2f%%（門檻>50%%）、當日 %.2f%%（門檻≥10%%）"
                         % (m.cum6_turnover_pct, m.turnover_pct)))

    # 款11 起迄價差
    if m.span6_diff is None or m.close is None:
        out.append(_crit("k11", ("第十一款", "近6日起迄兩日收盤價差異常"), False, "insufficient_data", "缺收盤資料"))
    else:
        thr = t.k11_price_diff + (int(m.close // t.k11_tier_base) * t.k11_tier_add if m.close >= t.k11_tier_base else 0)
        trig = (m.span6_diff >= thr and bool(m.is6_high)) or (-m.span6_diff >= thr and bool(m.is6_low))
        out.append(_crit("k11", ("第十一款", "近6日起迄兩日收盤價差異常"), trig,
                         "triggered" if trig else "not_triggered",
                         "近6日起迄收盤價差 %+.2f 元（門檻±%.0f 元，且須為近6日最高/最低收盤）" % (m.span6_diff, thr)))

    # 需額外資料的款別
    for k, no, name, note in [
        ("k5", "第五款", "單一券商成交占比過高", "需券商分公司買賣明細"),
        ("k6", "第六款", "本益比/淨值比異常且週轉率高", "需全市場加權本益比/淨值比平均"),
        ("k7", "第七款", "近6日漲跌異常且券資比放大", "需融資融券明細(MI_MARGN)"),
        ("k8", "第八款", "TDR 溢折價異常", "僅適用台灣存託憑證"),
        ("k12", "第十二款", "借券賣出占比過高", "需借券賣出明細"),
        ("k13", "第十三款", "當沖比過高", "需當日沖銷明細"),
    ]:
        out.append(_crit(k, (no, name), False, "insufficient_data", note + "（本工具未串接，請參官方公告）"))
    return out


# --------------------------------------------------------------------------
# 第三階：精確滾動反推
# --------------------------------------------------------------------------
def reverse(bars, m: Metrics, t: Thresholds) -> list:
    sc = []
    if m.close is None or m.cum6_sum is None or m.oldest6_return is None:
        return sc
    c = m.close
    limit_up = round(c * (1 + PRICE_LIMIT), 2)
    limit_dn = round(c * (1 - PRICE_LIMIT), 2)
    assume = "假設：市場/類股平均、除權息等非交易因素不變。"
    # 明日6日視窗 = 丟掉最舊一日(oldest6_return)、納入明日
    cum_excl_oldest = m.cum6_sum - m.oldest6_return

    def need_daily(T):
        """明日單日漲跌幅臨界值，使近6日累積=T。"""
        return T - cum_excl_oldest

    # 款1 / 款3 / 款4 共用「累積漲跌」反推（門檻不同）
    for key, label, T, extra in [
        ("k1", "第一款 近6日累積漲跌", t.k1_cum6_a, "（情形一，>%.0f%%）" % t.k1_cum6_a),
        ("k3", "第三款 漲跌+爆量", t.k3_cum6, "（另需當日量≥60日均量5倍）"),
        ("k4", "第四款 漲跌+週轉率", t.k4_cum6, "（另需當日週轉率≥10%）"),
    ]:
        r_up = need_daily(T)
        r_dn = need_daily(-T)
        price_up = round(c * (1 + r_up / 100), 2)
        price_dn = round(c * (1 + r_dn / 100), 2)
        reach = r_up <= PRICE_LIMIT * 100 + 1e-9
        if r_up <= -PRICE_LIMIT * 100:
            # 連跌停也守得住門檻（已遠超標準）
            target = ("明日即使跌停，近6日累積仍 >%.0f%%（已穩超門檻）%s。漲方向臨界收盤約 %s 元。"
                      % (T, extra, price_up))
            reach = True
        else:
            target = ("明日單日漲跌幅 ≥ %+.2f%% → 收盤 ≥ %s 元，近6日累積即 >%.0f%%%s。"
                      % (r_up, price_up, T, extra))
        sc.append({"key": key, "title": label, "target": target,
                   "reachable_next_day": reach,
                   "note": ("明日漲停參考價約 %s 元。" % limit_up)
                           + ("一日內可達。" if reach else "單日漲停不足，需連續上漲。") + " " + assume,
                   "numbers": {"need_tomorrow_return_pct": round(r_up, 2),
                               "target_close": price_up,
                               "cum6_today_sum": round(m.cum6_sum, 2),
                               "oldest_day_return": round(m.oldest6_return, 2),
                               "limit_up": limit_up}})

    # 款11 起迄價差
    if m.base6_close:
        thr = t.k11_price_diff + (int(limit_up // t.k11_tier_base) * t.k11_tier_add if limit_up >= t.k11_tier_base else 0)
        # 明日視窗首日收盤＝今日往前數第5日（bars[-5]）
        base_tmrw = bars[-5].close if len(bars) >= 5 and bars[-5].close else m.base6_close
        target_close = round(base_tmrw + thr, 2)
        sc.append({"key": "k11", "title": "第十一款 起迄兩日收盤價差",
                   "target": "明日收盤 ≥ %s 元（與明日6日視窗首日 %s 元價差達 %.0f 元，且須為近6日最高收盤）。"
                             % (target_close, base_tmrw, thr),
                   "reachable_next_day": target_close <= limit_up,
                   "note": ("一日內可達。" if target_close <= limit_up else "單日漲停不足。") + " " + assume,
                   "numbers": {"target_close": target_close, "price_diff_threshold": thr}})

    # 款4 週轉率所需量
    if m.turnover_pct is not None and m.volume_shares and m.turnover_pct > 0:
        shares = m.volume_shares / (m.turnover_pct / 100)
        lots10 = t.k4_turnover / 100 * shares / LOTS
        sc.append({"key": "k4t", "title": "第四款 週轉率條件",
                   "target": "當日週轉率需 ≥ 10%%，約需成交 %s 張（並同時滿足上方款4漲跌條件）。" % f"{lots10:,.0f}",
                   "reachable_next_day": None,
                   "note": "週轉率取決於成交量，無漲跌幅限制可單日達成。" + " " + assume,
                   "numbers": {"lots_for_10pct_turnover": round(lots10)}})

    # 款3/9 量能
    if m.avg60_vol:
        need = 5 * m.avg60_vol / LOTS
        sc.append({"key": "k3v", "title": "第三/九款 量能放大",
                   "target": "明日成交量 ≥ %s 張（達60日均量5倍）。" % f"{need:,.0f}",
                   "reachable_next_day": None,
                   "note": "60日均量約 %s 張。" % f"{m.avg60_vol/LOTS:,.0f}" + " " + assume,
                   "numbers": {"avg60_lots": round(m.avg60_vol / LOTS), "need_volume_lots": round(need)}})
    return sc


# --------------------------------------------------------------------------
# 串接
# --------------------------------------------------------------------------
MARKET_LABEL = {"TWSE": "上市", "TPEX": "上櫃", "EMERGING": "興櫃", "UNKNOWN": "未知"}


def analyze(stock_no: str, months: int = 6) -> dict:
    stock_no = stock_no.strip()
    sd = src.fetch_stock(stock_no, months=months)
    t = thresholds_for(sd.market)

    result = {
        "code": sd.code, "name": sd.name, "market": sd.market,
        "market_label": MARKET_LABEL.get(sd.market, sd.market),
        "warnings": list(sd.warnings),
        "as_of": sd.bars[-1].date.isoformat() if sd.bars else None,
        "shares_outstanding": sd.shares_outstanding,
        "threshold_note": "本檔以 %s 門檻評估（款三累積漲跌門檻 %.0f%%）。"
                          % (MARKET_LABEL.get(sd.market, sd.market), t.k3_cum6),
    }
    if not sd.bars:
        result.update({"stage": "NO_DATA", "headline": "查無日成交資料，無法分析。",
                       "criteria": [], "reverse_scenarios": [],
                       "disposition": {"official": None, "distance_to_disposition": {}, "notes": []}})
        return result

    m = compute_metrics(sd.bars, sd.shares_outstanding)
    crits = evaluate(m, t, market_avg=0.0)
    triggered = [c for c in crits if c["triggered"]]
    today_is_attention = len(triggered) > 0
    dview = assess(stock_no, today_is_attention)
    scenarios = reverse(sd.bars, m, t)

    result.update({
        "stage": dview.stage, "headline": dview.headline,
        "metrics": {"close": m.close, "cum6_sum": _r(m.cum6_sum),
                    "oldest6_return": _r(m.oldest6_return), "vol_amp": _r(m.vol_amp),
                    "turnover_pct": _r(m.turnover_pct), "cum6_turnover_pct": _r(m.cum6_turnover_pct),
                    "span6_diff": _r(m.span6_diff), "ret30": _r(m.ret30),
                    "ret60": _r(m.ret60), "ret90": _r(m.ret90), "pe": m.pe, "pb": m.pb},
        "criteria": crits,
        "triggered_criteria": [c["key"] for c in triggered],
        "today_is_attention": today_is_attention,
        "disposition": {
            "official": dview.official_disposition,
            "attention_count": dview.attention_count,
            "attention_window": dview.attention_window,
            "distance_to_disposition": dview.distance_to_disposition,
            "notes": dview.notes},
        "reverse_scenarios": scenarios,
        "stage_explanation": {
            "stage1": "抓官方既成注意/處置公告；已公告處置日期即結束。",
            "stage2": "公式化注意/處置標準，判斷今日踩到哪幾款、距處置還差幾次。",
            "stage3": "滾動視窗反推明日單日漲跌幅臨界值、臨界收盤價、所需週轉率/量能。"},
        "disclaimer": "本工具依官方公開資料與作業要點推算（累積漲跌＝每日漲跌幅加總；含市場/類股平均近似），"
                      "僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else x
