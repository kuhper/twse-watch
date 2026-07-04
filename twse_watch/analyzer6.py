"""整合版 v0.7（analyzer6，最終）：修正官方注意來源與解析。

* 官方累計次數改用即時 RWD notetrans（official.py），並支援「連續N次」寫法。
* 處置投影優先用官方累計次數；官方無值時退回歷史序列自算（history.py）。
"""
from __future__ import annotations

from . import source as src
from . import official as offi
from .analyzer import compute_metrics, evaluate, reverse, thresholds_for, MARKET_LABEL, _r
from .analyzer2 import project_disposition
from .daytrade import evaluate_k13
from .history import announcement_history, self_counts


def _k13_scenario(k13_today, k13_next):
    mt = (k13_today or {}).get("metrics", {})
    mn = (k13_next or {}).get("metrics", {})
    return {"key": "k13", "title": "第十三款 當沖比",
            "target": "需『當日當沖比』與『近6日當沖比』同 > 60%（且當沖量>5000張、成交額>5億）。",
            "reachable_next_day": None,
            "note": ("現行注意基準日 %s：當日 %s%%、近6日 %s%%。明日預判基準日 %s：當日 %s%%、近6日 %s%%。"
                     % (mt.get("as_of", "-"), mt.get("day_ratio"), mt.get("cum6_ratio"),
                        mn.get("as_of", "-"), mn.get("day_ratio"), mn.get("cum6_ratio"))),
            "numbers": {"announced_day": mt, "next_day": mn}}


