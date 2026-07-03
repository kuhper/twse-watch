"""★ 最新標準後端 v0.7 ★ analyzer6（官方即時注意來源 + 連續N次解析）+ 前端 index3.html。

啟動：python serve7.py  或  uvicorn app7:app --reload
"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from twse_watch.analyzer6 import analyze

_STATIC = pathlib.Path(__file__).resolve().parent / "static"

app = FastAPI(title="台股注意/處置 進度查詢（v0.7）", version="0.7.0")


@app.get("/api/query")
def api_query(code: str = Query(...), months: int = Query(6, ge=2, le=12)):
    try:
        return JSONResponse(analyze(code, months=months))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"stage": "ERROR", "code": code,
                             "headline": "分析發生錯誤：%s" % e}, status_code=200)


@app.get("/")
def index():
    return FileResponse(_STATIC / "index3.html")


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
