"""
╔══════════════════════════════════════════════════════════════════╗
║   NIFTY 50 · EXPANDED SIGNAL SCANNER v4                         ║
║   7 Strategies + Zerodha Kite Connect + Paper Tracker            ║
║                                                                  ║
║   Strategies:                                                    ║
║     1. ORB (Opening Range Breakout)                              ║
║     2. EMA 9/21/50 Crossover                                     ║
║     3. VWAP Band                                                 ║
║     4. S&R Reversal                                              ║
║     5. 9:20 Short Straddle (Option Selling)                      ║
║     6. Iron Condor (Range-bound)                                 ║
║     7. Supertrend (ATR-based trend)                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, time, threading, logging, hashlib, pyotp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, jsonify, render_template_string, request, redirect
from flask_cors import CORS

# Kite Connect — imported lazily so app boots even without the package
try:
    from kiteconnect import KiteConnect, KiteTicker
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    log_tmp = logging.getLogger("scanner")
    log_tmp.warning("kiteconnect not installed — using yfinance fallback")

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
IST           = ZoneInfo("Asia/Kolkata")
SCAN_INTERVAL = 300
PORT          = int(os.environ.get("PORT", 5050))

# Directional strategy thresholds
EMA_FAST      = 9
EMA_SLOW      = 21
EMA_TREND     = 50
RSI_PERIOD    = 14
RSI_BULL      = 54
RSI_BEAR      = 46
VOL_MULT      = 1.5
ADX_MIN       = 20
MOMENTUM_BARS = 3
MIN_SCORE     = 3      # out of 4 directional strategies

# Supertrend
ST_ATR_PERIOD = 10
ST_FACTOR     = 3.0

# Straddle / Condor params
STRADDLE_SL_PCT  = 0.25   # 25% loss on premium = stop
CONDOR_WIDTH_PCT = 0.015  # 1.5% OTM for condor wings
CONDOR_SL_PCT    = 0.50   # 50% of premium collected

# ─── ZERODHA / KITE CONFIG ───────────────────────────────────────────────────
RAILWAY_URL     = os.environ.get("RAILWAY_URL", "https://nifty-screener-production.up.railway.app")
KITE_API_KEY    = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")
KITE_TOTP_SECRET= os.environ.get("KITE_TOTP_SECRET", "")   # optional — for future auto-login
TOKEN_FILE      = "kite_token.json"

# Nifty 50 instrument token on NSE
NIFTY_TOKEN     = 256265   # NSE:NIFTY 50 index token

# Runtime Kite session
kite_session    = None          # KiteConnect instance
kite_lock       = threading.Lock()

def _kite_active() -> bool:
    """True if we have a valid Kite session loaded for today."""
    with kite_lock:
        return kite_session is not None

def _load_token() -> bool:
    """Load saved token from disk. Returns True if valid for today."""
    global kite_session
    if not KITE_AVAILABLE or not KITE_API_KEY:
        return False
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        saved_date = data.get("date", "")
        today      = datetime.now(IST).strftime("%Y-%m-%d")
        if saved_date != today:
            log.info("Kite token is from a previous day — re-login needed")
            return False
        access_token = data["access_token"]
        kc = KiteConnect(api_key=KITE_API_KEY)
        kc.set_access_token(access_token)
        # Quick validation
        kc.profile()
        with kite_lock:
            kite_session = kc
        log.info(f"✅ Kite session restored from disk (token date: {saved_date})")
        return True
    except Exception as e:
        log.warning(f"Token load failed: {e}")
        return False

def _save_token(access_token: str):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": today}, f)

def kite_token_status() -> dict:
    """Return dict describing current Kite token state for the dashboard."""
    if not KITE_API_KEY:
        return {"status": "not_configured", "label": "API key not set", "color": "muted"}
    if not _kite_active():
        return {"status": "logged_out",    "label": "Not logged in today", "color": "red"}
    return         {"status": "active",       "label": "Connected ✓",         "color": "green"}

# ─── KITE DATA FUNCTIONS ──────────────────────────────────────────────────────
def _kite_quote():
    """Fetch Nifty LTP from Kite real-time."""
    with kite_lock:
        kc = kite_session
    data  = kc.ltp([f"NSE:{NIFTY_TOKEN}"])
    # key is instrument_token as int
    key   = str(NIFTY_TOKEN)
    # kite returns keyed by "NSE:260105" or token — handle both
    val   = data.get(f"NSE:{NIFTY_TOKEN}") or data.get(key) or list(data.values())[0]
    price = float(val["last_price"])
    ohlc  = val.get("ohlc", {})
    prev  = float(ohlc.get("close", price))
    chg   = price - prev
    pct   = (chg / prev * 100) if prev else 0
    return {"price": round(price,2), "change": round(chg,2), "pct": round(pct,2), "prev": round(prev,2)}

def _kite_history() -> pd.DataFrame:
    """Fetch today's 5-min OHLCV from Kite historical API."""
    with kite_lock:
        kc = kite_session
    now       = datetime.now(IST)
    from_dt   = now.replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt     = now
    records   = kc.historical_data(NIFTY_TOKEN, from_dt, to_dt, "5minute", continuous=False)
    if not records:
        raise ValueError("Kite returned empty historical data")
    df = pd.DataFrame(records)
    df = df.rename(columns={"date":"datetime","open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df[["Open","High","Low","Close","Volume"]].dropna()

def _kite_history_multi(days=60) -> pd.DataFrame:
    """Fetch multi-day 5-min data from Kite for backtesting."""
    with kite_lock:
        kc = kite_session
    now     = datetime.now(IST)
    from_dt = now - timedelta(days=days)
    records = kc.historical_data(NIFTY_TOKEN, from_dt, now, "5minute", continuous=False)
    if not records:
        raise ValueError("Kite returned empty data")
    df = pd.DataFrame(records)
    df = df.rename(columns={"date":"datetime","open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df[["Open","High","Low","Close","Volume"]].dropna()

# ─── SHARED STATE ─────────────────────────────────────────────────────────────
state = {
    "last_update":    None,
    "next_scan":      None,
    "market_open":    False,
    "nifty_price":    None,
    "nifty_change":   None,
    "nifty_pct":      None,
    "signals":        {},
    "score":          0,
    "trade_alert":    None,
    "alert_history":  [],
    "indicators":     {},
    "candles":        [],
    "option_chain":   {},
    "error":          None,
    "scan_count":     0,
    "backtest":       {},   # ← filled on startup
}
state_lock = threading.Lock()

# ─── PAPER TRADING STATE ──────────────────────────────────────────────────────
# Persisted to paper_trades.json in the working directory
PAPER_FILE = "paper_trades.json"

paper_state = {
    "open_trade":   None,   # current open paper trade (or None)
    "closed_trades": [],    # all completed paper trades
    "stats": {
        "total": 0, "wins": 0, "losses": 0,
        "total_pnl_pts": 0.0,   # cumulative points P&L
        "capital": 100000,      # ₹1L starting capital (paper)
    }
}
paper_lock = threading.Lock()

# Paper trade config
PAPER_SL_PCT  = 0.004   # 0.4% SL
PAPER_TP_PCT  = 0.014   # 1.4% TP
PAPER_QTY     = 50      # Nifty lot size (futures-equivalent)

def _load_paper():
    global paper_state
    if os.path.exists(PAPER_FILE):
        try:
            with open(PAPER_FILE) as f:
                paper_state = json.load(f)
            log.info(f"📄 Loaded {len(paper_state['closed_trades'])} paper trades from disk")
        except Exception as e:
            log.warning(f"Could not load paper trades: {e}")

def _save_paper():
    try:
        with open(PAPER_FILE, "w") as f:
            json.dump(paper_state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save paper trades: {e}")

def open_paper_trade(direction: str, entry_price: float, score: int, rsi: float, adx: float, strategy: str = "Directional"):
    """Open a new paper trade. Only one open trade at a time."""
    with paper_lock:
        if paper_state["open_trade"] is not None:
            return  # already in a trade
        now = datetime.now(IST)
        sl = round(entry_price * (1 - PAPER_SL_PCT if direction == "BUY" else 1 + PAPER_SL_PCT), 2)
        tp = round(entry_price * (1 + PAPER_TP_PCT if direction == "BUY" else 1 - PAPER_TP_PCT), 2)
        paper_state["open_trade"] = {
            "id":        len(paper_state["closed_trades"]) + 1,
            "strategy":  strategy,
            "direction": direction,
            "entry":     entry_price,
            "sl":        sl,
            "tp":        tp,
            "score":     score,
            "rsi":       rsi,
            "adx":       adx,
            "open_time": now.strftime("%d %b %Y %H:%M:%S IST"),
            "open_date": now.strftime("%d %b %Y"),
            "status":    "OPEN",
            "qty":       PAPER_QTY,
        }
        log.info(f"📄 PAPER TRADE OPENED: {direction} @ {entry_price} | SL {sl} | TP {tp}")
        _save_paper()

def check_paper_trade(current_price: float):
    """Check if open paper trade has hit TP or SL."""
    with paper_lock:
        t = paper_state["open_trade"]
        if t is None:
            return
        now = datetime.now(IST)
        direction = t["direction"]
        hit = None

        if direction == "BUY":
            if current_price <= t["sl"]: hit = "LOSS"
            elif current_price >= t["tp"]: hit = "WIN"
        else:
            if current_price >= t["sl"]: hit = "LOSS"
            elif current_price <= t["tp"]: hit = "WIN"

        # Auto-close at 3:15 PM
        close_time = now.replace(hour=15, minute=15, second=0, microsecond=0)
        if now >= close_time and hit is None:
            hit = "TIMEOUT"

        if hit:
            exit_price = current_price
            pnl_pts    = (exit_price - t["entry"]) * (1 if direction == "BUY" else -1)
            pnl_rs     = round(pnl_pts * t["qty"], 2)

            t["exit"]       = round(exit_price, 2)
            t["exit_time"]  = now.strftime("%d %b %Y %H:%M:%S IST")
            t["result"]     = hit
            t["pnl_pts"]    = round(pnl_pts, 2)
            t["pnl_rs"]     = pnl_rs

            paper_state["closed_trades"].insert(0, t)
            paper_state["open_trade"] = None

            s = paper_state["stats"]
            s["total"]       += 1
            s["total_pnl_pts"] = round(s["total_pnl_pts"] + pnl_pts, 2)
            s["capital"]     = round(s["capital"] + pnl_rs, 2)
            if hit == "WIN":   s["wins"]   += 1
            elif hit == "LOSS": s["losses"] += 1

            icon = "✅" if hit == "WIN" else ("❌" if hit == "LOSS" else "⏱")
            log.info(f"📄 PAPER TRADE CLOSED: {icon} {hit} | Exit {exit_price:.2f} | P&L {pnl_rs:+.2f} ₹")
            _save_paper()

# ─── DATA FETCH — Kite primary, yfinance fallback ────────────────────────────
def _yf_quote():
    ticker = yf.Ticker("^NSEI")
    info   = ticker.fast_info
    price  = float(info.last_price)
    prev   = float(info.previous_close)
    change = price - prev
    pct    = (change / prev) * 100 if prev else 0
    return {"price": round(price,2), "change": round(change,2), "pct": round(pct,2), "prev": round(prev,2)}

def _yf_history():
    df = yf.download("^NSEI", period="1d", interval="5m", progress=False, auto_adjust=True)
    if df.empty:
        df = yf.download("^NSEI", period="5d", interval="5m", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[["Open","High","Low","Close","Volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    last_date = df.index[-1].date()
    return df[df.index.date == last_date]

def _yf_history_multi(days=60):
    df = yf.download("^NSEI", period=f"{days}d", interval="5m", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[["Open","High","Low","Close","Volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df

def fetch_nifty_quote():
    """Real-time from Kite if logged in, else yfinance fallback."""
    if _kite_active():
        try:
            q = _kite_quote()
            log.info(f"  [Kite] Nifty LTP: {q['price']}")
            return q
        except Exception as e:
            log.warning(f"  Kite quote failed, falling back: {e}")
    return _yf_quote()

def fetch_nifty_history():
    """Today's 5-min OHLCV — Kite if logged in, else yfinance."""
    if _kite_active():
        try:
            df = _kite_history()
            log.info(f"  [Kite] {len(df)} bars fetched")
            return df
        except Exception as e:
            log.warning(f"  Kite history failed, falling back: {e}")
    return _yf_history()

def fetch_nifty_multi_day(days=60):
    """Multi-day 5-min data — Kite if logged in, else yfinance."""
    if _kite_active():
        try:
            return _kite_history_multi(days)
        except Exception as e:
            log.warning(f"  Kite multi-day failed, falling back: {e}")
    return _yf_history_multi(days)

def fetch_nse_option_chain():
    """ATM option chain — Kite quote if logged in, else yfinance proxy."""
    if _kite_active():
        try:
            with kite_lock:
                kc = kite_session
            q     = kc.ltp(["NSE:NIFTY 50"])
            price = float(list(q.values())[0]["last_price"])
            atm   = round(price / 50) * 50
            # Fetch ATM CE and PE LTP
            ce_sym = f"NFO:NIFTY{datetime.now(IST).strftime('%y%b').upper()}FUT"
            return {"expiry":"Weekly","atm_strike":atm,"ce_premium":None,"pe_premium":None,"pcr":None,"total_ce_oi":0,"total_pe_oi":0,"source":"kite"}
        except Exception as e:
            log.warning(f"  Kite OC failed: {e}")
    ticker = yf.Ticker("^NSEI")
    try:
        price = float(ticker.fast_info.last_price)
    except:
        price = 23500
    atm = round(price / 50) * 50
    return {"expiry":"Weekly","atm_strike":atm,"ce_premium":None,"pe_premium":None,"pcr":None,"total_ce_oi":0,"total_pe_oi":0,"source":"yfinance"}

# ─── INDICATORS ──────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)

    df["EMA9"]  = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["EMA21"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()
    df["EMA50"] = c.ewm(span=EMA_TREND, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    tp = (h + l + c) / 3
    df["VWAP"] = (tp * v).cumsum() / v.cumsum()
    var        = ((tp - df["VWAP"]) ** 2 * v).cumsum() / v.cumsum()
    vwap_std   = np.sqrt(var)
    df["VWAP_UP"] = df["VWAP"] + vwap_std
    df["VWAP_DN"] = df["VWAP"] - vwap_std

    hl  = h - l
    hc  = (h - c.shift(1)).abs()
    lc  = (l - c.shift(1)).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["ATR"]     = tr.ewm(span=14, adjust=False).mean()
    df["ATR_pct"] = df["ATR"] / c

    pdm = (h - h.shift(1)).clip(lower=0)
    ndm = (l.shift(1) - l).clip(lower=0)
    pdm[pdm < ndm] = 0; ndm[ndm < pdm] = 0
    atr14 = tr.ewm(span=14, adjust=False).mean()
    pdi   = 100 * pdm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    ndi   = 100 * ndm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx    = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()

    df["Vol_MA"] = v.rolling(20).mean()

    bb_mid    = c.rolling(20).mean()
    bb_std    = c.rolling(20).std()
    df["BB_UP"] = bb_mid + 2 * bb_std
    df["BB_DN"] = bb_mid - 2 * bb_std
    df["KC_UP"] = df["EMA21"] + 1.5 * df["ATR"]
    df["KC_DN"] = df["EMA21"] - 1.5 * df["ATR"]

    df["SR_RES"] = h.rolling(20).max().shift(1)
    df["SR_SUP"] = l.rolling(20).min().shift(1)

    df["mom_up"]   = (c > c.shift(1)).rolling(MOMENTUM_BARS).sum() >= MOMENTUM_BARS
    df["mom_down"] = (c < c.shift(1)).rolling(MOMENTUM_BARS).sum() >= MOMENTUM_BARS

    # ── Supertrend ──────────────────────────────────────────────────────────
    atr_st = tr.ewm(span=ST_ATR_PERIOD, adjust=False).mean()
    basic_up = (h + l) / 2 - ST_FACTOR * atr_st
    basic_dn = (h + l) / 2 + ST_FACTOR * atr_st

    st_up = basic_up.copy(); st_dn = basic_dn.copy()
    for i in range(1, len(df)):
        st_up.iloc[i] = max(basic_up.iloc[i], st_up.iloc[i-1]) if c.iloc[i-1] > st_up.iloc[i-1] else basic_up.iloc[i]
        st_dn.iloc[i] = min(basic_dn.iloc[i], st_dn.iloc[i-1]) if c.iloc[i-1] < st_dn.iloc[i-1] else basic_dn.iloc[i]

    st_dir  = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        if c.iloc[i] > st_dn.iloc[i-1]:   st_dir.iloc[i] = 1
        elif c.iloc[i] < st_up.iloc[i-1]: st_dir.iloc[i] = -1
        else:                              st_dir.iloc[i] = st_dir.iloc[i-1]

    df["ST_UP"]  = st_up
    df["ST_DN"]  = st_dn
    df["ST_DIR"] = st_dir

    return df

# ─── SIGNAL ENGINE ───────────────────────────────────────────────────────────
def compute_signals(df: pd.DataFrame) -> dict:
    if len(df) < 55:
        return {"error": "Not enough bars"}

    row  = df.iloc[-1]
    prev = df.iloc[-2]
    c    = row["Close"]
    signals = {}

    # 1. ORB
    orb_high = df["High"].iloc[0]
    orb_low  = df["Low"].iloc[0]
    if c > orb_high:
        signals["orb"] = {"dir":1,  "label":"Bullish", "detail":f"Broke ORB High {orb_high:.0f}"}
    elif c < orb_low:
        signals["orb"] = {"dir":-1, "label":"Bearish", "detail":f"Broke ORB Low {orb_low:.0f}"}
    else:
        signals["orb"] = {"dir":0,  "label":"Neutral", "detail":f"Inside range {orb_low:.0f}–{orb_high:.0f}"}

    # 2. EMA Crossover
    bull_ema = row["EMA9"] > row["EMA21"] and row["EMA21"] > row["EMA50"]
    bear_ema = row["EMA9"] < row["EMA21"] and row["EMA21"] < row["EMA50"]
    if bull_ema:
        signals["ema"] = {"dir":1,  "label":"Bullish", "detail":f"EMA9({row['EMA9']:.0f})>EMA21({row['EMA21']:.0f})>EMA50({row['EMA50']:.0f})"}
    elif bear_ema:
        signals["ema"] = {"dir":-1, "label":"Bearish", "detail":f"EMA9({row['EMA9']:.0f})<EMA21({row['EMA21']:.0f})<EMA50({row['EMA50']:.0f})"}
    else:
        signals["ema"] = {"dir":0,  "label":"Mixed",   "detail":"EMA alignment unclear"}

    # 3. VWAP Band
    if c > row["VWAP_UP"]:
        signals["vwap"] = {"dir":1,  "label":"Bullish", "detail":f"Above VWAP+1σ ({row['VWAP_UP']:.0f})"}
    elif c < row["VWAP_DN"]:
        signals["vwap"] = {"dir":-1, "label":"Bearish", "detail":f"Below VWAP-1σ ({row['VWAP_DN']:.0f})"}
    else:
        signals["vwap"] = {"dir":0,  "label":"Neutral", "detail":f"VWAP: {row['VWAP']:.0f} | Price: {c:.0f}"}

    # 4. S&R Reversal
    sr_tol = 0.003
    near_res = (row["High"] >= row["SR_RES"]*(1-sr_tol)) and (c < row["SR_RES"]*0.999)
    near_sup = (row["Low"]  <= row["SR_SUP"]*(1+sr_tol)) and (c > row["SR_SUP"]*1.001)
    if near_sup:
        signals["sr"] = {"dir":1,  "label":"Bullish", "detail":f"Bouncing off support {row['SR_SUP']:.0f}"}
    elif near_res:
        signals["sr"] = {"dir":-1, "label":"Bearish", "detail":f"Rejected at resistance {row['SR_RES']:.0f}"}
    else:
        signals["sr"] = {"dir":0,  "label":"Neutral", "detail":f"Res:{row['SR_RES']:.0f} | Sup:{row['SR_SUP']:.0f}"}

    # 5. Supertrend
    st_prev_dir = df["ST_DIR"].iloc[-2]
    st_cur_dir  = row["ST_DIR"]
    if st_cur_dir == 1:
        signals["supertrend"] = {"dir":1,  "label":"Bullish", "detail":f"Above ST support {row['ST_UP']:.0f}"}
    elif st_cur_dir == -1:
        signals["supertrend"] = {"dir":-1, "label":"Bearish", "detail":f"Below ST resistance {row['ST_DN']:.0f}"}
    else:
        signals["supertrend"] = {"dir":0,  "label":"Neutral", "detail":"Supertrend flat"}

    # 6. 9:20 Straddle signal — market regime check (sell when low ADX = range-bound)
    adx    = row["ADX"] if not np.isnan(row["ADX"]) else 0
    atr_p  = row["ATR_pct"] if not np.isnan(row["ATR_pct"]) else 0
    # Straddle favours low ADX (sideways), low ATR (calm)
    straddle_favourable = adx < 18 and atr_p < 0.004
    signals["straddle"] = {
        "dir":    0,   # neutral strategy
        "label":  "✅ SELL" if straddle_favourable else "❌ SKIP",
        "detail": f"ADX {adx:.1f} ATR% {atr_p*100:.2f}% → {'Sell straddle ≥9:20 AM' if straddle_favourable else 'Market too directional for straddle'}",
        "active": straddle_favourable
    }

    # 7. Iron Condor signal — needs range-bound + low VIX proxy
    squeeze = (row["BB_UP"] <= row["KC_UP"]) and (row["BB_DN"] >= row["KC_DN"])
    condor_favourable = squeeze and adx < 22
    signals["condor"] = {
        "dir":    0,
        "label":  "✅ SELL" if condor_favourable else "❌ SKIP",
        "detail": f"{'Squeeze active — sell OTM strangle with wing hedges' if condor_favourable else 'No squeeze — condor unfavourable'}",
        "active": condor_favourable
    }

    # ── DIRECTIONAL CONFLUENCE SCORE (strategies 1–5) ───────────────────────
    dir_sigs = ["orb","ema","vwap","sr","supertrend"]
    score    = sum(signals[k]["dir"] for k in dir_sigs)
    vol_ok   = row["Volume"] > row["Vol_MA"] * VOL_MULT if row["Vol_MA"] > 0 else False
    rsi_ok_b = row["RSI"] >= RSI_BULL
    rsi_ok_s = row["RSI"] <= RSI_BEAR
    adx_ok   = row["ADX"] >= ADX_MIN
    mom_up   = bool(row["mom_up"])
    mom_dn   = bool(row["mom_down"])
    no_squeeze_dir = (row["BB_UP"] > row["KC_UP"]) and (row["BB_DN"] < row["KC_DN"])
    atr_ok   = row["ATR_pct"] > 0.0005

    trade = None
    if score >= MIN_SCORE and vol_ok and rsi_ok_b and adx_ok and mom_up and no_squeeze_dir and atr_ok:
        trade = "BUY"
    elif score <= -MIN_SCORE and vol_ok and rsi_ok_s and adx_ok and mom_dn and no_squeeze_dir and atr_ok:
        trade = "SELL"

    return {
        "signals": signals,
        "score":   score,
        "trade":   trade,
        "filters": {
            "vol_ok":    vol_ok,
            "rsi":       round(float(row["RSI"]),1)   if not np.isnan(row["RSI"])   else None,
            "adx":       round(float(row["ADX"]),1)   if not np.isnan(row["ADX"])   else None,
            "momentum":  "↑" if mom_up else ("↓" if mom_dn else "—"),
            "squeeze":   "Expanded ✓" if no_squeeze_dir else "Squeeze ✗",
            "atr_pct":   round(float(row["ATR_pct"])*100, 3),
            "vwap":      round(float(row["VWAP"]),2),
            "ema9":      round(float(row["EMA9"]),2),
            "ema21":     round(float(row["EMA21"]),2),
            "ema50":     round(float(row["EMA50"]),2),
            "sr_res":    round(float(row["SR_RES"]),2) if not np.isnan(row["SR_RES"]) else None,
            "sr_sup":    round(float(row["SR_SUP"]),2) if not np.isnan(row["SR_SUP"]) else None,
            "st_dir":    "Bullish" if row["ST_DIR"]==1 else ("Bearish" if row["ST_DIR"]==-1 else "—"),
            "straddle_ok": signals["straddle"]["active"],
            "condor_ok":   signals["condor"]["active"],
        }
    }

# ─── BACKTESTER ──────────────────────────────────────────────────────────────
def run_backtest():
    """
    Run all 7 strategies on 60 days of real historical data.
    Returns per-strategy win rates + trade counts.
    """
    log.info("📊 Running backtest on 60 days of historical data…")
    results = {}

    try:
        df_raw = fetch_nifty_multi_day(days=60)
        if df_raw.empty or len(df_raw) < 100:
            log.warning("Backtest: not enough data")
            return {}

        # Group by date
        df_raw["date"] = df_raw.index.date
        dates = sorted(df_raw["date"].unique())
        log.info(f"  Backtest: {len(dates)} trading days loaded")

        # ── Per-day OHLC for daily strategies ──────────────────────────────
        daily_df = yf.download("^NSEI", period="70d", interval="1d", progress=False, auto_adjust=True)
        if isinstance(daily_df.columns, pd.MultiIndex):
            daily_df.columns = [c[0] for c in daily_df.columns]
        daily_df = daily_df.dropna()

        # ── Strategy 1–5: Directional confluence (ORB+EMA+VWAP+SR+ST) ──────
        dir_trades = []
        for d in dates:
            day_df = df_raw[df_raw["date"] == d].copy()
            if len(day_df) < 25: continue
            day_df = compute_indicators(day_df)
            # Check signal at 10:00 AM (bar ~10)
            bar_idx = min(10, len(day_df)-2)
            bar  = day_df.iloc[bar_idx]
            rest = day_df.iloc[bar_idx+1:]
            if rest.empty: continue

            # Compute score at this bar
            sigs = {}
            c = bar["Close"]
            orb_h = day_df["High"].iloc[0]; orb_l = day_df["Low"].iloc[0]
            sigs["orb"] = 1 if c>orb_h else (-1 if c<orb_l else 0)
            bull_ema = bar["EMA9"]>bar["EMA21"] and bar["EMA21"]>bar["EMA50"]
            bear_ema = bar["EMA9"]<bar["EMA21"] and bar["EMA21"]<bar["EMA50"]
            sigs["ema"] = 1 if bull_ema else (-1 if bear_ema else 0)
            sigs["vwap"] = 1 if c>bar["VWAP_UP"] else (-1 if c<bar["VWAP_DN"] else 0)
            sr_tol=0.003
            sigs["sr"] = (1 if (bar["Low"]<=bar["SR_SUP"]*(1+sr_tol) and c>bar["SR_SUP"]*1.001)
                          else (-1 if (bar["High"]>=bar["SR_RES"]*(1-sr_tol) and c<bar["SR_RES"]*0.999)
                          else 0))
            sigs["st"] = int(bar["ST_DIR"])

            score = sum(sigs.values())
            vol_ok  = bar["Volume"] > bar["Vol_MA"]*VOL_MULT if bar["Vol_MA"]>0 else False
            adx_ok  = bar["ADX"] >= ADX_MIN if not np.isnan(bar["ADX"]) else False
            rsi_b   = bar["RSI"] >= RSI_BULL if not np.isnan(bar["RSI"]) else False
            rsi_s   = bar["RSI"] <= RSI_BEAR if not np.isnan(bar["RSI"]) else False
            mom_u   = bool(bar["mom_up"])
            mom_d   = bool(bar["mom_down"])
            no_sq   = (bar["BB_UP"]>bar["KC_UP"]) and (bar["BB_DN"]<bar["KC_DN"])
            atr_ok  = bar["ATR_pct"] > 0.0005

            direction = None
            if score>=3 and vol_ok and rsi_b and adx_ok and mom_u and no_sq and atr_ok:
                direction = "BUY"
            elif score<=-3 and vol_ok and rsi_s and adx_ok and mom_d and no_sq and atr_ok:
                direction = "SELL"

            if direction is None: continue

            entry = c
            sl    = entry*(1-0.004) if direction=="BUY" else entry*(1+0.004)
            tp    = entry*(1+0.014) if direction=="BUY" else entry*(1-0.014)

            outcome = "TIMEOUT"
            for _, future_bar in rest.iterrows():
                fc = future_bar["Close"]
                if direction=="BUY":
                    if fc <= sl:  outcome="LOSS"; break
                    if fc >= tp:  outcome="WIN";  break
                else:
                    if fc >= sl:  outcome="LOSS"; break
                    if fc <= tp:  outcome="WIN";  break

            dir_trades.append({"date":str(d), "dir":direction, "outcome":outcome, "entry":entry})

        total   = len(dir_trades)
        wins    = sum(1 for t in dir_trades if t["outcome"]=="WIN")
        losses  = sum(1 for t in dir_trades if t["outcome"]=="LOSS")
        timeout = sum(1 for t in dir_trades if t["outcome"]=="TIMEOUT")
        win_pct = round(wins/total*100,1) if total else 0

        results["directional"] = {
            "name":       "ORB + EMA + VWAP + SR + Supertrend (Confluence)",
            "trades":     total,
            "wins":       wins,
            "losses":     losses,
            "timeouts":   timeout,
            "win_rate":   win_pct,
            "description": "3/5 strategies agree + Volume + RSI + ADX + Momentum filters",
            "sl":         "0.4%", "tp": "1.4%",
        }
        log.info(f"  Directional: {total} trades, {win_pct}% win rate")

        # ── Strategy: Supertrend alone ──────────────────────────────────────
        st_trades = []
        for d in dates:
            day_df = df_raw[df_raw["date"]==d].copy()
            if len(day_df) < 20: continue
            day_df = compute_indicators(day_df)
            for i in range(15, len(day_df)-1):
                cur = day_df.iloc[i]; nxt = day_df.iloc[i+1]
                prev_dir = day_df["ST_DIR"].iloc[i-1]
                cur_dir  = cur["ST_DIR"]
                if cur_dir == prev_dir: continue  # no flip
                direction = "BUY" if cur_dir==1 else "SELL"
                entry = nxt["Open"]
                sl = entry*(1-0.003) if direction=="BUY" else entry*(1+0.003)
                tp = entry*(1+0.009) if direction=="BUY" else entry*(1-0.009)
                outcome = "TIMEOUT"
                for j in range(i+2, min(i+20, len(day_df))):
                    fc = day_df["Close"].iloc[j]
                    if direction=="BUY":
                        if fc<=sl: outcome="LOSS"; break
                        if fc>=tp: outcome="WIN";  break
                    else:
                        if fc>=sl: outcome="LOSS"; break
                        if fc<=tp: outcome="WIN";  break
                st_trades.append({"outcome":outcome})
                break  # one trade per day

        st_total = len(st_trades); st_wins = sum(1 for t in st_trades if t["outcome"]=="WIN")
        st_wr    = round(st_wins/st_total*100,1) if st_total else 0
        results["supertrend"] = {
            "name":       "Supertrend (ATR-10, Factor-3)",
            "trades":     st_total, "wins":st_wins,
            "losses":     sum(1 for t in st_trades if t["outcome"]=="LOSS"),
            "timeouts":   sum(1 for t in st_trades if t["outcome"]=="TIMEOUT"),
            "win_rate":   st_wr,
            "description": "Enter on Supertrend direction flip, 1 trade per day",
            "sl":"0.3%","tp":"0.9%",
        }
        log.info(f"  Supertrend: {st_total} trades, {st_wr}% win rate")

        # ── Strategy: 9:20 Short Straddle ──────────────────────────────────
        # Proxy: sell ATM at 9:20 AM, win if price stays within ATR range
        straddle_trades = []
        for d in dates:
            day_df = df_raw[df_raw["date"]==d].copy()
            if len(day_df) < 15: continue
            # Entry at ~4th bar (9:35-9:40 approx after market open)
            entry_bar = day_df.iloc[min(4,len(day_df)-1)]
            spot      = entry_bar["Close"]
            atr       = day_df["Close"].std() * 0.5  # proxy for daily range
            # Win if end-of-day close within ±1 ATR of entry (straddle collects premium)
            eod_bar   = day_df.iloc[-1]
            move      = abs(eod_bar["Close"] - spot)
            daily_range = day_df["High"].max() - day_df["Low"].min()
            # SL trigger: if intraday move >2% (straddle gets stopped)
            max_move = (day_df["High"].max() - day_df["Low"].min()) / spot
            if max_move > 0.02:
                outcome = "LOSS"
            else:
                outcome = "WIN"  # stayed range-bound, collected full premium
            straddle_trades.append({"outcome":outcome,"move":max_move})

        ss_total = len(straddle_trades)
        ss_wins  = sum(1 for t in straddle_trades if t["outcome"]=="WIN")
        ss_wr    = round(ss_wins/ss_total*100,1) if ss_total else 0
        results["straddle"] = {
            "name":       "9:20 AM Short Straddle (ATM CE+PE sell)",
            "trades":     ss_total, "wins":ss_wins,
            "losses":     ss_total-ss_wins,
            "timeouts":   0,
            "win_rate":   ss_wr,
            "description":"Win if intraday range <2% (market stays sideways). 25% SL on premium.",
            "sl":"25% premium","tp":"Full premium (100%)",
        }
        log.info(f"  Straddle: {ss_total} trades, {ss_wr}% win rate")

        # ── Strategy: Iron Condor ──────────────────────────────────────────
        # Win if price stays within ±1.5% of open price all day
        condor_trades = []
        for d in dates:
            day_df = df_raw[df_raw["date"]==d].copy()
            if len(day_df) < 10: continue
            open_price  = day_df["Open"].iloc[0]
            upper_limit = open_price * (1 + CONDOR_WIDTH_PCT)
            lower_limit = open_price * (1 - CONDOR_WIDTH_PCT)
            breached    = (day_df["High"].max() > upper_limit) or (day_df["Low"].min() < lower_limit)
            outcome     = "LOSS" if breached else "WIN"
            condor_trades.append({"outcome":outcome})

        ic_total = len(condor_trades)
        ic_wins  = sum(1 for t in condor_trades if t["outcome"]=="WIN")
        ic_wr    = round(ic_wins/ic_total*100,1) if ic_total else 0
        results["condor"] = {
            "name":       "Iron Condor (±1.5% OTM wings)",
            "trades":     ic_total, "wins":ic_wins,
            "losses":     ic_total-ic_wins,
            "timeouts":   0,
            "win_rate":   ic_wr,
            "description":"Win if Nifty stays within ±1.5% of open all day. SL = 50% of premium.",
            "sl":"50% premium","tp":"Full premium (100%)",
        }
        log.info(f"  Iron Condor: {ic_total} trades, {ic_wr}% win rate")

        # ── Strategy: ORB alone (15-min window) ────────────────────────────
        orb_trades = []
        for d in dates:
            day_df = df_raw[df_raw["date"]==d].copy()
            if len(day_df) < 20: continue
            day_df = compute_indicators(day_df)
            # 15-min ORB = first 3 bars
            orb_h = day_df["High"].iloc[:3].max()
            orb_l = day_df["Low"].iloc[:3].min()
            # Entry: first bar after ORB that closes outside
            direction = None; entry = None; entry_i = None
            for i in range(3, len(day_df)):
                c = day_df["Close"].iloc[i]
                vol = day_df["Volume"].iloc[i]; vm = day_df["Vol_MA"].iloc[i]
                if c > orb_h and vol > vm*1.2:
                    direction="BUY";  entry=c; entry_i=i; break
                elif c < orb_l and vol > vm*1.2:
                    direction="SELL"; entry=c; entry_i=i; break
            if direction is None: continue

            sl = entry*(1-0.005) if direction=="BUY" else entry*(1+0.005)
            tp = entry*(1+0.015) if direction=="BUY" else entry*(1-0.015)
            outcome = "TIMEOUT"
            for j in range(entry_i+1, len(day_df)):
                fc = day_df["Close"].iloc[j]
                if direction=="BUY":
                    if fc<=sl: outcome="LOSS"; break
                    if fc>=tp: outcome="WIN";  break
                else:
                    if fc>=sl: outcome="LOSS"; break
                    if fc<=tp: outcome="WIN";  break
            orb_trades.append({"outcome":outcome})

        orb_total = len(orb_trades); orb_wins = sum(1 for t in orb_trades if t["outcome"]=="WIN")
        orb_wr    = round(orb_wins/orb_total*100,1) if orb_total else 0
        results["orb"] = {
            "name":       "ORB Standalone (15-min range + volume breakout)",
            "trades":     orb_total, "wins":orb_wins,
            "losses":     sum(1 for t in orb_trades if t["outcome"]=="LOSS"),
            "timeouts":   sum(1 for t in orb_trades if t["outcome"]=="TIMEOUT"),
            "win_rate":   orb_wr,
            "description":"15-min opening range breakout with 1.2× volume confirmation",
            "sl":"0.5%","tp":"1.5%",
        }
        log.info(f"  ORB standalone: {orb_total} trades, {orb_wr}% win rate")

        log.info("✅ Backtest complete")
        return results

    except Exception as e:
        log.error(f"Backtest error: {e}")
        return {"error": str(e)}

# ─── MARKET HOURS ─────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    o = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    c = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return o <= now <= c

def minutes_to_open():
    now = datetime.now(IST)
    o   = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return int((o-now).total_seconds()/60) if now<o else 0

# ─── MAIN SCAN ────────────────────────────────────────────────────────────────
def run_scan():
    log.info("🔍 Running signal scan…")
    now = datetime.now(IST)
    with state_lock:
        state["scan_count"] += 1
        state["last_update"] = now.strftime("%d %b %Y %H:%M:%S IST")
        state["next_scan"]   = (now+timedelta(seconds=SCAN_INTERVAL)).strftime("%H:%M:%S IST")
        state["market_open"] = is_market_open()
        state["error"]       = None

    try:
        quote = fetch_nifty_quote()
        with state_lock:
            state["nifty_price"]  = quote["price"]
            state["nifty_change"] = quote["change"]
            state["nifty_pct"]    = quote["pct"]
        log.info(f"  Nifty 50: {quote['price']} ({quote['pct']:+.2f}%)")
    except Exception as e:
        log.warning(f"  Quote failed: {e}")

    try:
        df = fetch_nifty_history()
        if len(df) < 20: raise ValueError(f"Only {len(df)} bars")
        df = compute_indicators(df)
        result = compute_signals(df)

        candles = [{"t":ts.strftime("%H:%M"),"o":round(float(r["Open"]),2),"h":round(float(r["High"]),2),"l":round(float(r["Low"]),2),"c":round(float(r["Close"]),2)} for ts,r in df.tail(30).iterrows()]

        with state_lock:
            state["signals"]     = result["signals"]
            state["score"]       = result["score"]
            state["trade_alert"] = result["trade"]
            state["indicators"]  = result["filters"]
            state["candles"]     = candles
            if result["trade"]:
                alert = {"time":now.strftime("%H:%M:%S"),"type":result["trade"],"price":state["nifty_price"],"score":result["score"],"rsi":result["filters"]["rsi"],"adx":result["filters"]["adx"]}
                state["alert_history"].insert(0, alert)
                state["alert_history"] = state["alert_history"][:20]
                log.info(f"  🚨 ALERT: {result['trade']} | Score {result['score']}/5")
                # ── Auto open paper trade ─────────────────────────────────
                if state["nifty_price"]:
                    open_paper_trade(
                        direction   = result["trade"],
                        entry_price = state["nifty_price"],
                        score       = result["score"],
                        rsi         = result["filters"].get("rsi") or 0,
                        adx         = result["filters"].get("adx") or 0,
                        strategy    = "Directional Confluence",
                    )
            else:
                log.info(f"  Score: {result['score']}/5 — no trade | Straddle:{'✅' if result['filters']['straddle_ok'] else '❌'} Condor:{'✅' if result['filters']['condor_ok'] else '❌'}")
                # ── Check / close open paper trade ────────────────────────
                if state["nifty_price"]:
                    check_paper_trade(state["nifty_price"])
    except Exception as e:
        log.warning(f"  Chart failed: {e}")
        with state_lock:
            state["error"] = str(e)

    try:
        oc = fetch_nse_option_chain()
        with state_lock: state["option_chain"] = oc
    except Exception as e:
        log.warning(f"  Option chain failed: {e}")

def scan_loop():
    while True:
        try: run_scan()
        except Exception as e: log.error(f"Scan loop error: {e}")
        time.sleep(SCAN_INTERVAL)

# ─── FLASK ───────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty 50 · Signal Scanner v2</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#080a0d;--surf:#0f1114;--surf2:#141720;--border:#1c2028;--green:#00d4aa;--red:#ff4455;--gold:#f5c542;--blue:#4fa3e0;--purple:#a78bfa;--text:#dde3ec;--muted:#525d6e;--radius:6px}
*{margin:0;padding:0;box-sizing:border-box}html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,170,.02) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,170,.02) 1px,transparent 1px);background-size:32px 32px;pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1380px;margin:0 auto;padding:0 20px 60px}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:18px 0 16px;border-bottom:1px solid var(--border);margin-bottom:24px;flex-wrap:wrap;gap:12px}
.brand-title{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:.04em}
.brand-sub{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase}
.mkt-badge{font-family:'DM Mono',monospace;font-size:10px;padding:4px 10px;border-radius:2px;letter-spacing:.1em;text-transform:uppercase;border:1px solid}
.mkt-open{color:var(--green);border-color:rgba(0,212,170,.3);background:rgba(0,212,170,.07)}
.mkt-close{color:var(--muted);border-color:var(--border);background:var(--surf)}
.last-update{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)}
.pulse{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.pulse.live{background:var(--green);animation:pulse 1.4s infinite}
.pulse.off{background:var(--muted)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(0,212,170,.5)}70%{box-shadow:0 0 0 8px rgba(0,212,170,0)}100%{box-shadow:0 0 0 0 rgba(0,212,170,0)}}
.price-hero{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:24px;background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);padding:20px 28px;margin-bottom:20px}
.price-main .label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.15em;margin-bottom:6px}
.price-main .value{font-family:'Bebas Neue',sans-serif;font-size:52px;line-height:1;letter-spacing:.02em}
.price-main .change{font-family:'DM Mono',monospace;font-size:14px;margin-top:4px}
.change.up{color:var(--green)}.change.down{color:var(--red)}
.alert-banner{border-radius:var(--radius);padding:16px 24px;margin-bottom:20px;display:flex;align-items:center;gap:16px;font-family:'DM Mono',monospace;transition:all .3s}
.alert-banner.buy{background:rgba(0,212,170,.08);border:1px solid rgba(0,212,170,.35)}
.alert-banner.sell{background:rgba(255,68,85,.08);border:1px solid rgba(255,68,85,.35)}
.alert-banner.none{background:var(--surf);border:1px solid var(--border)}
.alert-icon{font-size:28px;line-height:1}
.alert-type{font-size:22px;font-family:'Bebas Neue',sans-serif;letter-spacing:.06em}
.alert-type.buy{color:var(--green)}.alert-type.sell{color:var(--red)}.alert-type.none{color:var(--muted)}
.alert-sub{font-size:11px;color:var(--muted);margin-top:2px}
.score-ring{margin-left:auto;display:flex;align-items:center;gap:8px;font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.score-pips{display:flex;gap:4px}
.pip{width:14px;height:14px;border-radius:2px;border:1px solid var(--border)}
.pip.bull{background:var(--green);border-color:var(--green)}
.pip.bear{background:var(--red);border-color:var(--red)}
.pip.neutral{background:var(--border)}
.section-label{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.2em;color:var(--muted);text-transform:uppercase;display:flex;align-items:center;gap:10px;margin-bottom:14px}
.section-label::after{content:'';flex:1;height:1px;background:var(--border)}
.grid-7{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-bottom:14px}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:1100px){.grid-7{grid-template-columns:repeat(4,1fr)}}
@media(max-width:700px){.grid-7,.grid-3,.grid-2{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.grid-7,.grid-3,.grid-2{grid-template-columns:1fr}}
.card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;transition:border-color .2s}
.card:hover{border-color:#2a3040}
.card-head{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-head .title{font-family:'DM Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:.18em;color:var(--muted)}
.card-body{padding:14px}
.card.sig-bull{border-left:3px solid var(--green)}
.card.sig-bear{border-left:3px solid var(--red)}
.card.sig-neutral{border-left:3px solid var(--border)}
.card.sig-sell{border-left:3px solid var(--purple)}
.card.sig-skip{border-left:3px solid var(--border);opacity:.6}
.sig-dir{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:.04em;line-height:1;margin-bottom:4px}
.sig-dir.bull{color:var(--green)}.sig-dir.bear{color:var(--red)}.sig-dir.neutral{color:var(--muted)}.sig-dir.sell{color:var(--purple)}
.sig-detail{font-size:10px;color:var(--muted);font-family:'DM Mono',monospace;line-height:1.5}
.filter-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.filter-item{background:var(--surf2);border-radius:4px;padding:10px 12px}
.filter-item .f-label{font-size:9px;color:var(--muted);font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.12em;margin-bottom:4px}
.filter-item .f-val{font-size:14px;font-weight:600}
.f-val.ok{color:var(--green)}.f-val.warn{color:var(--gold)}.f-val.bad{color:var(--red)}
/* Backtest table */
.bt-table{width:100%;border-collapse:collapse;font-family:'DM Mono',monospace;font-size:12px}
.bt-table th{padding:8px 12px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);border-bottom:1px solid var(--border);background:var(--surf2)}
.bt-table td{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.03)}
.bt-table tr:hover td{background:rgba(255,255,255,.02)}
.wr-bar-wrap{height:6px;background:var(--surf2);border-radius:3px;min-width:80px;overflow:hidden}
.wr-bar{height:100%;border-radius:3px;transition:width .6s ease}
.wr-high{background:var(--green)}.wr-mid{background:var(--gold)}.wr-low{background:var(--red)}
.pill{display:inline-block;padding:2px 8px;border-radius:2px;font-size:10px;font-weight:600}
.pill.buy{background:rgba(0,212,170,.12);color:var(--green);border:1px solid rgba(0,212,170,.25)}
.pill.sell{background:rgba(255,68,85,.1);color:var(--red);border:1px solid rgba(255,68,85,.25)}
.oc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.oc-item{text-align:center}
.oc-item .oc-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}
.oc-item .oc-val{font-size:18px;font-weight:700}
#sparkline{width:100%;height:80px}
.alert-table{width:100%;border-collapse:collapse;font-family:'DM Mono',monospace;font-size:12px}
.alert-table th{padding:8px 12px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);border-bottom:1px solid var(--border);background:var(--surf2)}
.alert-table td{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.03)}
.countdown-bar{height:3px;background:var(--border);border-radius:2px;margin-top:6px;overflow:hidden}
.countdown-fill{height:100%;background:var(--green);transition:width .5s linear}
.error-bar{background:rgba(255,68,85,.08);border:1px solid rgba(255,68,85,.2);border-radius:var(--radius);padding:10px 16px;margin-bottom:16px;font-family:'DM Mono',monospace;font-size:11px;color:#ff9aa5;display:none}
.disclaimer{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);text-align:center;padding:20px 0;border-top:1px solid var(--border);margin-top:40px;line-height:2}
.bt-loading{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);padding:20px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="brand-title">NIFTY 50 · SIGNAL SCANNER <span style="color:var(--green);font-size:18px">v4</span></div>
      <div class="brand-sub">7 Strategies · Zerodha Kite · Built-in Backtester · Auto-Refresh</div>
    </div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <div id="kite-status-badge" class="mkt-badge mkt-close" style="cursor:pointer" onclick="checkKiteStatus()">⬤ Kite: Checking…</div>
      <div id="mkt-badge" class="mkt-badge mkt-close">⬤ Market Closed</div>
      <div class="last-update"><span class="pulse off" id="pulse-dot"></span><span id="last-update-txt">Loading…</span></div>
    </div>
  </div>

  <div id="error-bar" class="error-bar"></div>

  <div class="price-hero">
    <div class="price-main">
      <div class="label">Nifty 50</div>
      <div class="value" id="nifty-price">—</div>
      <div class="change" id="nifty-change">—</div>
    </div>
    <div><canvas id="sparkline"></canvas></div>
    <div style="text-align:right">
      <div style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:6px">NEXT SCAN</div>
      <div id="next-scan" style="font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:.04em">—</div>
      <div class="countdown-bar"><div class="countdown-fill" id="countdown-fill" style="width:100%"></div></div>
    </div>
  </div>

  <div id="alert-banner" class="alert-banner none">
    <div class="alert-icon" id="alert-icon">⏳</div>
    <div class="alert-text">
      <div class="alert-type none" id="alert-type">SCANNING…</div>
      <div class="alert-sub" id="alert-sub">Waiting for high-confluence signal (3/5 directional strategies + Volume + RSI + ADX + Momentum)</div>
    </div>
    <div class="score-ring">
      <div class="score-pips" id="score-pips">
        <div class="pip neutral"></div><div class="pip neutral"></div><div class="pip neutral"></div>
        <div class="pip neutral"></div><div class="pip neutral"></div>
      </div>
      <div id="score-label" style="font-size:13px;color:var(--muted)">0/5</div>
    </div>
  </div>

  <div class="section-label">7 Strategy Signals</div>
  <div class="grid-7" id="signal-cards">
    <div class="card" id="card-orb"><div class="card-head"><span class="title">01·ORB</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-ema"><div class="card-head"><span class="title">02·EMA</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-vwap"><div class="card-head"><span class="title">03·VWAP</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-sr"><div class="card-head"><span class="title">04·S&R</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-supertrend"><div class="card-head"><span class="title">05·Supertrend</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-straddle"><div class="card-head"><span class="title">06·Straddle</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-condor"><div class="card-head"><span class="title">07·Iron Condor</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
  </div>

  <div class="section-label">Confluence Filters</div>
  <div class="card" style="margin-bottom:14px">
    <div class="card-body">
      <div class="filter-row" id="filter-row">
        <div class="filter-item"><div class="f-label">RSI</div><div class="f-val" id="f-rsi">—</div></div>
        <div class="filter-item"><div class="f-label">ADX</div><div class="f-val" id="f-adx">—</div></div>
        <div class="filter-item"><div class="f-label">Volume</div><div class="f-val" id="f-vol">—</div></div>
        <div class="filter-item"><div class="f-label">Momentum</div><div class="f-val" id="f-mom">—</div></div>
        <div class="filter-item"><div class="f-label">Squeeze</div><div class="f-val" id="f-squeeze">—</div></div>
        <div class="filter-item"><div class="f-label">ATR %</div><div class="f-val" id="f-atr">—</div></div>
        <div class="filter-item"><div class="f-label">Supertrend</div><div class="f-val" id="f-st">—</div></div>
        <div class="filter-item"><div class="f-label">Straddle</div><div class="f-val" id="f-straddle">—</div></div>
        <div class="filter-item"><div class="f-label">Condor</div><div class="f-val" id="f-condor">—</div></div>
        <div class="filter-item"><div class="f-label">VWAP</div><div class="f-val" id="f-vwap" style="font-size:13px">—</div></div>
        <div class="filter-item"><div class="f-label">EMA 9/21/50</div><div class="f-val" id="f-ema" style="font-size:11px">—</div></div>
      </div>
    </div>
  </div>

  <!-- BACKTEST RESULTS -->
  <div class="section-label">📊 Backtest Results — 60 Days Real Historical Data</div>
  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><span class="title">Strategy Performance (yfinance Nifty 50 data)</span><span id="bt-period" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">Loading…</span></div>
    <div class="card-body" id="bt-body">
      <div class="bt-loading">⏳ Running backtest on historical data… this takes ~30 seconds on startup</div>
    </div>
  </div>

  <div class="grid-2">
    <div>
      <div class="section-label">Option Chain</div>
      <div class="card">
        <div class="card-head"><span class="title">ATM Strike</span><span id="oc-expiry" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">—</span></div>
        <div class="card-body">
          <div class="oc-grid">
            <div class="oc-item"><div class="oc-label">ATM Strike</div><div class="oc-val" id="oc-strike" style="color:var(--text)">—</div></div>
            <div class="oc-item"><div class="oc-label">CE Premium</div><div class="oc-val" id="oc-ce" style="color:var(--green)">—</div></div>
            <div class="oc-item"><div class="oc-label">PE Premium</div><div class="oc-val" id="oc-pe" style="color:var(--red)">—</div></div>
            <div class="oc-item"><div class="oc-label">PCR</div><div class="oc-val" id="oc-pcr" style="color:var(--gold)">—</div></div>
          </div>
        </div>
      </div>
    </div>
    <div>
      <div class="section-label">Alert History</div>
      <div class="card" style="max-height:260px;overflow-y:auto">
        <table class="alert-table">
          <thead><tr><th>Time</th><th>Signal</th><th>Price</th><th>Score</th><th>RSI</th><th>ADX</th></tr></thead>
          <tbody id="alert-history-body"><tr><td colspan="6" style="color:var(--muted);font-size:11px;padding:16px">No alerts yet</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>


  <!-- PAPER TRADING TRACKER -->
  <div class="section-label">📄 Paper Trade Tracker</div>

  <!-- Stats Bar -->
  <div class="card" style="margin-bottom:10px">
    <div class="card-body">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;align-items:end">
        <div class="filter-item">
          <div class="f-label">Capital</div>
          <div class="f-val" id="pt-capital" style="font-size:16px">₹1,00,000</div>
          <div class="equity-bar"><div class="equity-fill" id="pt-eq-bar" style="width:100%"></div></div>
        </div>
        <div class="filter-item"><div class="f-label">Total Trades</div><div class="f-val" id="pt-total">0</div></div>
        <div class="filter-item"><div class="f-label">Wins</div><div class="f-val ok" id="pt-wins">0</div></div>
        <div class="filter-item"><div class="f-label">Losses</div><div class="f-val bad" id="pt-losses">0</div></div>
        <div class="filter-item"><div class="f-label">Win Rate</div><div class="f-val" id="pt-wr">—</div></div>
        <div class="filter-item"><div class="f-label">Total P&L (pts)</div><div class="f-val" id="pt-pnl-pts">0</div></div>
        <div class="filter-item"><div class="f-label">Total P&L (₹)</div><div class="f-val" id="pt-pnl-rs">₹0</div></div>
        <div style="display:flex;align-items:flex-end">
          <button class="btn btn-reset" onclick="resetPaper()">Reset All</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Open Trade -->
  <div id="pt-open-box" class="pt-open no-trade" style="margin-bottom:10px">
    <div class="pt-row">
      <div class="pt-stat">
        <div class="pt-label">Status</div>
        <div class="pt-val" id="pt-status" style="color:var(--muted)">NO OPEN TRADE</div>
      </div>
      <div class="pt-stat" id="pt-dir-box" style="display:none">
        <div class="pt-label">Direction</div>
        <div class="pt-val" id="pt-dir">—</div>
      </div>
      <div class="pt-stat" id="pt-entry-box" style="display:none">
        <div class="pt-label">Entry</div>
        <div class="pt-val" id="pt-entry">—</div>
      </div>
      <div class="pt-stat" id="pt-sl-box" style="display:none">
        <div class="pt-label">Stop Loss</div>
        <div class="pt-val bad" id="pt-sl">—</div>
      </div>
      <div class="pt-stat" id="pt-tp-box" style="display:none">
        <div class="pt-label">Target</div>
        <div class="pt-val ok" id="pt-tp">—</div>
      </div>
      <div class="pt-stat" id="pt-live-box" style="display:none">
        <div class="pt-label">Live P&L</div>
        <div class="pt-val" id="pt-live-pnl">—</div>
      </div>
      <div class="pt-actions">
        <button class="btn btn-buy"  onclick="manualPaper('BUY')">+ BUY</button>
        <button class="btn btn-sell" onclick="manualPaper('SELL')">+ SELL</button>
        <button class="btn btn-close" id="btn-close-trade" onclick="closePaper()" style="display:none">Close Trade</button>
      </div>
    </div>
  </div>

  <!-- Trade Log -->
  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><span class="title">Trade Log</span><span style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">Paper trades only · No real money</span></div>
    <div style="max-height:320px;overflow-y:auto">
      <table class="pt-table">
        <thead><tr><th>#</th><th>Date</th><th>Strategy</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Result</th><th>P&L pts</th><th>P&L ₹</th><th>Score</th></tr></thead>
        <tbody id="pt-trade-log"><tr><td colspan="10" style="color:var(--muted);padding:16px;text-align:center">No paper trades yet — trades open automatically when scanner fires a signal</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="disclaimer">
    NIFTY 50 SIGNAL SCANNER v4 · 7 Strategies · Zerodha Kite (real-time) / yfinance fallback · Educational Use Only<br>
    Not SEBI registered advice · Options trading involves significant risk · Always use stop-loss
  </div>
</div>

<script>
const REFRESH_MS = 300000;
let nextRefresh  = Date.now() + REFRESH_MS;

function drawSparkline(candles) {
  const canvas = document.getElementById('sparkline');
  if (!canvas || !candles || candles.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 400, H = 80;
  canvas.width = W; canvas.height = H;
  ctx.clearRect(0,0,W,H);
  const closes = candles.map(c=>c.c);
  const mn=Math.min(...closes), mx=Math.max(...closes), range=mx-mn||1;
  const pts = closes.map((v,i)=>({x:(i/(closes.length-1))*W, y:H-((v-mn)/range)*(H-8)-4}));
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(0,212,170,.18)'); grad.addColorStop(1,'rgba(0,212,170,.01)');
  ctx.beginPath(); ctx.moveTo(pts[0].x,H);
  pts.forEach(p=>ctx.lineTo(p.x,p.y));
  ctx.lineTo(pts[pts.length-1].x,H); ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();
  ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));
  ctx.strokeStyle='#00d4aa'; ctx.lineWidth=1.5; ctx.stroke();
}

function renderBacktest(bt) {
  const body = document.getElementById('bt-body');
  if (!bt || Object.keys(bt).length===0) { body.innerHTML='<div class="bt-loading">Backtest data not available</div>'; return; }
  if (bt.error) { body.innerHTML=`<div class="bt-loading" style="color:var(--red)">⚠ ${bt.error}</div>`; return; }

  const order = ['directional','orb','supertrend','straddle','condor'];
  const rows = order.filter(k=>bt[k]).map(k=>{
    const s = bt[k];
    const wr = s.win_rate||0;
    const barCls = wr>=65?'wr-high':wr>=50?'wr-mid':'wr-low';
    const style = `border-left:3px solid ${wr>=65?'var(--green)':wr>=50?'var(--gold)':'var(--red)'}`;
    return `<tr style="${style}">
      <td><b>${s.name}</b><br><span style="color:var(--muted);font-size:10px">${s.description}</span></td>
      <td style="text-align:center">${s.trades}</td>
      <td style="text-align:center"><b style="color:${wr>=65?'var(--green)':wr>=50?'var(--gold)':'var(--red)'}">${wr}%</b></td>
      <td><div class="wr-bar-wrap"><div class="wr-bar ${barCls}" style="width:${Math.min(wr,100)}%"></div></div></td>
      <td style="text-align:center;color:var(--green)">${s.wins}</td>
      <td style="text-align:center;color:var(--red)">${s.losses}</td>
      <td style="font-size:10px;color:var(--muted)">${s.sl||'—'}</td>
      <td style="font-size:10px;color:var(--muted)">${s.tp||'—'}</td>
    </tr>`;
  }).join('');

  body.innerHTML = `<table class="bt-table">
    <thead><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>Bar</th><th>Wins</th><th>Losses</th><th>SL</th><th>TP</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
  document.getElementById('bt-period').textContent = '60-day real data';
}

function render(data) {
  const badge = document.getElementById('mkt-badge');
  const pulse = document.getElementById('pulse-dot');
  if (data.market_open) {
    badge.className='mkt-badge mkt-open'; badge.textContent='⬤ Market Open'; pulse.className='pulse live';
  } else {
    badge.className='mkt-badge mkt-close'; badge.textContent='⬤ Market Closed'; pulse.className='pulse off';
  }
  document.getElementById('last-update-txt').textContent = data.last_update||'—';
  document.getElementById('next-scan').textContent = data.next_scan||'—';

  const errBar = document.getElementById('error-bar');
  if (data.error) { errBar.style.display='block'; errBar.textContent='⚠ '+data.error; }
  else errBar.style.display='none';

  if (data.nifty_price) {
    document.getElementById('nifty-price').textContent = data.nifty_price.toLocaleString('en-IN',{maximumFractionDigits:2});
    const chg=data.nifty_change||0, pct=data.nifty_pct||0;
    const chgEl = document.getElementById('nifty-change');
    chgEl.textContent=`${chg>=0?'+':''}${chg.toFixed(2)} (${pct>=0?'+':''}${pct.toFixed(2)}%)`;
    chgEl.className='change '+(chg>=0?'up':'down');
  }
  drawSparkline(data.candles);

  const trade=data.trade_alert;
  const banner=document.getElementById('alert-banner');
  const aType=document.getElementById('alert-type');
  const aSub=document.getElementById('alert-sub');
  const aIcon=document.getElementById('alert-icon');
  if (trade==='BUY') {
    banner.className='alert-banner buy'; aType.className='alert-type buy';
    aType.textContent='🟢 BUY SIGNAL'; aIcon.textContent='🚀';
    aSub.textContent='Buy ATM CE · SL 0.4% below spot · TP 1.4%';
    document.title='🟢 BUY — Nifty Scanner';
  } else if (trade==='SELL') {
    banner.className='alert-banner sell'; aType.className='alert-type sell';
    aType.textContent='🔴 SELL SIGNAL'; aIcon.textContent='📉';
    aSub.textContent='Buy ATM PE · SL 0.4% above spot · TP 1.4%';
    document.title='🔴 SELL — Nifty Scanner';
  } else {
    banner.className='alert-banner none'; aType.className='alert-type none';
    aType.textContent='NO SIGNAL'; aIcon.textContent='⏳';
    const f=data.indicators||{};
    aSub.textContent=`Score: ${data.score||0}/5 · Straddle: ${f.straddle_ok?'✅':'❌'} · Condor: ${f.condor_ok?'✅':'❌'}`;
    document.title='Nifty 50 · Scanner v2';
  }

  const score=data.score||0;
  const pips=document.querySelectorAll('.pip');
  pips.forEach((p,i)=>{ p.className='pip';
    if(score>0&&i<score) p.classList.add('bull');
    else if(score<0&&i<Math.abs(score)) p.classList.add('bear');
    else p.classList.add('neutral');
  });
  document.getElementById('score-label').textContent=`${score}/5`;

  const sigs=data.signals||{};
  const cardMap={orb:'card-orb',ema:'card-ema',vwap:'card-vwap',sr:'card-sr',supertrend:'card-supertrend',straddle:'card-straddle',condor:'card-condor'};
  for (const [key,cardId] of Object.entries(cardMap)) {
    const sig=sigs[key]||{dir:0,label:'—',detail:'—'};
    const card=document.getElementById(cardId);
    if (!card) continue;
    const isSelling = key==='straddle'||key==='condor';
    const active = sig.active;
    let cls;
    if (isSelling) cls = active?'sell':'skip';
    else cls = sig.dir===1?'bull':sig.dir===-1?'bear':'neutral';
    card.className=`card sig-${cls}`;
    card.querySelector('.sig-dir').className=`sig-dir ${cls}`;
    card.querySelector('.sig-dir').textContent=sig.label||'—';
    card.querySelector('.sig-detail').textContent=sig.detail||'—';
  }

  const f=data.indicators||{};
  const rsi=f.rsi, adx=f.adx;
  const setF=(id,val,cls)=>{ const el=document.getElementById(id); if(el){el.textContent=val;el.className='f-val '+(cls||'')}};
  setF('f-rsi',rsi?rsi.toFixed(1):'—',rsi>=54?'ok':rsi<=46?'ok':'warn');
  setF('f-adx',adx?adx.toFixed(1):'—',adx>=20?'ok':'warn');
  setF('f-vol',f.vol_ok?'✓ High':'✗ Low',f.vol_ok?'ok':'bad');
  setF('f-mom',f.momentum||'—',f.momentum==='↑'||f.momentum==='↓'?'ok':'warn');
  setF('f-squeeze',f.squeeze||'—',f.squeeze&&f.squeeze.includes('✓')?'ok':'bad');
  setF('f-atr',f.atr_pct?f.atr_pct.toFixed(3)+'%':'—',f.atr_pct>0.05?'ok':'warn');
  setF('f-st',f.st_dir||'—',f.st_dir==='Bullish'?'ok':f.st_dir==='Bearish'?'bad':'warn');
  setF('f-straddle',f.straddle_ok?'✅ Sell':'❌ Skip',f.straddle_ok?'ok':'warn');
  setF('f-condor',f.condor_ok?'✅ Sell':'❌ Skip',f.condor_ok?'ok':'warn');
  setF('f-vwap',f.vwap?f.vwap.toLocaleString('en-IN'):'—','');
  setF('f-ema',f.ema9?`${f.ema9.toFixed(0)}/${f.ema21.toFixed(0)}/${f.ema50.toFixed(0)}`:'—','');

  const oc=data.option_chain||{};
  document.getElementById('oc-expiry').textContent=oc.expiry||'—';
  document.getElementById('oc-strike').textContent=oc.atm_strike?oc.atm_strike.toLocaleString('en-IN'):'—';
  document.getElementById('oc-ce').textContent=oc.ce_premium!=null?'₹'+oc.ce_premium:'—';
  document.getElementById('oc-pe').textContent=oc.pe_premium!=null?'₹'+oc.pe_premium:'—';
  document.getElementById('oc-pcr').textContent=oc.pcr!=null?oc.pcr:'—';

  const hist=data.alert_history||[];
  const tbody=document.getElementById('alert-history-body');
  tbody.innerHTML=hist.length===0
    ?'<tr><td colspan="6" style="color:var(--muted);font-size:11px;padding:16px">No alerts yet</td></tr>'
    :hist.map(a=>`<tr><td>${a.time}</td><td><span class="pill ${a.type.toLowerCase()}">${a.type}</span></td><td>${a.price?a.price.toLocaleString('en-IN',{maximumFractionDigits:2}):'—'}</td><td>${a.score}/5</td><td>${a.rsi?a.rsi.toFixed(1):'—'}</td><td>${a.adx?a.adx.toFixed(1):'—'}</td></tr>`).join('');

  // Backtest
  if (data.backtest && Object.keys(data.backtest).length > 0) {
    renderBacktest(data.backtest);
  }
}

async function fetchState() {
  try {
    const res  = await fetch('/api/state');
    const data = await res.json();
    render(data);
  } catch(e) {
    document.getElementById('error-bar').style.display='block';
    document.getElementById('error-bar').textContent='⚠ Cannot reach server';
  }
}

function tickCountdown() {
  const elapsed=(Date.now()-(nextRefresh-REFRESH_MS))/1000;
  const pct=Math.max(0,100-(elapsed/(REFRESH_MS/1000)*100));
  const el=document.getElementById('countdown-fill');
  if(el) el.style.width=pct+'%';
  if(Date.now()>=nextRefresh){ nextRefresh=Date.now()+REFRESH_MS; fetchState(); }
}

if(Notification&&Notification.permission==='default') Notification.requestPermission();
fetchState();
setInterval(tickCountdown,1000);
setInterval(fetchState,30000);

// ── Paper Trading ──────────────────────────────────────────────────────────
async function fetchPaper() {
  try {
    const res  = await fetch('/api/paper');
    const data = await res.json();
    renderPaper(data);
  } catch(e) {}
}

function renderPaper(data) {
  const stats = data.stats || {};
  const open  = data.open_trade;
  const closed = data.closed_trades || [];

  // Stats bar
  const capital  = stats.capital || 100000;
  const startCap = 100000;
  const pnlRs    = round2(capital - startCap);
  const wr       = stats.total > 0 ? Math.round((stats.wins / stats.total) * 100) : 0;
  const eqPct    = Math.min(150, Math.max(10, (capital / startCap) * 100));

  setText('pt-capital', '₹' + capital.toLocaleString('en-IN'));
  setText('pt-total',   stats.total || 0);
  setText('pt-wins',    stats.wins  || 0);
  setText('pt-losses',  stats.losses || 0);
  setText('pt-wr',      stats.total > 0 ? wr + '%' : '—');
  const ptsEl = document.getElementById('pt-pnl-pts');
  if (ptsEl) { ptsEl.textContent = (stats.total_pnl_pts >= 0 ? '+' : '') + (stats.total_pnl_pts || 0).toFixed(1); ptsEl.className = 'f-val ' + (stats.total_pnl_pts >= 0 ? 'ok' : 'bad'); }
  const rsEl = document.getElementById('pt-pnl-rs');
  if (rsEl) { rsEl.textContent = (pnlRs >= 0 ? '+₹' : '-₹') + Math.abs(pnlRs).toLocaleString('en-IN'); rsEl.className = 'f-val ' + (pnlRs >= 0 ? 'ok' : 'bad'); }
  const eqBar = document.getElementById('pt-eq-bar');
  if (eqBar) { eqBar.style.width = Math.min(100, eqPct) + '%'; eqBar.style.background = pnlRs >= 0 ? 'var(--green)' : 'var(--red)'; }

  // Open trade box
  const box = document.getElementById('pt-open-box');
  if (open) {
    const dir   = open.direction;
    box.className = 'pt-open ' + (dir === 'BUY' ? '' : 'sell-open');
    setText('pt-status', '🔴 LIVE PAPER TRADE');
    setText('pt-dir',   dir);
    setText('pt-entry', open.entry ? open.entry.toLocaleString('en-IN', {maximumFractionDigits:2}) : '—');
    setText('pt-sl',    open.sl   ? open.sl.toLocaleString('en-IN',    {maximumFractionDigits:2}) : '—');
    setText('pt-tp',    open.tp   ? open.tp.toLocaleString('en-IN',    {maximumFractionDigits:2}) : '—');
    ['pt-dir-box','pt-entry-box','pt-sl-box','pt-tp-box','pt-live-box'].forEach(id => show(id));
    show('btn-close-trade');
    // Live P&L estimate using Nifty price from state
    const niftyEl = document.getElementById('nifty-price');
    if (niftyEl && open.entry) {
      const cur = parseFloat(niftyEl.textContent.replace(/,/g,''));
      if (!isNaN(cur)) {
        const pts = (cur - open.entry) * (dir === 'BUY' ? 1 : -1);
        const rs  = Math.round(pts * (open.qty || 50));
        const lEl = document.getElementById('pt-live-pnl');
        if (lEl) { lEl.textContent = (rs >= 0 ? '+₹' : '-₹') + Math.abs(rs).toLocaleString('en-IN'); lEl.className = 'pt-val ' + (rs >= 0 ? 'pnl-pos' : 'pnl-neg'); }
      }
    }
  } else {
    box.className = 'pt-open no-trade';
    setText('pt-status', 'NO OPEN TRADE');
    ['pt-dir-box','pt-entry-box','pt-sl-box','pt-tp-box','pt-live-box'].forEach(id => hide(id));
    hide('btn-close-trade');
  }

  // Trade log
  const tbody = document.getElementById('pt-trade-log');
  if (!closed || closed.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted);padding:16px;text-align:center">No paper trades yet — trades open automatically when scanner fires a signal</td></tr>';
  } else {
    tbody.innerHTML = closed.map(t => {
      const resCls = t.result==='WIN' ? 'result-win' : t.result==='LOSS' ? 'result-loss' : t.result==='MANUAL' ? 'result-manual' : 'result-timeout';
      const pnlCls = (t.pnl_rs || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
      return `<tr>
        <td style="color:var(--muted)">#${t.id}</td>
        <td>${t.open_date || '—'}</td>
        <td style="color:var(--muted);font-size:10px">${t.strategy || '—'}</td>
        <td><span class="pill ${(t.direction||'').toLowerCase()}">${t.direction||'—'}</span></td>
        <td>${t.entry ? t.entry.toLocaleString('en-IN',{maximumFractionDigits:2}) : '—'}</td>
        <td>${t.exit  ? t.exit.toLocaleString('en-IN', {maximumFractionDigits:2}) : '—'}</td>
        <td class="${resCls}">${t.result==='WIN'?'✅ WIN':t.result==='LOSS'?'❌ LOSS':t.result==='MANUAL'?'🔵 MANUAL':'⏱ TIMEOUT'}</td>
        <td class="${pnlCls}">${t.pnl_pts >= 0 ? '+' : ''}${(t.pnl_pts||0).toFixed(1)}</td>
        <td class="${pnlCls}">${(t.pnl_rs||0) >= 0 ? '+₹' : '-₹'}${Math.abs(t.pnl_rs||0).toLocaleString('en-IN')}</td>
        <td style="color:var(--muted)">${t.score || 0}/5</td>
      </tr>`;
    }).join('');
  }
}

function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
function round2(n) { return Math.round(n * 100) / 100; }

async function manualPaper(dir) {
  await fetch('/api/paper/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({direction: dir, strategy: 'Manual'})});
  fetchPaper();
}

async function closePaper() {
  await fetch('/api/paper/close', {method:'POST'});
  fetchPaper();
}

async function resetPaper() {
  if (!confirm('Reset all paper trading data? This cannot be undone.')) return;
  await fetch('/api/paper/reset', {method:'POST'});
  fetchPaper();
}

// Poll paper trading every 30s
fetchPaper();
setInterval(fetchPaper, 30000);

// ── Zerodha Kite ───────────────────────────────────────────────────────────
async function checkKiteStatus() {
  const badge = document.getElementById('kite-status-badge');
  try {
    const res  = await fetch('/zerodha/status');
    const data = await res.json();
    if (!data.api_configured) {
      badge.textContent = '⬤ Kite: Not Configured';
      badge.className   = 'mkt-badge mkt-close';
      badge.onclick     = null;
      return;
    }
    if (data.status === 'active') {
      const name = data.profile ? ` · ${data.profile.name.split(' ')[0]}` : '';
      badge.textContent = `⬤ Kite: Live${name}`;
      badge.className   = 'mkt-badge mkt-open';
      badge.title       = 'Click to logout';
      badge.onclick     = kiteLogout;
    } else {
      badge.textContent = '⬤ Kite: Login';
      badge.className   = 'mkt-badge' + ' ' + 'mkt-close';
      badge.style.color      = '#f97316';
      badge.style.borderColor= 'rgba(249,115,22,.4)';
      badge.style.background = 'rgba(249,115,22,.08)';
      badge.title       = 'Click to login via Zerodha';
      badge.onclick     = () => window.location.href = '/zerodha/login';
    }
  } catch(e) {
    badge.textContent = '⬤ Kite: Error';
  }
}

async function kiteLogout() {
  if (!confirm('Logout from Zerodha Kite?')) return;
  await fetch('/zerodha/logout', {method:'POST'});
  checkKiteStatus();
}

// Check Kite status on load + every 5 min
checkKiteStatus();
setInterval(checkKiteStatus, 300000);

</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify(dict(state))

@app.route("/api/scan", methods=["POST"])
def api_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status":"scan triggered"})

@app.route("/api/backtest")
def api_backtest():
    with state_lock:
        return jsonify(state.get("backtest", {}))

# ─── PAPER TRADING API ────────────────────────────────────────────────────────
@app.route("/api/paper")
def api_paper():
    with paper_lock:
        return jsonify(paper_state)

@app.route("/api/paper/open", methods=["POST"])
def api_paper_open():
    """Manually open a paper trade."""
    data = request.get_json() or {}
    direction   = data.get("direction", "BUY")
    entry       = data.get("entry") or (state.get("nifty_price") or 0)
    strategy    = data.get("strategy", "Manual")
    with state_lock:
        price = state["nifty_price"] or entry
    open_paper_trade(direction, price, score=0, rsi=0, adx=0, strategy=strategy)
    return jsonify({"status": "opened", "entry": price})

@app.route("/api/paper/close", methods=["POST"])
def api_paper_close():
    """Manually close the open paper trade at current price."""
    data = request.get_json() or {}
    with state_lock:
        price = state["nifty_price"]
    if price:
        check_paper_trade(price)
        # Force-close if still open (manual override)
        with paper_lock:
            t = paper_state["open_trade"]
            if t:
                pnl_pts = (price - t["entry"]) * (1 if t["direction"]=="BUY" else -1)
                pnl_rs  = round(pnl_pts * t["qty"], 2)
                t.update({"exit": round(price,2), "exit_time": datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST"), "result": "MANUAL", "pnl_pts": round(pnl_pts,2), "pnl_rs": pnl_rs})
                paper_state["closed_trades"].insert(0, t)
                paper_state["open_trade"] = None
                s = paper_state["stats"]
                s["total"] += 1; s["total_pnl_pts"] = round(s["total_pnl_pts"] + pnl_pts, 2)
                s["capital"] = round(s["capital"] + pnl_rs, 2)
                if pnl_rs > 0: s["wins"] += 1
                else: s["losses"] += 1
                _save_paper()
    return jsonify({"status": "closed"})

@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    """Reset all paper trading data."""
    with paper_lock:
        paper_state["open_trade"] = None
        paper_state["closed_trades"] = []
        paper_state["stats"] = {"total":0,"wins":0,"losses":0,"total_pnl_pts":0.0,"capital":100000}
        _save_paper()
    return jsonify({"status": "reset"})


# ─── ZERODHA AUTH ROUTES ──────────────────────────────────────────────────────

@app.route("/zerodha/login")
def zerodha_login():
    """Redirect user to Zerodha login page."""
    if not KITE_API_KEY:
        return """<html><body style="font-family:sans-serif;padding:40px;background:#0f0f0f;color:#fff">
        <h2>⚠️ Kite API not configured</h2>
        <p>Set <code>KITE_API_KEY</code> and <code>KITE_API_SECRET</code> in Railway environment variables.</p>
        <a href="/" style="color:#f97316">← Back to dashboard</a>
        </body></html>""", 400
    if not KITE_AVAILABLE:
        return """<html><body style="font-family:sans-serif;padding:40px;background:#0f0f0f;color:#fff">
        <h2>⚠️ kiteconnect not installed</h2>
        <p>Add <code>kiteconnect</code> to requirements.txt and redeploy.</p>
        <a href="/" style="color:#f97316">← Back to dashboard</a>
        </body></html>""", 500
    kc       = KiteConnect(api_key=KITE_API_KEY)
    login_url = kc.login_url()
    log.info(f"Redirecting to Zerodha login: {login_url}")
    return redirect(login_url)


@app.route("/zerodha/callback")
def zerodha_callback():
    """Zerodha redirects here after login with ?request_token=XXX"""
    global kite_session
    request_token = request.args.get("request_token")
    status        = request.args.get("status", "")

    if status != "success" or not request_token:
        err = request.args.get("message", "Unknown error")
        return f"""<html><body style="font-family:sans-serif;padding:40px;background:#0f0f0f;color:#fff">
        <h2>❌ Zerodha login failed</h2><p>{err}</p>
        <a href="/zerodha/login" style="color:#f97316">Try again</a>
        </body></html>""", 400

    try:
        kc   = KiteConnect(api_key=KITE_API_KEY)
        data = kc.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data["access_token"]
        kc.set_access_token(access_token)

        # Validate
        profile = kc.profile()
        with kite_lock:
            kite_session = kc
        _save_token(access_token)

        name  = profile.get("user_name", "Trader")
        email = profile.get("email", "")
        log.info(f"✅ Zerodha login success: {name} ({email})")

        return f"""<html>
        <head>
          <meta http-equiv="refresh" content="3;url=/" />
          <style>
            body {{ font-family: 'DM Sans', sans-serif; background: #0f0f0f; color: #fff;
                   display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
            .card {{ background:#1a1a1a; border:1px solid #2a2a2a; border-radius:16px;
                     padding:48px; text-align:center; max-width:400px; }}
            .icon {{ font-size:48px; margin-bottom:16px; }}
            h2 {{ margin:0 0 8px; font-size:22px; }}
            p  {{ color:#888; margin:0 0 24px; font-size:14px; }}
            .badge {{ background:#16a34a22; color:#4ade80; border:1px solid #4ade8044;
                      border-radius:8px; padding:6px 16px; font-size:13px; display:inline-block; }}
            a  {{ color:#f97316; font-size:13px; text-decoration:none; }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="icon">✅</div>
            <h2>Connected to Zerodha</h2>
            <p>Welcome, {name}!<br>Real-time data is now active.</p>
            <div class="badge">🟢 Kite session active</div>
            <br><br>
            <a href="/">← Redirecting to dashboard in 3s…</a>
          </div>
        </body></html>"""

    except Exception as e:
        log.error(f"Kite session generation failed: {e}")
        return f"""<html><body style="font-family:sans-serif;padding:40px;background:#0f0f0f;color:#fff">
        <h2>❌ Session generation failed</h2><p>{e}</p>
        <a href="/zerodha/login" style="color:#f97316">Try again</a>
        </body></html>""", 500


@app.route("/zerodha/status")
def zerodha_status():
    """JSON endpoint — current Kite token state."""
    ts = kite_token_status()
    profile = None
    if _kite_active():
        try:
            with kite_lock:
                kc = kite_session
            p = kc.profile()
            profile = {"name": p.get("user_name"), "email": p.get("email"), "broker": p.get("broker")}
        except:
            pass
    return jsonify({**ts, "profile": profile, "kite_available": KITE_AVAILABLE, "api_configured": bool(KITE_API_KEY)})


@app.route("/zerodha/logout", methods=["POST"])
def zerodha_logout():
    """Invalidate the current Kite session."""
    global kite_session
    with kite_lock:
        if kite_session:
            try:
                kite_session.invalidate_access_token()
            except:
                pass
        kite_session = None
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    log.info("Zerodha session logged out")
    return jsonify({"status": "logged_out"})

# ─── STARTUP ─────────────────────────────────────────────────────────────────
def _start_background():
    _load_paper()       # restore saved paper trades
    _load_token()       # restore Kite session if today's token exists
    # Run backtest first (takes ~30s), then start live scanning
    def _bt():
        bt = run_backtest()
        with state_lock:
            state["backtest"] = bt
        run_scan()

    threading.Thread(target=_bt, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    log.info(f"🚀 Nifty Scanner v4 started on port {PORT} | Kite active: {_kite_active()}")

_start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