def _official_projection(count, window, consecutive, dist, today_is_attention):
    proj = {"available": True, "source": "official", "leads_to_disposition_tomorrow": None,
            "binding_rule": None, "remaining": None, "messages": []}
    if not dist:
        proj["available"] = False
        return proj
    binding, rem = min(dist.items(), key=lambda kv: kv[1])
    proj["binding_rule"], proj["remaining"] = binding, rem
    tnote = "（今日規則引擎亦推估達注意）" if today_is_attention else ""
    if rem <= 0:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append("官方累計已達「%s」→ 預期公告處置。" % binding)
    elif rem == 1:
        proj["leads_to_disposition_tomorrow"] = True
        proj["messages"].append(
            "官方累計 %d 次%s；明日再被列注意（第 %d 次）即達「%s」→ 預期次一營業日起處置"
            "（近30日首次為第一次處置：人工約5分鐘撮合＋單筆≥10張/多筆累計≥30張預收款券）。"
            % (count, ("（連續）" if consecutive else ""), count + 1, binding))
    else:
        proj["leads_to_disposition_tomorrow"] = False
        proj["messages"].append("官方累計 %d 次；最接近門檻「%s」，尚差 %d 次%s。"
                                % (count, binding, rem, tnote))
    return proj


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

    # === 官方注意（權威來源：notice 個股歷史，含各款與計入處置判定）===
    import datetime as _dt
    disps = src.fetch_official_disposition(stock_no)
    # end 為 None 表示日期解析失敗，保守視為仍在處置中
    ongoing = [d for d in disps if d.end is None or d.end >= _dt.date.today()]

    # 官方個股注意歷史（截圖那個查詢頁的後端）+ 計入處置進度
    ahist = offi.fetch_attention_history(stock_no) if sd.market == "TWSE" else None
    trading_dates = [b.date for b in sd.bars]
    ap = offi.disposition_progress(ahist["entries"], trading_dates) if ahist else {"available": False}

    scenarios = reverse(sd.bars, m, t)
    if k13_today and k13_today.get("metrics"):
        scenarios.append(_k13_scenario(k13_today, k13_next))

    # 歷史序列自算（官方注意端點無法涵蓋時的後備，例如上櫃）
    hist = announcement_history(sd.bars, stock_no, shares, market=sd.market, lookback=30, k13_lookback=10)
    sc = self_counts(hist)

    latest_att = ahist["entries"][-1] if (ahist and ahist["entries"]) else None

    if ongoing:
        # 官方已正式公告處置
        d = max(ongoing, key=lambda x: x.end)
        stage = "DISPOSED"
        headline = "已公告處置：%s，處置期間 %s（原因：%s）。" % (d.measures, d.period, d.reason)
        disp = {"official": {"reason": d.reason, "period": d.period, "measures": d.measures,
                             "start": d.start.isoformat() if d.start else None,
                             "end": d.end.isoformat() if d.end else None, "detail": d.detail[:400]},
                "attention_progress": ap,
                "distance_to_disposition": ap.get("distance", {}), "notes": ["處置時間已明確。"]}
        proj = {"available": True, "leads_to_disposition_tomorrow": True, "messages": ["已處於官方處置期間。"]}

    elif ap.get("available"):
        # 有官方注意歷史 → 用計入處置款別數距離
        binding, rem = ap["binding_rule"], ap["remaining"]
        proj = {"available": True, "source": "official_notice", "binding_rule": binding,
                "remaining": rem, "leads_to_disposition_tomorrow": rem <= 1, "messages": []}
        base = ("官方注意：近期共 %d 天列注意，其中**計入處置** %d 天"
                "（連續 %d 日／近10日 %d 次／近30日 %d 次）。"
                % (ap["official_cum_attention"], ap["dispo_counting_days"],
                   ap["consecutive_dispo_days"], ap["count_10d"], ap["count_30d"]))
        if rem <= 0:
            stage = "DISPOSED"
            proj["leads_to_disposition_tomorrow"] = True
            proj["messages"].append(
                "⚠ 已達「%s」處置標準；官方 punish 公告可能尚未同步至 API，請至 TWSE 官網確認。" % binding)
        else:
            stage = "WATCH"
            proj["messages"].append(
                "最接近門檻「%s」，再被列注意（計入處置款別）%d 次即達標。" % (binding, rem))
        headline = base + proj["messages"][0]
        disp = {"official": None, "attention_progress": ap,
                "latest_attention": (latest_att["text"] if latest_att else None),
                "latest_attention_date": (latest_att["date_str"] if latest_att else None),
                "distance_to_disposition": ap["distance"],
                "notes": ["資料來源：官方個股注意歷史（notice 端點）。"
                          "距離只計入第 1~8 款；第 9~13 款（紅字）依規定不計入處置。"]}

    else:
        # 官方注意端點無資料（例如上櫃、或近期未列注意）→ 退回自算
        sp = sc.get("distance", {})
        proj = {"available": sc.get("available", False), "source": "self",
                "leads_to_disposition_tomorrow": None, "messages": []}
        stage = "WATCH" if today_is_attention or (sc.get("available") and sc["consecutive_announced"] > 0) else "CLEAR"
        if sc.get("available") and sp:
            binding, rem = min(sp.items(), key=lambda kv: kv[1])
            proj["binding_rule"], proj["remaining"] = binding, rem
            proj["leads_to_disposition_tomorrow"] = rem <= 1
            if rem <= 0:
                stage = "DISPOSED"
                proj["leads_to_disposition_tomorrow"] = True
                proj["messages"].append(
                    "⚠ 自算已達「%s」處置標準（連續 %d 日、近30日 %d 次），"
                    "官方 API 尚未更新；請至 TWSE/TPEx 官網確認處置公告。"
                    % (binding, sc["consecutive_announced"], sc["count_30d_k18"]))
            else:
                proj["messages"].append("官方注意端點無此檔，改用自算：連續 %d 日、近30日 %d 次；最接近「%s」尚差 %d。"
                                        % (sc["consecutive_announced"], sc["count_30d_k18"], binding, rem))
        headline = ("（官方 API 尚未更新，以下為自算）" if stage == "DISPOSED" else "（官方注意端點暫無此檔，以下為自算）") + \
                   (proj["messages"][0] if proj["messages"] else "近期未觸發。")
        disp = {"official": None, "attention_progress": None,
                "distance_to_disposition": sp,
                "notes": ["官方注意端點無此檔，改用歷史序列自算（上櫃或近期未列注意常見）。"]}

    if stage != "DISPOSED" and today_is_attention and proj.get("leads_to_disposition_tomorrow"):
        headline += "　⚠ 逼近處置。"

    result.update({
        "stage": stage, "headline": headline,
        "metrics": {"close": m.close, "cum6_sum": _r(m.cum6_sum), "oldest6_return": _r(m.oldest6_return),
                    "vol_amp": _r(m.vol_amp), "turnover_pct": _r(m.turnover_pct),
                    "cum6_turnover_pct": _r(m.cum6_turnover_pct), "span6_diff": _r(m.span6_diff),
                    "ret30": _r(m.ret30), "ret60": _r(m.ret60), "ret90": _r(m.ret90)},
        "criteria": crits,
        "triggered_criteria": [c["key"] for c in triggered],
        "today_is_attention": today_is_attention,
        "disposition": disp,
        "disposition_projection": proj,
        "self_history": {**sc, "recent_days": hist[-12:]},
        "reverse_scenarios": scenarios,
        "disclaimer": "官方注意累計採即時 RWD 端點；自算次數實算款1-4,9,10,11,13（缺款5-8,12且含近似）。"
                      "僅供研究參考，實際以證交所/櫃買中心公告為準，非投資建議。",
    })
    return result
