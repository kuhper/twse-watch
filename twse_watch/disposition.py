"""Disposition assessment (stages 1 and 2). See README for the official rules."""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Optional

from . import source as src

# rule name -> (window days, times needed)
DISPOSITION_RULES = {
    "consecutive_3d": (3, 3),
    "recent_5d_5x": (5, 5),
    "recent_10d_6x": (10, 6),
    "recent_30d_12x": (30, 12),
}
RULE_LABEL = {
    "consecutive_3d": "連續3個營業日",
    "recent_5d_5x": "最近5日內5次",
    "recent_10d_6x": "最近10日內6次",
    "recent_30d_12x": "最近30日內12次",
}


@dataclass
class DispositionView:
    stage: str
    headline: str
    official_disposition: Optional[dict] = None
    attention_count: Optional[int] = None
    attention_window: Optional[int] = None
    distance_to_disposition: dict = field(default_factory=dict)
    today_is_attention: Optional[bool] = None
    notes: list = field(default_factory=list)


def _parse_attention_text(text: str):
    """'...等九個營業日已有五次' -> (5, 9); '連續三個營業日' -> (3, 3)."""
    if not text:
        return None, None
    count = window = None
    m = re.search(r"已有([零一二兩三四五六七八九十\d]+)次", text)
    if m:
        count = src.cjk_to_int(m.group(1))
    m = re.search(r"等([零一二兩三四五六七八九十\d]+)個營業日", text)
    if m:
        window = src.cjk_to_int(m.group(1))
    m = re.search(r"連續([零一二兩三四五六七八九十\d]+)個營業日", text)
    if m and count is None:
        n = src.cjk_to_int(m.group(1))
        count, window = n, n
    return count, window


def assess(code: str, today_is_attention: Optional[bool]) -> DispositionView:
    today = _dt.date.today()

    # Stage 1: existing official disposition announcement.
    disps = src.fetch_official_disposition(code)
    ongoing = [d for d in disps if d.end and d.end >= today]
    if ongoing:
        d = max(ongoing, key=lambda x: x.end or today)
        return DispositionView(
            stage="DISPOSED",
            headline="已公告處置：%s，處置期間 %s（原因：%s）。" % (d.measures, d.period, d.reason),
            official_disposition={
                "reason": d.reason, "period": d.period, "measures": d.measures,
                "start": d.start.isoformat() if d.start else None,
                "end": d.end.isoformat() if d.end else None,
                "detail": d.detail[:400]},
            notes=["處置時間已明確，第一階即結束；下方僅附後續監控供參。"])

    # Stage 2: attention accumulation -> distance to disposition.
    att = src.fetch_official_attention(code)
    count, window = _parse_attention_text(att.criteria_text) if att else (None, None)

    view = DispositionView(
        stage="WATCH" if (att or today_is_attention) else "CLEAR",
        headline="", attention_count=count, attention_window=window,
        today_is_attention=today_is_attention)

    if att:
        view.notes.append("官方注意公告：" + (att.criteria_text or "(已列注意)"))
    if disps and not ongoing:
        view.notes.append("近期曾有處置紀錄 %d 筆（已出關），第二次以上處置條件更嚴。" % len(disps))

    if count is not None:
        is_consec = bool(att and "連續" in (att.criteria_text or ""))
        for rk, (win, need) in DISPOSITION_RULES.items():
            if rk in ("consecutive_3d", "recent_5d_5x"):
                if is_consec and rk == "consecutive_3d":
                    view.distance_to_disposition[RULE_LABEL[rk]] = max(need - count, 0)
                continue
            if window is None or win >= window:
                view.distance_to_disposition[RULE_LABEL[rk]] = max(need - count, 0)
        note = "（今日依規則引擎推估亦達注意，計入則再少 1 次）" if today_is_attention else ""
        if view.distance_to_disposition:
            r, rem = min(view.distance_to_disposition.items(), key=lambda kv: kv[1])
            if rem <= 0:
                view.stage = "DISPOSED"
                view.headline = "累積次數已達「%s」處置標準，預期將公告處置（以官方公告為準）。" % r
            else:
                view.headline = ("官方注意累積 %d 次／最近 %s 個營業日；最接近門檻「%s」，再列注意 %d 次即達標%s。"
                                 % (count, window, r, rem, note))
        else:
            view.headline = "官方注意累積 %d 次／最近 %s 個營業日；請參官方公告%s。" % (count, window, note)
    elif today_is_attention:
        view.headline = "今日依規則引擎推估『達注意標準』，官方累積次數尚未顯示；連續或累積達標將公告處置。"
    elif view.stage == "CLEAR":
        view.headline = "目前未列注意、亦無處置；下方提供距各注意門檻的反推數字供預警。"
    else:
        view.headline = "已被列注意，但無法解析累積次數，請參官方公告。"
    return view
