"""整合版 v0.6（analyzer5，最終）：在 v0.5 之上加『歷史公告序列自算』。

不再只依賴官方 notetrans 即時快照——逐日回算過去30營業日的公告，
自算『累積第幾次／連續幾日』，官方缺值時自動以自算值接管處置進度。
"""
from __future__ import annotations

from . import source as src
from .analyzer import compute_metrics, evaluate, reverse, thresholds_for, MARKET_LABEL, _r
from .dispo import assess
from .analyzer2 import project_disposition
from .daytrade import evaluate_k13
from .history import announcement_history, self_counts


def _k13_scenario(k13_today, k13_next):
    mt = k13_today.get("metrics", {}) if k13_today else {}
    mn = k13_next.get("metrics", {}) if k13_next else {}
    return {"key": "k13", "title": "第十三款 當沖比",
            "target": "需『當日當沖比』與『近6日當沖比』同 > 60%（且當沖量>5000張、成交額>5億）。",
            "reachable_next_day": None,
            "note": ("現行注意基準日 %s：當日 %s%%、近6日 %s%%。明日預判基準日 %s：當日 %s%%、近6日 %s%%。"
                     % (mt.get("as_of", "-"), mt.get("day_ratio"), mt.get("cum6_ratio"),
                        mn.get("as_of", "-"), mn.get("day_ratio"), mn.get("cum6_ratio"))),
            "numbers": {"announced_day": mt, "next_day": mn}}


def analyze(stock_no: str, months: int = 6) -> dict:
    stock_no = stock_no.strip()
    sd = src.fetch_stock(stock_no, months=months)
    t = thresholds_for(sd.market)
    shares = sd.shares_outstanding

    result = {
        "code": sd.code, "name": sd.name, "market": sd.market,
        "market_label": MARKET_LABEL.get(sd.market, sd.market),
        "warnings": list(sd.warnings),
        "as_of": sd.bars[-1].date.isoformat() if sd.bars else None,
        "shares_outstanding": shares,
        "threshold_note": "本檔以 %s 門檻評估（款三累積漲跌門檻 %.0f%%）。"
                          % (MARKET_LABEL.get(sd.market, sd.market), t.k3_cum6),
    }
    if not sd.bars:
        result.update({"stage": "NO_DATA", "headline": "查無日成交資料，無法分析。",
                       "criteria": [], "reverse_scenarios": [],
                       "disposition": {"official": None, "distance_to_disposition": {}, "notes": []},
                       "disposition_projection": {"available": False, "messages": []},
                       "self_history": {"available": False}})
        return result

    m = compute_metrics(sd.bars, shares)
    crits = evaluate(m, t, market_avg=0.0)

    # 第十三款（上市，T-1 時點）
    k13_today = k13_next = None
    if sd.market == "TWSE":
        k13_today = evaluate_k13(sd.bars[:-1], stock_no, shares) if len(sd.bars) >= 7 else evaluate_k13(sd.bars, stock_no, shares)
        k13_next = evaluate_k13(sd.bars, stock_no, shares)
        crits = [k13_today if c["key"] == "k13" else c for c in crits]
    else:
        for c in crits:
            if c["key"] == "k13":
                c["detail"] = "當沖明細目前僅串接上市(TWSE)；上櫃請參官方公告。"

    triggered = [c for c in crits if c["triggered"]]
    today_is_attention = len(triggered) > 0
    dview = assess(stock_no, today_is_attention)
    scenarios = reverse(sd.bars, m, t)
    if k13_today and k13_today.get("metrics"):
        scenarios.append(_k13_scenario(k13_today, k13_next))
    proj = project_disposition(dview, scenarios)

    # === 歷史公告序列自算 ===
    hist = announcement_history(sd.bars, stock_no, shares, market=sd.market,
                                lookback=30, k13_lookback=10)
    sc = self_counts(hist)
    self_proj = _self_projection(sc) if sc.get("available") else {"available": False, "messages": []}

    # headline：官方優先，缺值改用自算
    headline = dview.headline
    if proj.get("leads_to_disposition_tomorrow") and dview.stage != "DISPOSED":
        headline += "　⚠ 明日達標即進入處置（款1~8累積）。"
    if dview.attention_count is None and sc.get("available"):
        headline = ("（官方快照無此檔，以下為自算）近30日達公告 %d 次、最近連續 %d 日；%s"
                    % (sc["announced_days_total"], sc["consecutive_announced"], self_proj.get("headline", "")))

    result.update({
        "stage": dview.stage, "headline": headline,
        "metrics": {"close": m.close, "cum6_sum": _r(m.cum6_sum), "oldest6_return": _r(m.oldest6_return),
                    "vol_amp": _r(m.vol_amp), "turnover_pct": _r(m.turnover_pct),
                    "cum6_turnover_pct": _r(m.cum6_turnover_pct), "span6_diff": _r(m.span6_diff),
                    "ret30": _r(m.ret30), "ret60": _r(m.ret60), "ret90": _r(m.ret90)},
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
        "self_history": {**sc, "projection": self_proj, "recent_days": hist[-12:]},
        "reverse_scenarios": scenarios,
        "disclaimer": "本工具依官方公開資料與作業要點推算；自算次數實算款1-4,9,10,11,13（缺款5-8,12且含近似），"
                      "可能與官方有出入，僅供預警，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result


def _self_projection(sc: dict) -> dict:
    dist = sc.get("distance", {})
    proj = {"available": True, "messages": [], "binding_rule": None, "remaining": None,
            "leads_to_disposition_tomorrow": None}
    if not dist:
        return proj
    binding, rem = min(dist.items(), key=lambda kv: kv[1])
    proj["binding_rule"], proj["remaining"] = binding, rem
    consec = sc.get("consecutive_announced", 0)
    if rem <= 0:
        proj["leads_to_disposition_tomorrow"] = True
        proj["headline"] = "自算已達「%s」→ 預期處置。" % binding
    elif rem == 1:
        # 連續3日需明日續達；累積型需明日再被列注意
        proj["leads_to_disposition_tomorrow"] = True
        proj["headline"] = "距「%s」僅差 1 次/日，明日達標即進入處置。" % binding
    else:
        proj["leads_to_disposition_tomorrow"] = False
        proj["headline"] = "最接近門檻「%s」，尚差 %d 次/日。" % (binding, rem)
    proj["messages"].append(
        "自算：最近連續公告 %d 日；最近10日內款1~8公告 %d 次、最近30日內 %d 次。"
        % (consec, sc.get("count_10d_k18", 0), sc.get("count_30d_k18", 0)))
    proj["messages"].append(proj["headline"])
    proj["messages"].append("註：自算僅含款1-4,9,10,11,13（缺款5-8,12、且含近似），與官方可能有出入。")
    return proj
