"""
╔══════════════════════════════════════════════════════════════╗
║   NIFTY 50 LIVE SIGNAL SCANNER                               ║
║   NSE data → Multi-strategy signal engine → Live dashboard   ║
║                                                              ║
║   Run:  python nifty_scanner.py                              ║
║   Open: http://localhost:5050                                ║
╚══════════════════════════════════════════════════════════════╝

Dependencies:
    pip install requests pandas numpy flask flask-cors

NSE data is fetched from the public NSE India API (no auth needed).
Data is ~15-min delayed during market hours.
"""

import json
import time
import threading
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS

# ─── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ─── CONFIG ─────────────────────────────────────────────────────────────────
IST          = ZoneInfo("Asia/Kolkata")
SCAN_INTERVAL = 300          # seconds between scans (5 min)
import os
PORT          = int(os.environ.get("PORT", 5050))

# Strategy thresholds (tuned for high confluence)
EMA_FAST     = 9
EMA_SLOW     = 21
EMA_TREND    = 50
RSI_PERIOD   = 14
RSI_BULL     = 54
RSI_BEAR     = 46
VOL_MULT     = 1.5
ADX_MIN      = 20
MOMENTUM_BARS= 3
MIN_SCORE    = 3             # out of 4 strategies

# NSE API headers — required to avoid 403
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

# ─── SHARED STATE ────────────────────────────────────────────────────────────
state = {
    "last_update":   None,
    "next_scan":     None,
    "market_open":   False,
    "nifty_price":   None,
    "nifty_change":  None,
    "nifty_pct":     None,
    "signals":       {},       # strategy → signal dict
    "score":         0,
    "trade_alert":   None,     # None | "BUY" | "SELL"
    "alert_history": [],       # list of past alerts
    "indicators":    {},       # latest indicator values
    "candles":       [],       # last 20 5-min candles for mini chart
    "error":         None,
    "scan_count":    0,
}
state_lock = threading.Lock()

