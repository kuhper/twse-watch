"""整合版 v0.5（analyzer4，最終）：修正第十三款的『T-1 時點』。

第十二、十三款（借券、當沖）法規明定以『當日之前一個營業日』判定，故：
* 現行注意狀態（今日盤後公告）＝以最新完整日的『前一營業日』為基準。
* 明日預判＝以最新完整日為基準。

以 daytrade.evaluate_k13(bars_slice) 達成（不改 daytrade.py）：
  evaluate_k13(bars[:-1]) → 基準日 = 倒數第2根（＝現行注意對應日）
  evaluate_k13(bars)      → 基準日 = 最新一根（＝明日是否續列）
"""
from __future__ import annotations

from . import source as src
from .analyzer import compute_metrics, evaluate, reverse, thresholds_for, MARKET_LABEL, _r
from .dispo import assess
from .analyzer2 import project_disposition
from .daytrade import evaluate_k13, consecutive_k13_days


def _k13_scenario(k13_today: dict, k13_next: dict) -> dict:
    mt = k13_today.get("metrics", {})
    mn = k13_next.get("metrics", {})
    return {
        "key": "k13", "title": "第十三款 當沖比",
        "target": "需『當日當沖比』與『近6日當沖比』同 > 60%（且當沖量>5000張、成交額>5億）。",
        "reachable_next_day": None,
        "note": ("現行注意基準日 %s：當日 %s%%、近6日 %s%%。"
                 "明日預判基準日 %s：當日 %s%%、近6日 %s%%。當沖比取決於隔日當沖力道，非價格可反推。"
                 % (mt.get("as_of", "-"), mt.get("day_ratio"), mt.get("cum6_ratio"),
                    mn.get("as_of", "-"), mn.get("day_ratio"), mn.get("cum6_ratio"))),
        "numbers": {"announced_day": mt, "next_day": mn},
    }


def _k13_projection(consec: int, today_triggers: bool, next_triggers: bool) -> dict:
    proj = {"available": True, "rule": "連續3日(第十三款屬之)", "consecutive_days": consec,
            "leads_to_disposition_tomorrow": None, "messages": []}
    if consec >= 3:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append("自算最近已連續 %d 個營業日達第十三款，符合『連續3個營業日』處置標準。" % consec)
    elif consec == 2:
        if next_triggers:
            proj["leads_to_disposition_tomorrow"] = True
            proj["messages"].append("已連續2日達第十三款，且明日(最新日)仍達標 → 將成連續3日 → 觸發處置。")
        else:
            proj["leads_to_disposition_tomorrow"] = False
            proj["messages"].append("已連續2日達第十三款，但最新日當沖已降溫未達標，連續中斷，暫不處置。")
    elif consec == 1:
        proj["messages"].append("最近1個營業日達第十三款；需連續3日才處置。")
    else:
        proj["messages"].append("現行基準日未達第十三款（可能當沖已降溫或屬除外）。")
    proj["messages"].append("註：以當沖明細自算，官方以實際公告為準；第十三款不計入『10日內6次/30日內12次』(限第1~8款)，"
                            "但計入『連續3日』。")
    return proj


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
                       "disposition": {"official": None, "distance_to_disposition": {}, "notes": []},
                       "disposition_projection": {"available": False, "messages": []}})
        return result

    m = compute_metrics(sd.bars, sd.shares_outstanding)
    crits = evaluate(m, t, market_avg=0.0)

    # === 第十三款（僅上市）：以 T-1 為現行注意基準 ===
    k13_proj = None
    k13_today = k13_next = None
    if sd.market == "TWSE":
        shares = sd.shares_outstanding
        k13_today = evaluate_k13(sd.bars[:-1], stock_no, shares) if len(sd.bars) >= 7 else evaluate_k13(sd.bars, stock_no, shares)
        k13_next = evaluate_k13(sd.bars, stock_no, shares)
        crits = [k13_today if c["key"] == "k13" else c for c in crits]
        consec = consecutive_k13_days(sd.bars[:-1] if len(sd.bars) >= 7 else sd.bars, stock_no, shares)
        k13_proj = _k13_projection(consec, k13_today["triggered"], k13_next["triggered"])
    else:
        for c in crits:
            if c["key"] == "k13":
                c["detail"] = "當沖明細目前僅串接上市(TWSE)；上櫃請參官方公告。"

    triggered = [c for c in crits if c["triggered"]]
    today_is_attention = len(triggered) > 0
    dview = assess(stock_no, today_is_attention)
    scenarios = reverse(sd.bars, m, t)
    if k13_today and k13_today.get("metrics"):
        scenarios.append(_k13_scenario(k13_today, k13_next or k13_today))

    proj = project_disposition(dview, scenarios)

    headline = dview.headline
    if proj.get("leads_to_disposition_tomorrow") and dview.stage != "DISPOSED":
        headline += "　⚠ 明日達標即進入處置（款1~8累積）。"
    if k13_today and k13_today["triggered"]:
        headline += "　• 現行達第十三款（當沖比）。"
        if k13_proj and k13_proj.get("leads_to_disposition_tomorrow"):
            headline += "　⚠ 逼近『連續3日』處置。"

    result.update({
        "stage": dview.stage, "headline": headline,
        "metrics": {"close": m.close, "cum6_sum": _r(m.cum6_sum),
                    "oldest6_return": _r(m.oldest6_return), "vol_amp": _r(m.vol_amp),
                    "turnover_pct": _r(m.turnover_pct), "cum6_turnover_pct": _r(m.cum6_turnover_pct),
                    "span6_diff": _r(m.span6_diff), "ret30": _r(m.ret30),
                    "ret60": _r(m.ret60), "ret90": _r(m.ret90)},
        "criteria": crits,
        "triggered_criteria": [c["key"] for c in triggered],
        "today_is_attention": today_is_attention,
        "disposition": {
            "official": dview.official_disposition,
            "attention_count": dview.attention_count,
            "attention_window": dview.attention_window,
            "distance_to_disposition": dview.distance_to_disposition,
            "notes": dview.notes},
        "disposition_projection": proj,
        "disposition_projection_k13": k13_proj,
        "reverse_scenarios": scenarios,
        "disclaimer": "本工具依官方公開資料與作業要點推算（累積漲跌＝每日漲跌幅加總；當沖比依TWTB4U且依T-1時點；"
                      "含市場/類股平均近似），僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result
