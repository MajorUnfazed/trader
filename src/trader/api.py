from pathlib import Path
import sqlite3
try:
    from fastapi import FastAPI, HTTPException
except ImportError:
    raise RuntimeError("Install dashboard/API extras: pip install -e '.[web]'")
app=FastAPI(title="Long-Term Trader — Paper Only")
def db():
    if not Path("data/trader.db").exists(): raise HTTPException(404,"No paper-trading database yet")
    c=sqlite3.connect("data/trader.db"); c.row_factory=sqlite3.Row; return c
@app.get("/health")
def health(): return {"mode":"paper-only","live_trading":False}
@app.get("/portfolio")
def portfolio():
    c=db(); row=c.execute("SELECT * FROM snapshots ORDER BY date DESC LIMIT 1").fetchone(); return dict(row) if row else {}
@app.get("/trades")
def trades():
    c=db(); return [dict(x) for x in c.execute("SELECT * FROM fills ORDER BY id DESC LIMIT 100")]
@app.get("/decisions")
def decisions():
    c=db(); return [dict(x) for x in c.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 100")]