# ─── NSE DATA FETCH ──────────────────────────────────────────────────────────
def nse_session():
    """Create a requests session with NSE cookies."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass
    return s

_session = None
_session_ts = 0

def get_session():
    global _session, _session_ts
    if _session is None or (time.time() - _session_ts) > 600:
        _session = nse_session()
        _session_ts = time.time()
    return _session

def fetch_nifty_quote():
    """Fetch current Nifty 50 index quote from NSE."""
    s = get_session()
    url = "https://www.nseindia.com/api/allIndices"
    r = s.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    for item in data.get("data", []):
        if item.get("index") == "NIFTY 50":
            return {
                "price":  float(item["last"]),
                "open":   float(item["open"]),
                "high":   float(item["dayHigh"]),
                "low":    float(item["dayLow"]),
                "change": float(item["change"]),
                "pct":    float(item["percentChange"]),
                "prev":   float(item["previousClose"]),
            }
    raise ValueError("Nifty 50 not found in NSE index data")

def fetch_nifty_history_yf():
    """
    Fallback: fetch recent 5-min Nifty bars via yfinance-style URL
    (NSE doesn't expose 5-min OHLCV publicly without auth).
    We use the NSE chart data endpoint.
    """
    s = get_session()
    # NSE chart data for Nifty 50 intraday
    url = (
        "https://www.nseindia.com/api/chart-databyindex?"
        "index=NIFTY%2050&indices=true"
    )
    r = s.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    # Response: {"grapthData": [[timestamp_ms, price], ...], ...}
    graph = data.get("grapthData") or data.get("graphData") or []
    if not graph:
        raise ValueError("No chart data returned from NSE")

    # Convert to DataFrame
    records = []
    for point in graph:
        ts_ms, price = point[0], point[1]
        records.append({"ts": pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(IST),
                        "Close": float(price)})

    df = pd.DataFrame(records).set_index("ts").sort_index()

    # Resample to 5-min OHLCV (we only have close from chart, synthesise OHLC)
    df5 = df["Close"].resample("5min").ohlc()
    df5.columns = ["Open", "High", "Low", "Close"]
    # Volume: not available from chart endpoint, use synthetic proxy
    df5["Volume"] = 50000
    df5 = df5.dropna()
    return df5

def fetch_nse_option_chain():
    """Fetch ATM strikes from NSE option chain for context."""
    s = get_session()
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    r = s.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    underlying = data["records"]["underlyingValue"]
    expiry_dates = data["records"]["expiryDates"][:2]

    # Get ATM CE/PE premiums for nearest expiry
    nearest_exp = expiry_dates[0] if expiry_dates else None
    atm_strike  = round(underlying / 50) * 50  # round to nearest 50

    ce_premium = pe_premium = pcr = None
    total_ce_oi = total_pe_oi = 0

    for record in data["records"]["data"]:
        if record.get("expiryDate") != nearest_exp:
            continue
        total_ce_oi += record.get("CE", {}).get("openInterest", 0)
        total_pe_oi += record.get("PE", {}).get("openInterest", 0)
        if record.get("strikePrice") == atm_strike:
            ce_premium = record.get("CE", {}).get("lastPrice")
            pe_premium = record.get("PE", {}).get("lastPrice")

    if total_ce_oi > 0:
        pcr = round(total_pe_oi / total_ce_oi, 2)

    return {
        "expiry":      nearest_exp,
        "atm_strike":  atm_strike,
        "ce_premium":  ce_premium,
        "pe_premium":  pe_premium,
        "pcr":         pcr,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
    }

# ─── INDICATOR ENGINE ────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)

    # EMAs
    df["EMA9"]  = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["EMA21"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()
    df["EMA50"] = c.ewm(span=EMA_TREND, adjust=False).mean()

    # RSI
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    # VWAP (cumulative for today's session)
    tp = (h + l + c) / 3
    df["VWAP"] = (tp * v).cumsum() / v.cumsum()

    # VWAP std bands
    variance     = ((tp - df["VWAP"]) ** 2 * v).cumsum() / v.cumsum()
    vwap_std     = np.sqrt(variance)
    df["VWAP_UP"] = df["VWAP"] + vwap_std
    df["VWAP_DN"] = df["VWAP"] - vwap_std

    # ATR
    hl  = h - l
    hc  = (h - c.shift(1)).abs()
    lc  = (l - c.shift(1)).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["ATR"]     = tr.ewm(span=14, adjust=False).mean()
    df["ATR_pct"] = df["ATR"] / c

    # ADX
    pdm = (h - h.shift(1)).clip(lower=0)
    ndm = (l.shift(1) - l).clip(lower=0)
    pdm[pdm < ndm] = 0
    ndm[ndm < pdm] = 0
    atr14   = tr.ewm(span=14, adjust=False).mean()
    pdi     = 100 * pdm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    ndi     = 100 * ndm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx      = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan))
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()

    # Volume MA
    df["Vol_MA"] = v.rolling(20).mean()

    # Bollinger Bands
    bb_mid      = c.rolling(20).mean()
    bb_std      = c.rolling(20).std()
    df["BB_UP"] = bb_mid + 2 * bb_std
    df["BB_DN"] = bb_mid - 2 * bb_std

    # Keltner for squeeze
    df["KC_UP"] = df["EMA21"] + 1.5 * df["ATR"]
    df["KC_DN"] = df["EMA21"] - 1.5 * df["ATR"]

    # S&R
    df["SR_RES"] = h.rolling(20).max().shift(1)
    df["SR_SUP"] = l.rolling(20).min().shift(1)

    # Momentum (consecutive bars)
    df["mom_up"]   = (c > c.shift(1)).rolling(MOMENTUM_BARS).sum() >= MOMENTUM_BARS
    df["mom_down"] = (c < c.shift(1)).rolling(MOMENTUM_BARS).sum() >= MOMENTUM_BARS

    return df

def compute_signals(df: pd.DataFrame) -> dict:
    """Run all 4 strategies on the latest bar. Return signal dict."""
    if len(df) < 55:
        return {"error": "Not enough bars"}

    row  = df.iloc[-1]
    prev = df.iloc[-2]
    c    = row["Close"]

    signals = {}

    # ── 1. ORB ──────────────────────────────────────────────────────────────
    # ORB high/low = first bar's high/low for today
    orb_high = df["High"].iloc[0]
    orb_low  = df["Low"].iloc[0]
    if c > orb_high:
        signals["orb"] = {"dir": 1,  "label": "Bullish", "detail": f"Price {c:.0f} broke ORB High {orb_high:.0f}"}
    elif c < orb_low:
        signals["orb"] = {"dir": -1, "label": "Bearish", "detail": f"Price {c:.0f} broke ORB Low {orb_low:.0f}"}
    else:
        signals["orb"] = {"dir": 0,  "label": "Neutral", "detail": f"Inside ORB range {orb_low:.0f}–{orb_high:.0f}"}

    # ── 2. EMA Crossover ────────────────────────────────────────────────────
    bull_ema = row["EMA9"] > row["EMA21"] and row["EMA21"] > row["EMA50"]
    bear_ema = row["EMA9"] < row["EMA21"] and row["EMA21"] < row["EMA50"]
    if bull_ema:
        signals["ema"] = {"dir": 1,  "label": "Bullish", "detail": f"EMA9({row['EMA9']:.0f}) > EMA21({row['EMA21']:.0f}) > EMA50({row['EMA50']:.0f})"}
    elif bear_ema:
        signals["ema"] = {"dir": -1, "label": "Bearish", "detail": f"EMA9({row['EMA9']:.0f}) < EMA21({row['EMA21']:.0f}) < EMA50({row['EMA50']:.0f})"}
    else:
        signals["ema"] = {"dir": 0,  "label": "Mixed",   "detail": f"EMA alignment unclear"}

    # ── 3. VWAP ─────────────────────────────────────────────────────────────
    if c > row["VWAP_UP"]:
        signals["vwap"] = {"dir": 1,  "label": "Bullish", "detail": f"Price above VWAP+1σ ({row['VWAP_UP']:.0f})"}
    elif c < row["VWAP_DN"]:
        signals["vwap"] = {"dir": -1, "label": "Bearish", "detail": f"Price below VWAP-1σ ({row['VWAP_DN']:.0f})"}
    else:
        signals["vwap"] = {"dir": 0,  "label": "Neutral", "detail": f"VWAP: {row['VWAP']:.0f} | Price: {c:.0f}"}

    # ── 4. S&R ──────────────────────────────────────────────────────────────
    sr_tol = 0.003
    near_res = (row["High"] >= row["SR_RES"] * (1 - sr_tol)) and (c < row["SR_RES"] * 0.999)
    near_sup = (row["Low"]  <= row["SR_SUP"] * (1 + sr_tol)) and (c > row["SR_SUP"] * 1.001)
    if near_sup:
        signals["sr"] = {"dir": 1,  "label": "Bullish", "detail": f"Bouncing off support {row['SR_SUP']:.0f}"}
    elif near_res:
        signals["sr"] = {"dir": -1, "label": "Bearish", "detail": f"Rejected at resistance {row['SR_RES']:.0f}"}
    else:
        signals["sr"] = {"dir": 0,  "label": "Neutral", "detail": f"Res: {row['SR_RES']:.0f} | Sup: {row['SR_SUP']:.0f}"}

    # ── CONFLUENCE FILTERS ───────────────────────────────────────────────────
    score = sum(s["dir"] for s in signals.values())
    vol_ok     = row["Volume"] > row["Vol_MA"] * VOL_MULT if row["Vol_MA"] > 0 else False
    rsi_ok_b   = row["RSI"] >= RSI_BULL
    rsi_ok_s   = row["RSI"] <= RSI_BEAR
    adx_ok     = row["ADX"] >= ADX_MIN
    mom_up     = bool(row["mom_up"])
    mom_dn     = bool(row["mom_down"])
    no_squeeze = (row["BB_UP"] > row["KC_UP"]) and (row["BB_DN"] < row["KC_DN"])
    atr_ok     = row["ATR_pct"] > 0.0005

    # Final trade signal
    trade = None
    if score >= MIN_SCORE and vol_ok and rsi_ok_b and adx_ok and mom_up and no_squeeze and atr_ok:
        trade = "BUY"
    elif score <= -MIN_SCORE and vol_ok and rsi_ok_s and adx_ok and mom_dn and no_squeeze and atr_ok:
        trade = "SELL"

    return {
        "signals":    signals,
        "score":      score,
        "trade":      trade,
        "filters": {
            "vol_ok":     vol_ok,
            "rsi":        round(float(row["RSI"]), 1) if not np.isnan(row["RSI"]) else None,
            "adx":        round(float(row["ADX"]), 1) if not np.isnan(row["ADX"]) else None,
            "momentum":   "↑" if mom_up else ("↓" if mom_dn else "—"),
            "squeeze":    "Expanded ✓" if no_squeeze else "Squeeze ✗",
            "atr_pct":    round(float(row["ATR_pct"]) * 100, 3),
            "vwap":       round(float(row["VWAP"]), 2),
            "ema9":       round(float(row["EMA9"]), 2),
            "ema21":      round(float(row["EMA21"]), 2),
            "ema50":      round(float(row["EMA50"]), 2),
            "sr_res":     round(float(row["SR_RES"]), 2) if not np.isnan(row["SR_RES"]) else None,
            "sr_sup":     round(float(row["SR_SUP"]), 2) if not np.isnan(row["SR_SUP"]) else None,
        }
    }

# ─── MARKET HOURS ────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t

def minutes_to_open():
    now = datetime.now(IST)
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < open_t:
        return int((open_t - now).total_seconds() / 60)
    return 0

# ─── MAIN SCAN LOOP ──────────────────────────────────────────────────────────
def run_scan():
    log.info("🔍 Running signal scan...")
    now = datetime.now(IST)

    with state_lock:
        state["scan_count"] += 1
        state["last_update"] = now.strftime("%d %b %Y %H:%M:%S IST")
        state["next_scan"]   = (now + timedelta(seconds=SCAN_INTERVAL)).strftime("%H:%M:%S IST")
        state["market_open"] = is_market_open()
        state["error"]       = None

    # ── Fetch quote ──
    try:
        quote = fetch_nifty_quote()
        with state_lock:
            state["nifty_price"]  = quote["price"]
            state["nifty_change"] = quote["change"]
            state["nifty_pct"]    = quote["pct"]
        log.info(f"  Nifty 50: {quote['price']} ({quote['pct']:+.2f}%)")
    except Exception as e:
        log.warning(f"  Quote fetch failed: {e}")
        with state_lock:
            state["error"] = f"Quote fetch failed: {e}"

    # ── Fetch intraday chart ──
    try:
        df = fetch_nifty_history_yf()
        if len(df) < 20:
            raise ValueError(f"Only {len(df)} bars — need ≥20")

        df = compute_indicators(df)
        result = compute_signals(df)

        # Candles for sparkline (last 30)
        candles = []
        for ts, row in df.tail(30).iterrows():
            candles.append({
                "t": ts.strftime("%H:%M"),
                "o": round(float(row["Open"]),  2),
                "h": round(float(row["High"]),  2),
                "l": round(float(row["Low"]),   2),
                "c": round(float(row["Close"]), 2),
            })

        with state_lock:
            state["signals"]    = result["signals"]
            state["score"]      = result["score"]
            state["trade_alert"]= result["trade"]
            state["indicators"] = result["filters"]
            state["candles"]    = candles

            if result["trade"]:
                alert = {
                    "time":   now.strftime("%H:%M:%S"),
                    "type":   result["trade"],
                    "price":  state["nifty_price"],
                    "score":  result["score"],
                    "rsi":    result["filters"]["rsi"],
                    "adx":    result["filters"]["adx"],
                }
                state["alert_history"].insert(0, alert)
                state["alert_history"] = state["alert_history"][:20]
                log.info(f"  🚨 TRADE ALERT: {result['trade']} | Score {result['score']}/4")
            else:
                log.info(f"  Score: {result['score']}/4 — no trade")

    except Exception as e:
        log.warning(f"  Chart fetch failed: {e}")
        with state_lock:
            if not state["error"]:
                state["error"] = f"Chart data: {e}"

    # ── Option chain ──
    try:
        oc = fetch_nse_option_chain()
        with state_lock:
            state["option_chain"] = oc
        log.info(f"  OC: ATM {oc['atm_strike']} | CE ₹{oc['ce_premium']} | PE ₹{oc['pe_premium']} | PCR {oc['pcr']}")
    except Exception as e:
        log.warning(f"  Option chain failed: {e}")
        with state_lock:
            state["option_chain"] = {}

def scan_loop():
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
        time.sleep(SCAN_INTERVAL)

# ─── FLASK APP ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty 50 · Live Signal Scanner</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:     #080a0d;
  --surf:   #0f1114;
  --surf2:  #141720;
  --border: #1c2028;
  --green:  #00d4aa;
  --red:    #ff4455;
  --gold:   #f5c542;
  --blue:   #4fa3e0;
  --text:   #dde3ec;
  --muted:  #525d6e;
  --radius: 6px;
}
* { margin:0; padding:0; box-sizing:border-box; }
html { scroll-behavior:smooth; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'DM Sans', sans-serif;
  font-size: 14px;
  min-height: 100vh;
}

/* Grid texture */
body::before {
  content:'';
  position:fixed; inset:0;
  background-image:
    linear-gradient(rgba(0,212,170,.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,170,.025) 1px, transparent 1px);
  background-size: 32px 32px;
  pointer-events:none; z-index:0;
}

.wrap { position:relative; z-index:1; max-width:1280px; margin:0 auto; padding:0 20px 60px; }

/* ── TOP BAR ── */
.topbar {
  display:flex; align-items:center; justify-content:space-between;
  padding: 18px 0 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 24px;
  flex-wrap: wrap; gap:12px;
}
.brand { display:flex; align-items:baseline; gap:10px; }
.brand-title {
  font-family:'Bebas Neue',sans-serif;
  font-size: 28px; letter-spacing:.04em; color:var(--text);
}
.brand-sub {
  font-family:'DM Mono',monospace;
  font-size:10px; color:var(--muted); letter-spacing:.15em; text-transform:uppercase;
}
.topbar-right { display:flex; align-items:center; gap:16px; flex-wrap:wrap; }

.mkt-badge {
  font-family:'DM Mono',monospace; font-size:10px;
  padding:4px 10px; border-radius:2px; letter-spacing:.1em; text-transform:uppercase;
  border:1px solid;
}
.mkt-open  { color:var(--green); border-color:rgba(0,212,170,.3); background:rgba(0,212,170,.07); }
.mkt-close { color:var(--muted); border-color:var(--border); background:var(--surf); }

.last-update { font-family:'DM Mono',monospace; font-size:10px; color:var(--muted); }

/* pulse dot */
.pulse { width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:5px; }
.pulse.live { background:var(--green); box-shadow:0 0 0 0 rgba(0,212,170,.6); animation:pulse 1.4s infinite; }
.pulse.off  { background:var(--muted); }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(0,212,170,.5)} 70%{box-shadow:0 0 0 8px rgba(0,212,170,0)} 100%{box-shadow:0 0 0 0 rgba(0,212,170,0)} }

/* ── NIFTY PRICE HERO ── */
.price-hero {
  display:grid; grid-template-columns:auto 1fr auto;
  align-items:center; gap:24px;
  background:var(--surf); border:1px solid var(--border);
  border-radius:var(--radius); padding:20px 28px; margin-bottom:20px;
}
.price-main .label { font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:.15em; margin-bottom:6px; }
.price-main .value { font-family:'Bebas Neue',sans-serif; font-size:52px; line-height:1; color:var(--text); letter-spacing:.02em; }
.price-main .change { font-family:'DM Mono',monospace; font-size:14px; margin-top:4px; }
.change.up   { color:var(--green); }
.change.down { color:var(--red); }

/* ── TRADE ALERT BANNER ── */
.alert-banner {
  border-radius:var(--radius); padding:16px 24px; margin-bottom:20px;
  display:flex; align-items:center; gap:16px;
  font-family:'DM Mono',monospace;
  transition: all .3s;
}
.alert-banner.buy  { background:rgba(0,212,170,.08); border:1px solid rgba(0,212,170,.35); }
.alert-banner.sell { background:rgba(255,68,85,.08);  border:1px solid rgba(255,68,85,.35); }
.alert-banner.none { background:var(--surf); border:1px solid var(--border); }

.alert-icon { font-size:28px; line-height:1; }
.alert-text .alert-type { font-size:22px; font-family:'Bebas Neue',sans-serif; letter-spacing:.06em; }
.alert-text .alert-type.buy  { color:var(--green); }
.alert-text .alert-type.sell { color:var(--red); }
.alert-text .alert-type.none { color:var(--muted); }
.alert-text .alert-sub { font-size:11px; color:var(--muted); margin-top:2px; }

.score-ring {
  margin-left:auto; display:flex; align-items:center; gap:8px;
  font-family:'DM Mono',monospace; font-size:11px; color:var(--muted);
}
.score-pips { display:flex; gap:4px; }
.pip { width:14px; height:14px; border-radius:2px; border:1px solid var(--border); }
.pip.bull { background:var(--green); border-color:var(--green); }
.pip.bear { background:var(--red);   border-color:var(--red); }
.pip.neutral { background:var(--border); }

/* ── GRID ── */
.grid-3 { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:14px; }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }
@media(max-width:900px) { .grid-3{grid-template-columns:1fr 1fr} }
@media(max-width:600px) { .grid-3,.grid-2{grid-template-columns:1fr} }

/* ── CARDS ── */
.card {
  background:var(--surf); border:1px solid var(--border);
  border-radius:var(--radius); overflow:hidden;
  transition: border-color .2s;
}
.card:hover { border-color: #2a3040; }
.card-head {
  padding:12px 16px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
}
.card-head .title {
  font-family:'DM Mono',monospace; font-size:9px;
  text-transform:uppercase; letter-spacing:.18em; color:var(--muted);
}
.card-body { padding:16px; }

/* signal card accent */
.card.sig-bull { border-left:3px solid var(--green); }
.card.sig-bear { border-left:3px solid var(--red); }
.card.sig-neutral { border-left:3px solid var(--border); }

.sig-dir {
  font-family:'Bebas Neue',sans-serif; font-size:26px; letter-spacing:.04em; line-height:1;
  margin-bottom:4px;
}
.sig-dir.bull { color:var(--green); }
.sig-dir.bear { color:var(--red); }
.sig-dir.neutral { color:var(--muted); }

.sig-detail { font-size:11px; color:var(--muted); font-family:'DM Mono',monospace; line-height:1.5; }

/* ── FILTER GRID ── */
.filter-row {
  display:grid; grid-template-columns: repeat(auto-fit, minmax(130px,1fr));
  gap:10px;
}
.filter-item { background:var(--surf2); border-radius:4px; padding:10px 12px; }
.filter-item .f-label { font-size:9px; color:var(--muted); font-family:'DM Mono',monospace; text-transform:uppercase; letter-spacing:.12em; margin-bottom:4px; }
.filter-item .f-val   { font-size:14px; font-weight:600; }
.f-val.ok   { color:var(--green); }
.f-val.warn { color:var(--gold); }
.f-val.bad  { color:var(--red); }

/* ── OPTION CHAIN ── */
.oc-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
.oc-item { text-align:center; }
.oc-item .oc-label { font-family:'DM Mono',monospace; font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; margin-bottom:4px; }
.oc-item .oc-val   { font-size:18px; font-weight:700; }

/* ── SPARKLINE ── */
#sparkline { width:100%; height:80px; }

/* ── ALERT HISTORY ── */
.alert-table { width:100%; border-collapse:collapse; font-family:'DM Mono',monospace; font-size:12px; }
.alert-table th {
  padding:8px 12px; text-align:left; font-size:9px;
  text-transform:uppercase; letter-spacing:.1em; color:var(--muted);
  border-bottom:1px solid var(--border); background:var(--surf2);
}
.alert-table td { padding:9px 12px; border-bottom:1px solid rgba(255,255,255,.03); }
.alert-table tr:hover td { background:rgba(255,255,255,.02); }
.pill { display:inline-block; padding:2px 8px; border-radius:2px; font-size:10px; font-weight:600; }
.pill.buy  { background:rgba(0,212,170,.12); color:var(--green); border:1px solid rgba(0,212,170,.25); }
.pill.sell { background:rgba(255,68,85,.1);  color:var(--red);   border:1px solid rgba(255,68,85,.25); }

/* ── COUNTDOWN ── */
.countdown-bar { height:3px; background:var(--border); border-radius:2px; margin-top:6px; overflow:hidden; }
.countdown-fill { height:100%; background:var(--green); transition:width .5s linear; }

/* ── ERROR BANNER ── */
.error-bar {
  background:rgba(255,68,85,.08); border:1px solid rgba(255,68,85,.2);
  border-radius:var(--radius); padding:10px 16px; margin-bottom:16px;
  font-family:'DM Mono',monospace; font-size:11px; color:#ff9aa5;
  display:none;
}

/* ── SECTION LABEL ── */
.section-label {
  font-family:'DM Mono',monospace; font-size:9px; letter-spacing:.2em;
  color:var(--muted); text-transform:uppercase;
  display:flex; align-items:center; gap:10px; margin-bottom:14px;
}
.section-label::after { content:''; flex:1; height:1px; background:var(--border); }

/* ── DISCLAIMER ── */
.disclaimer {
  font-family:'DM Mono',monospace; font-size:10px; color:var(--muted);
  text-align:center; padding:20px 0; border-top:1px solid var(--border); margin-top:40px; line-height:2;
}
</style>
</head>
<body>
<div class="wrap">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-title">NIFTY 50 · SIGNAL SCANNER</div>
      <div class="brand-sub">Live · Multi-Strategy · Auto-Refresh</div>
    </div>
    <div class="topbar-right">
      <div id="mkt-badge" class="mkt-badge mkt-close">⬤ Market Closed</div>
      <div class="last-update">
        <span class="pulse off" id="pulse-dot"></span>
        <span id="last-update-txt">Loading…</span>
      </div>
    </div>
  </div>

  <!-- ERROR BANNER -->
  <div id="error-bar" class="error-bar"></div>

  <!-- NIFTY PRICE HERO -->
  <div class="price-hero">
    <div class="price-main">
      <div class="label">Nifty 50</div>
      <div class="value" id="nifty-price">—</div>
      <div class="change" id="nifty-change">— (—%)</div>
    </div>
    <div>
      <canvas id="sparkline"></canvas>
    </div>
    <div style="text-align:right">
      <div style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:6px;">NEXT SCAN</div>
      <div id="next-scan" style="font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:.04em;">—</div>
      <div class="countdown-bar"><div class="countdown-fill" id="countdown-fill" style="width:100%"></div></div>
    </div>
  </div>

  <!-- TRADE ALERT BANNER -->
  <div id="alert-banner" class="alert-banner none">
    <div class="alert-icon" id="alert-icon">⏳</div>
    <div class="alert-text">
      <div class="alert-type none" id="alert-type">SCANNING…</div>
      <div class="alert-sub" id="alert-sub">Waiting for high-confluence signal (3/4 strategies + volume + RSI + ADX + momentum)</div>
    </div>
    <div class="score-ring">
      <div class="score-pips" id="score-pips">
        <div class="pip neutral"></div>
        <div class="pip neutral"></div>
        <div class="pip neutral"></div>
        <div class="pip neutral"></div>
      </div>
      <div id="score-label" style="font-size:13px;color:var(--muted)">0/4</div>
    </div>
  </div>

  <!-- STRATEGY SIGNALS -->
  <div class="section-label">Strategy Signals</div>
  <div class="grid-3" id="signal-cards" style="grid-template-columns:repeat(4,1fr)">
    <div class="card" id="card-orb"><div class="card-head"><span class="title">01 · ORB</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-ema"><div class="card-head"><span class="title">02 · EMA 9/21/50</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-vwap"><div class="card-head"><span class="title">03 · VWAP Band</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
    <div class="card" id="card-sr"><div class="card-head"><span class="title">04 · S&R</span></div><div class="card-body"><div class="sig-dir neutral">—</div><div class="sig-detail">Loading…</div></div></div>
  </div>

  <!-- CONFLUENCE FILTERS -->
  <div class="section-label">Confluence Filters</div>
  <div class="card" style="margin-bottom:14px;">
    <div class="card-body">
      <div class="filter-row" id="filter-row">
        <div class="filter-item"><div class="f-label">RSI</div><div class="f-val" id="f-rsi">—</div></div>
        <div class="filter-item"><div class="f-label">ADX</div><div class="f-val" id="f-adx">—</div></div>
        <div class="filter-item"><div class="f-label">Volume</div><div class="f-val" id="f-vol">—</div></div>
        <div class="filter-item"><div class="f-label">Momentum</div><div class="f-val" id="f-mom">—</div></div>
        <div class="filter-item"><div class="f-label">Squeeze</div><div class="f-val" id="f-squeeze">—</div></div>
        <div class="filter-item"><div class="f-label">ATR %</div><div class="f-val" id="f-atr">—</div></div>
        <div class="filter-item"><div class="f-label">VWAP</div><div class="f-val" id="f-vwap" style="font-size:13px">—</div></div>
        <div class="filter-item"><div class="f-label">EMA 9/21/50</div><div class="f-val" id="f-ema" style="font-size:11px">—</div></div>
      </div>
    </div>
  </div>

  <!-- OPTION CHAIN + ALERTS -->
  <div class="grid-2">
    <div>
      <div class="section-label">Option Chain</div>
      <div class="card">
        <div class="card-head"><span class="title">Nearest Expiry · ATM Strike</span><span id="oc-expiry" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">—</span></div>
        <div class="card-body">
          <div class="oc-grid">
            <div class="oc-item"><div class="oc-label">ATM Strike</div><div class="oc-val" id="oc-strike" style="color:var(--text)">—</div></div>
            <div class="oc-item"><div class="oc-label">CE Premium</div><div class="oc-val" id="oc-ce" style="color:var(--green)">—</div></div>
            <div class="oc-item"><div class="oc-label">PE Premium</div><div class="oc-val" id="oc-pe" style="color:var(--red)">—</div></div>
            <div class="oc-item"><div class="oc-label">PCR</div><div class="oc-val" id="oc-pcr" style="color:var(--gold)">—</div></div>
          </div>
          <div style="margin-top:14px;display:flex;gap:20px;">
            <div style="flex:1">
              <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.1em">Total CE OI</div>
              <div id="oc-ce-oi" style="font-size:13px;font-family:'DM Mono',monospace;color:var(--green)">—</div>
            </div>
            <div style="flex:1">
              <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.1em">Total PE OI</div>
              <div id="oc-pe-oi" style="font-size:13px;font-family:'DM Mono',monospace;color:var(--red)">—</div>
            </div>
            <div style="flex:1">
              <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.1em">PCR Bias</div>
              <div id="oc-bias" style="font-size:13px;font-weight:600">—</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div>
      <div class="section-label">Alert History</div>
      <div class="card" style="max-height:280px;overflow-y:auto;">
        <table class="alert-table">
          <thead><tr><th>Time</th><th>Signal</th><th>Price</th><th>Score</th><th>RSI</th><th>ADX</th></tr></thead>
          <tbody id="alert-history-body">
            <tr><td colspan="6" style="color:var(--muted);font-size:11px;padding:16px">No alerts yet — scanner will trigger when confluence criteria are met</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="disclaimer">
    NIFTY 50 LIVE SIGNAL SCANNER · NSE Data (15-min delayed) · Educational Use Only<br>
    Not SEBI registered advice · Options trading involves significant risk of loss · Always use a stop-loss
  </div>
</div>

<script>
const REFRESH_MS = 300000; // 5 min
let nextRefresh  = Date.now() + REFRESH_MS;
let scanInterval = REFRESH_MS / 1000;

// ── Sparkline canvas ─────────────────────────────────────────────────────────
function drawSparkline(candles) {
  const canvas = document.getElementById('sparkline');
  if (!canvas || !candles || candles.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 400;
  const H = 80;
  canvas.width  = W;
  canvas.height = H;
  ctx.clearRect(0, 0, W, H);

  const closes = candles.map(c => c.c);
  const mn = Math.min(...closes), mx = Math.max(...closes);
  const range = mx - mn || 1;
  const pts = closes.map((v, i) => ({
    x: (i / (closes.length - 1)) * W,
    y: H - ((v - mn) / range) * (H - 8) - 4
  }));

  // Fill
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0,   'rgba(0,212,170,.18)');
  grad.addColorStop(1,   'rgba(0,212,170,.01)');
  ctx.beginPath();
  ctx.moveTo(pts[0].x, H);
  pts.forEach(p => ctx.lineTo(p.x, p.y));
  ctx.lineTo(pts[pts.length-1].x, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
  ctx.strokeStyle = '#00d4aa';
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

// ── Render state ─────────────────────────────────────────────────────────────
function render(data) {
  // Market badge
  const badge = document.getElementById('mkt-badge');
  const pulse = document.getElementById('pulse-dot');
  if (data.market_open) {
    badge.className = 'mkt-badge mkt-open';
    badge.textContent = '⬤ Market Open';
    pulse.className = 'pulse live';
  } else {
    badge.className = 'mkt-badge mkt-close';
    badge.textContent = '⬤ Market Closed';
    pulse.className = 'pulse off';
  }

  // Last update
  document.getElementById('last-update-txt').textContent = data.last_update || '—';
  document.getElementById('next-scan').textContent = data.next_scan || '—';

  // Error
  const errBar = document.getElementById('error-bar');
  if (data.error) {
    errBar.style.display = 'block';
    errBar.textContent = '⚠ ' + data.error;
  } else {
    errBar.style.display = 'none';
  }

  // Price
  if (data.nifty_price) {
    document.getElementById('nifty-price').textContent = data.nifty_price.toLocaleString('en-IN', {maximumFractionDigits:2});
    const chg = data.nifty_change || 0;
    const pct = data.nifty_pct   || 0;
    const chgEl = document.getElementById('nifty-change');
    chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)`;
    chgEl.className = 'change ' + (chg >= 0 ? 'up' : 'down');
  }

  // Sparkline
  drawSparkline(data.candles);

  // Trade alert banner
  const trade = data.trade_alert;
  const banner = document.getElementById('alert-banner');
  const aType  = document.getElementById('alert-type');
  const aSub   = document.getElementById('alert-sub');
  const aIcon  = document.getElementById('alert-icon');
  if (trade === 'BUY') {
    banner.className = 'alert-banner buy';
    aType.className  = 'alert-type buy';
    aType.textContent = '🟢 BUY SIGNAL ACTIVE';
    aIcon.textContent = '🚀';
    aSub.textContent  = `Buy CE · ATM or 1 strike OTM · Set SL at 0.4% below entry on spot`;
    document.title = '🟢 BUY SIGNAL — Nifty Scanner';
  } else if (trade === 'SELL') {
    banner.className = 'alert-banner sell';
    aType.className  = 'alert-type sell';
    aType.textContent = '🔴 SELL SIGNAL ACTIVE';
    aIcon.textContent = '📉';
    aSub.textContent  = `Buy PE · ATM or 1 strike OTM · Set SL at 0.4% above entry on spot`;
    document.title = '🔴 SELL SIGNAL — Nifty Scanner';
  } else {
    banner.className = 'alert-banner none';
    aType.className  = 'alert-type none';
    aType.textContent = 'NO SIGNAL';
    aIcon.textContent = '⏳';
    aSub.textContent  = `Score: ${data.score}/4 · Need ≥3/4 strategies + Volume + RSI + ADX + Momentum`;
    document.title = 'Nifty 50 · Signal Scanner';
  }

  // Score pips
  const score = data.score || 0;
  const pips  = document.querySelectorAll('.pip');
  pips.forEach((p, i) => {
    p.className = 'pip';
    if (score > 0 && i < score)  p.classList.add('bull');
    if (score < 0 && i < Math.abs(score)) p.classList.add('bear');
    if (Math.abs(score) <= i) p.classList.add('neutral');
  });
  document.getElementById('score-label').textContent = `${score}/4`;

  // Strategy signal cards
  const sigs = data.signals || {};
  const cardMap = {orb:'card-orb', ema:'card-ema', vwap:'card-vwap', sr:'card-sr'};
  for (const [key, cardId] of Object.entries(cardMap)) {
    const sig  = sigs[key] || {dir:0, label:'—', detail:'—'};
    const card = document.getElementById(cardId);
    if (!card) continue;
    const cls  = sig.dir === 1 ? 'bull' : sig.dir === -1 ? 'bear' : 'neutral';
    card.className = `card sig-${cls}`;
    card.querySelector('.sig-dir').className = `sig-dir ${cls}`;
    card.querySelector('.sig-dir').textContent = sig.label || '—';
    card.querySelector('.sig-detail').textContent = sig.detail || '—';
  }

  // Confluence filters
  const f = data.indicators || {};
  const rsi = f.rsi;
  const adx = f.adx;
  const setF = (id, val, cls) => {
    const el = document.getElementById(id);
    if (el) { el.textContent = val; el.className = 'f-val ' + (cls||''); }
  };
  setF('f-rsi',    rsi   ? rsi.toFixed(1) : '—',   rsi >= 54 ? 'ok' : rsi <= 46 ? 'ok' : 'warn');
  setF('f-adx',    adx   ? adx.toFixed(1) : '—',   adx >= 20 ? 'ok' : 'warn');
  setF('f-vol',    f.vol_ok ? '✓ High' : '✗ Low',  f.vol_ok  ? 'ok' : 'bad');
  setF('f-mom',    f.momentum || '—',               f.momentum === '↑' || f.momentum === '↓' ? 'ok' : 'warn');
  setF('f-squeeze',f.squeeze || '—',                f.squeeze && f.squeeze.includes('✓') ? 'ok' : 'bad');
  setF('f-atr',    f.atr_pct ? f.atr_pct.toFixed(3)+'%' : '—', f.atr_pct > 0.05 ? 'ok' : 'warn');
  setF('f-vwap',   f.vwap ? f.vwap.toLocaleString('en-IN') : '—', '');
  setF('f-ema',    f.ema9 ? `${f.ema9.toFixed(0)} / ${f.ema21.toFixed(0)} / ${f.ema50.toFixed(0)}` : '—', '');

  // Option chain
  const oc = data.option_chain || {};
  document.getElementById('oc-expiry').textContent  = oc.expiry  || '—';
  document.getElementById('oc-strike').textContent  = oc.atm_strike ? oc.atm_strike.toLocaleString('en-IN') : '—';
  document.getElementById('oc-ce').textContent      = oc.ce_premium  != null ? '₹' + oc.ce_premium  : '—';
  document.getElementById('oc-pe').textContent      = oc.pe_premium  != null ? '₹' + oc.pe_premium  : '—';
  document.getElementById('oc-pcr').textContent     = oc.pcr         != null ? oc.pcr               : '—';
  document.getElementById('oc-ce-oi').textContent   = oc.total_ce_oi ? (oc.total_ce_oi/1e5).toFixed(1)+'L' : '—';
  document.getElementById('oc-pe-oi').textContent   = oc.total_pe_oi ? (oc.total_pe_oi/1e5).toFixed(1)+'L' : '—';
  const pcr = oc.pcr;
  const biasEl = document.getElementById('oc-bias');
  if (pcr) {
    if (pcr > 1.2)      { biasEl.textContent = '🟢 Bullish'; biasEl.style.color = 'var(--green)'; }
    else if (pcr < 0.8) { biasEl.textContent = '🔴 Bearish'; biasEl.style.color = 'var(--red)'; }
    else                { biasEl.textContent = '🟡 Neutral'; biasEl.style.color = 'var(--gold)'; }
  }

  // Alert history
  const hist = data.alert_history || [];
  const tbody = document.getElementById('alert-history-body');
  if (hist.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted);font-size:11px;padding:16px">No alerts yet</td></tr>';
  } else {
    tbody.innerHTML = hist.map(a => `
      <tr>
        <td>${a.time}</td>
        <td><span class="pill ${a.type.toLowerCase()}">${a.type}</span></td>
        <td>${a.price ? a.price.toLocaleString('en-IN',{maximumFractionDigits:2}) : '—'}</td>
        <td>${a.score}/4</td>
        <td>${a.rsi ? a.rsi.toFixed(1) : '—'}</td>
        <td>${a.adx ? a.adx.toFixed(1) : '—'}</td>
      </tr>`).join('');
  }

  // Browser notification on new alert
  if (trade && Notification.permission === 'granted') {
    new Notification(`Nifty 50 — ${trade} Signal`, {
      body: `Score ${data.score}/4 · Price ${data.nifty_price}`,
      icon: trade === 'BUY' ? '🟢' : '🔴'
    });
  }
}

// ── Fetch & refresh ──────────────────────────────────────────────────────────
async function fetchState() {
  try {
    const res  = await fetch('/api/state');
    const data = await res.json();
    render(data);
  } catch (e) {
    document.getElementById('error-bar').style.display = 'block';
    document.getElementById('error-bar').textContent   = '⚠ Cannot reach scanner server. Is Python running?';
  }
}

// Countdown fill
function tickCountdown() {
  const elapsed  = (Date.now() - (nextRefresh - REFRESH_MS)) / 1000;
  const pct      = Math.max(0, 100 - (elapsed / scanInterval * 100));
  const fillEl   = document.getElementById('countdown-fill');
  if (fillEl) fillEl.style.width = pct + '%';
  if (Date.now() >= nextRefresh) {
    nextRefresh = Date.now() + REFRESH_MS;
    fetchState();
  }
}

// Request notification permission
if (Notification && Notification.permission === 'default') {
  Notification.requestPermission();
}

// Initial load + poll every 10 seconds for UI update, fetch from server every 5 min
fetchState();
setInterval(tickCountdown, 1000);
setInterval(fetchState, 30000);   // poll UI every 30s for latest state
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
    """Manual trigger endpoint."""
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "scan triggered"})

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
# ── Start background thread at module load (works with gunicorn + Railway) ──
def _start_background():
    threading.Thread(target=run_scan, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    log.info(f"🚀 Scanner started — dashboard on port {PORT}")

_start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
