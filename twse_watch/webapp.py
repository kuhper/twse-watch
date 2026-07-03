"""FastAPI 後端（乾淨版，取代 server.py）。

啟動：python start.py  或  uvicorn twse_watch.webapp:app --reload
"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import analyze

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="台股注意/處置三階段預判", version="0.1.0")


@app.get("/api/query")
def api_query(code: str = Query(..., description="股票代號，如 2330"),
              months: int = Query(6, ge=2, le=12)):
    try:
        return JSONResponse(analyze(code, months=months))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"stage": "ERROR", "code": code,
                             "headline": "分析發生錯誤：%s" % e}, status_code=200)


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
