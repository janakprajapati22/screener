"""
╔══════════════════════════════════════════════════════════════════╗
║   NIFTY 50 · OPTIONS SCANNER v5                                  ║
║   Intraday Options Paper Trading · Zerodha Kite Connect          ║
║                                                                  ║
║   Features:                                                      ║
║     • Live Nifty price (3-sec poll via Kite LTP)                ║
║     • Option chain: ATM CE/PE LTP live                          ║
║     • Market mood engine (Trending/Sideways/Choppy)             ║
║     • Auto strategy selection based on mood                     ║
║     • Options paper trading (CE/PE by premium)                  ║
║     • Auto square-off at 3:15 PM                                ║
║     • Auto-login via TOTP on startup                            ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, time, threading, logging, hashlib
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_PG = True
except ImportError:
    HAS_PG = False
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
from flask import Flask, jsonify, render_template_string, request, redirect
from flask_cors import CORS

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

try:
    from kiteconnect import KiteConnect, KiteTicker
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scanner")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
IST           = ZoneInfo("Asia/Kolkata")
PORT          = int(os.environ.get("PORT", 5050))
SCAN_INTERVAL = 300       # full signal scan every 5 min
PRICE_INTERVAL = 3        # price + option LTP poll every 3 sec
# Nifty lot size — fetched dynamically from Kite instruments, fallback to known value
LOT_SIZE      = 65        # Fallback — overridden at runtime by fetch_lot_size()


def _est_premium(spot: float, dte: int = 3, iv_pct: float = 22.0) -> float:
    """
    Estimate ATM option premium using simplified Black-Scholes.
    ATM premium ≈ spot * IV * sqrt(DTE/365) * 0.4
    Default IV=22% — back-solved from real Nifty weekly options (Mar 2026).
    e.g. Nifty 23450 PE at DTE=4, IV=23% → ₹~229 actual vs ₹229 estimate ✓
    Old method (spot*1.5%) gave ₹351 — 53% overestimate.
    """
    import math
    iv   = iv_pct / 100.0
    t    = max(dte, 0.5) / 365.0
    prem = spot * iv * math.sqrt(t) * 0.4
    return round(max(prem, 10.0), 2)  # floor ₹10

def _dte(trade_date) -> int:
    """Days to expiry from trade_date."""
    from datetime import date as date_type
    expiry = _get_expiry_for_date(trade_date)
    if isinstance(trade_date, date_type):
        return max((expiry.date() - trade_date).days, 0)
    return max((expiry.date() - trade_date.date()).days, 0)

def fetch_lot_size() -> int:
    """Fetch current Nifty lot size from Kite instruments API."""
    global LOT_SIZE
    try:
        instruments = kite.instruments("NFO")
        for inst in instruments:
            if inst.get("name") == "NIFTY" and inst.get("instrument_type") == "CE":
                lot = int(inst.get("lot_size", 0))
                if lot > 0:
                    LOT_SIZE = lot
                    log.info(f"✅ Lot size fetched from Kite: {LOT_SIZE}")
                    return lot
    except Exception as e:
        log.warning(f"Could not fetch lot size from Kite: {e}")
    log.info(f"Using fallback lot size: {LOT_SIZE}")
    return LOT_SIZE

# Signal thresholds
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 50
RSI_PERIOD = 14
ADX_MIN    = 20
MIN_SCORE  = 4   # raised from 3 → better signal quality, fewer false entries

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Paper trade option SL/TP (% of premium)
OPTION_SL_PCT    = 0.35   # SL: exit if premium drops 35% (tighter than 40%)
OPTION_TP_PCT    = 0.80   # TP: exit if premium gains 80%
STRADDLE_SL_PCT  = 0.25   # Straddle SL: exit if premium rises 25%
STRADDLE_TP_PCT  = 0.50   # Straddle TP: collect 50% premium decay
TRAILING_SL      = True   # Enable trailing SL
# Dynamic trailing levels: (profit_threshold, lock_in_gain)
TRAILING_LEVELS  = [(0.40, 0.10), (0.60, 0.25), (1.00, 0.50)]

# Risk guardrails
MAX_DAILY_LOSS_PCT = 0.03   # Stop trading at -3% of capital
MAX_TRADES_DAY     = 3      # Max 3 trades per day
TRADE_COOLDOWN_MIN = 10     # 10 min between trades
MIN_PREMIUM        = 80     # Skip options below ₹80 premium
SLIPPAGE_PCT       = 0.005  # 0.5% slippage each side simulation

# ─── ZERODHA CONFIG ───────────────────────────────────────────────────────────
RAILWAY_URL      = os.environ.get("RAILWAY_URL", "https://nifty-screener-production.up.railway.app")
KITE_API_KEY     = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET  = os.environ.get("KITE_API_SECRET", "")
KITE_TOTP_SECRET = os.environ.get("KITE_TOTP_SECRET", "")
KITE_USER_ID     = os.environ.get("KITE_USER_ID", "")
KITE_PASSWORD    = os.environ.get("KITE_PASSWORD", "")
TOKEN_FILE       = "kite_token.json"
NIFTY_TOKEN      = 256265   # NSE:NIFTY 50

kite_session = None
kite_lock    = threading.Lock()

def _kite_active():
    with kite_lock:
        return kite_session is not None

def _save_token(access_token):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": today}, f)

def _load_token():
    global kite_session
    if not KITE_AVAILABLE or not KITE_API_KEY:
        return False
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        if data.get("date") != datetime.now(IST).strftime("%Y-%m-%d"):
            return False
        kc = KiteConnect(api_key=KITE_API_KEY)
        kc.set_access_token(data["access_token"])
        kc.profile()
        with kite_lock:
            kite_session = kc
        log.info("✅ Kite session restored from saved token")
        return True
    except Exception as e:
        log.warning(f"Token load failed: {e}")
        return False

def _auto_login():
    global kite_session
    if not all([KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET]):
        log.warning("Auto-login skipped — set KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in Railway vars")
        return False
    if not KITE_AVAILABLE or not PYOTP_AVAILABLE:
        return False
    try:
        log.info("🤖 Auto-login: starting...")
        kc      = KiteConnect(api_key=KITE_API_KEY)
        sess    = requests.Session()

        # Step 1: credentials
        r1 = sess.post("https://kite.zerodha.com/api/login", data={
            "user_id": KITE_USER_ID, "password": KITE_PASSWORD
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        d1 = r1.json()
        if d1.get("status") != "success":
            log.error(f"Auto-login credentials failed: {d1.get('message')}")
            return False
        request_id = d1["data"]["request_id"]

        # Step 2: TOTP
        totp = pyotp.TOTP(KITE_TOTP_SECRET).now()
        r2 = sess.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": KITE_USER_ID, "request_id": request_id,
            "twofa_value": totp, "twofa_type": "totp"
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        d2 = r2.json()
        if d2.get("status") != "success":
            log.error(f"Auto-login TOTP failed: {d2.get('message')}")
            return False

        # Step 3: get request_token
        import urllib.parse as up
        login_url = kc.login_url()
        r3 = sess.get(login_url, allow_redirects=False, timeout=15)
        location  = r3.headers.get("Location", "")
        if not location:
            r3b      = sess.get(login_url, timeout=15)
            location = r3b.url
        params        = up.parse_qs(up.urlparse(location).query)
        request_token = params.get("request_token", [None])[0]
        if not request_token:
            log.error(f"Auto-login: no request_token in: {location[:150]}")
            return False

        # Step 4: generate session
        data         = kc.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data["access_token"]
        kc.set_access_token(access_token)
        profile = kc.profile()
        with kite_lock:
            kite_session = kc
        _save_token(access_token)
        log.info(f"✅ Auto-login success: {profile.get('user_name')}")
        fetch_lot_size()
        return True
    except Exception as e:
        log.error(f"Auto-login error: {e}")
        return False

# ─── KITE DATA ─────────────────────────────────────────────────────────────────
# Cache yesterday's close so we don't re-fetch every tick
_prev_close_cache = {"date": None, "close": None}

def _get_prev_close() -> float:
    """Fetch yesterday's close price, cached per day."""
    global _prev_close_cache
    today = datetime.now(IST).date()
    if _prev_close_cache["date"] == today and _prev_close_cache["close"]:
        return _prev_close_cache["close"]
    try:
        with kite_lock:
            kc = kite_session
        # Fetch last 5 daily candles to get yesterday's close
        now     = datetime.now(IST)
        from_dt = now - timedelta(days=7)
        records = kc.historical_data(NIFTY_TOKEN, from_dt, now, "day", continuous=False)
        if records and len(records) >= 2:
            # Second last record = yesterday (last = today or latest)
            prev_close = float(records[-2]["close"]) if len(records) >= 2 else float(records[-1]["close"])
        elif records:
            prev_close = float(records[-1]["close"])
        else:
            return 0.0
        _prev_close_cache = {"date": today, "close": prev_close}
        log.info(f"  Prev close cached: {prev_close}")
        return prev_close
    except Exception as e:
        log.warning(f"Prev close fetch failed: {e}")
        return 0.0

def kite_ltp_nifty():
    """Fetch Nifty spot LTP with accurate prev close."""""
    with kite_lock:
        kc = kite_session
    data  = kc.ltp(["NSE:NIFTY 50"])
    val   = list(data.values())[0]
    price = float(val["last_price"])
    # Try ohlc.close first (works during market hours)
    ohlc  = val.get("ohlc", {})
    prev  = float(ohlc.get("close", 0)) or _get_prev_close() or price
    chg   = price - prev
    pct   = (chg / prev * 100) if prev else 0
    return {"price": round(price, 2), "change": round(chg, 2), "pct": round(pct, 2), "prev": round(prev, 2)}

def kite_history_today():
    """5-min OHLCV bars for today."""
    with kite_lock:
        kc = kite_session
    now     = datetime.now(IST)
    from_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    records = kc.historical_data(NIFTY_TOKEN, from_dt, now, "5minute", continuous=False)
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df = df.rename(columns={"date": "datetime", "open": "Open", "high": "High",
                             "low": "Low", "close": "Close", "volume": "Volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def kite_history_multi(days=60):
    """Multi-day 5-min bars for backtesting."""
    with kite_lock:
        kc = kite_session
    now     = datetime.now(IST)
    from_dt = now - timedelta(days=days)
    records = kc.historical_data(NIFTY_TOKEN, from_dt, now, "5minute", continuous=False)
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df = df.rename(columns={"date": "datetime", "open": "Open", "high": "High",
                             "low": "Low", "close": "Close", "volume": "Volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def kite_option_ltp(symbol: str) -> float:
    """Fetch LTP for a single NFO option symbol."""
    try:
        with kite_lock:
            kc = kite_session
        data = kc.ltp([symbol])
        val  = list(data.values())[0]
        return round(float(val["last_price"]), 2)
    except Exception as e:
        log.warning(f"Option LTP failed for {symbol}: {e}")
        return 0.0

def fetch_option_ltp_batch(symbols: list) -> dict:
    """Fetch multiple option LTPs in one API call."""
    try:
        with kite_lock:
            kc = kite_session
        data   = kc.ltp(symbols)
        result = {}
        for sym in symbols:
            matched = False
            for k, v in data.items():
                if sym.replace("NFO:", "") in k or k in sym:
                    result[sym] = round(float(v["last_price"]), 2)
                    matched = True
                    break
            if not matched:
                result[sym] = 0.0
        return result
    except Exception as e:
        log.warning(f"Batch option LTP failed: {e}")
        return {s: 0.0 for s in symbols}

# NSE market holidays 2026 (verified)
NSE_HOLIDAYS = {
    # 2026
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-31",  # Id-Ul-Fitr
    "2026-04-02",  # Ram Navami
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Diwali (Laxmi Pujan)
    "2026-10-21",  # Diwali (Balipratipada)
    "2026-11-04",  # Gurunanak Jayanti
    "2026-12-25",  # Christmas
    # 2025 (for backtesting)
    "2025-01-26","2025-02-26","2025-03-14","2025-03-31",
    "2025-04-10","2025-04-14","2025-04-18","2025-05-01",
    "2025-08-15","2025-08-27","2025-10-02","2025-10-21",
    "2025-10-22","2025-11-05","2025-12-25",
}

def get_weekly_expiry_str() -> str:
    """
    Return nearest Nifty weekly expiry in Kite symbol format.
    Nifty expiry = Tuesday (moved from Thursday in 2025).
    If Tuesday is a holiday → Monday.
    If Monday also holiday → previous Friday.
    """
    now  = datetime.now(IST)
    # Find next Tuesday (weekday 1)
    days_to_tue = (1 - now.weekday()) % 7
    if days_to_tue == 0 and now.hour >= 15:
        days_to_tue = 7   # today's expiry already past, move to next week
    expiry = now + timedelta(days=days_to_tue)

    # Holiday fallback: Tuesday → Monday → Friday
    for fallback_days in [0, -1, -4]:
        candidate = expiry + timedelta(days=fallback_days)
        if candidate.strftime("%Y-%m-%d") not in NSE_HOLIDAYS and candidate.weekday() < 5:
            expiry = candidate
            break

    month_map = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                 7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
    yy  = expiry.strftime("%y")
    mon = month_map[expiry.month]
    dd  = expiry.strftime("%d")
    return f"{yy}{mon}{dd}"

def is_expiry_day() -> bool:
    """True if today is Nifty weekly expiry day."""""
    now    = datetime.now(IST)
    expiry = get_weekly_expiry_str()
    # Parse expiry string back to date for comparison
    month_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                 "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    try:
        yy  = int("20" + expiry[:2])
        mon = month_map[expiry[2:5]]
        dd  = int(expiry[5:])
        exp_date = datetime(yy, mon, dd, tzinfo=IST).date()
        return now.date() == exp_date
    except:
        return False

def _get_expiry_for_date(trade_date) -> datetime:
    """Get the weekly expiry date for a given trade date (for backtesting)."""
    from datetime import date as date_type
    if isinstance(trade_date, date_type):
        d = datetime(trade_date.year, trade_date.month, trade_date.day, tzinfo=IST)
    else:
        d = trade_date
    days_to_tue = (1 - d.weekday()) % 7
    if days_to_tue == 0:
        days_to_tue = 7  # already Tuesday — use next week's expiry
    expiry = d + timedelta(days=days_to_tue)
    for fb in [0, -1, -4]:
        candidate = expiry + timedelta(days=fb)
        if candidate.strftime("%Y-%m-%d") not in NSE_HOLIDAYS and candidate.weekday() < 5:
            expiry = candidate
            break
    return expiry

def build_option_symbol(strike: int, opt_type: str, trade_date=None) -> str:
    """
    Build Kite NFO weekly option symbol.
    Kite format: NIFTY + YY + M + DD + STRIKE + CE/PE
    where M = 1-9 for Jan-Sep, O=Oct, N=Nov, D=Dec
    e.g. NIFTY2631623650CE = 2026, Mar(3), 16th, 23650 CE
    trade_date: if provided, compute expiry relative to that date (for backtesting)
    """
    if trade_date is not None:
        expiry = _get_expiry_for_date(trade_date)
    else:
        now  = datetime.now(IST)
        days_to_tue = (1 - now.weekday()) % 7
        if days_to_tue == 0 and now.hour >= 15:
            days_to_tue = 7
        expiry = now + timedelta(days=days_to_tue)
        for fb in [0, -1, -4]:
            candidate = expiry + timedelta(days=fb)
            if candidate.strftime("%Y-%m-%d") not in NSE_HOLIDAYS and candidate.weekday() < 5:
                expiry = candidate
                break
    yy  = expiry.strftime("%y")
    dd  = expiry.strftime("%d")
    month_code = {1:"1",2:"2",3:"3",4:"4",5:"5",6:"6",
                  7:"7",8:"8",9:"9",10:"O",11:"N",12:"D"}
    m = month_code[expiry.month]
    return f"NFO:NIFTY{yy}{m}{dd}{strike}{opt_type}"

def _expiry_str_for_date(trade_date) -> str:
    """Human readable expiry string for a trade date."""
    expiry = _get_expiry_for_date(trade_date)
    return expiry.strftime("%d %b %Y")

def fetch_atm_options(spot_price: float, extra_syms: list = None) -> dict:
    """
    Fetch ATM CE/PE LTP + any extra symbols (open trade exact strikes).
    extra_syms: list of NFO: symbols for open paper trades — fetched in same batch.
    """
    atm        = round(spot_price / 50) * 50
    ce_sym     = build_option_symbol(atm,       "CE")
    pe_sym     = build_option_symbol(atm,       "PE")
    otm_ce_sym = build_option_symbol(atm + 100, "CE")
    otm_pe_sym = build_option_symbol(atm - 100, "PE")
    batch      = [ce_sym, pe_sym, otm_ce_sym, otm_pe_sym]
    # Add exact open trade symbols to batch (no extra API call!)
    if extra_syms:
        for s in extra_syms:
            full = s if s.startswith("NFO:") else f"NFO:{s}"
            if full not in batch:
                batch.append(full)
    log.info(f"  Fetching options batch: {len(batch)} symbols")
    ltps       = fetch_option_ltp_batch(batch)
    ce_ltp     = ltps.get(ce_sym, 0.0)
    pe_ltp     = ltps.get(pe_sym, 0.0)
    otm_ce_ltp = ltps.get(otm_ce_sym, 0.0)
    otm_pe_ltp = ltps.get(otm_pe_sym, 0.0)
    return {
        "atm": atm,
        "ce_ltp": ce_ltp, "pe_ltp": pe_ltp,
        "ce_sym": ce_sym, "pe_sym": pe_sym,
        "otm_ce_ltp": otm_ce_ltp, "otm_pe_ltp": otm_pe_ltp,
        "otm_ce_sym": otm_ce_sym, "otm_pe_sym": otm_pe_sym,
        "expiry": get_weekly_expiry_str(),
        "straddle_premium": round(ce_ltp + pe_ltp, 2),
        "all_ltps": ltps,   # full map: symbol → ltp for exact strike lookup
    }

# ─── VIX + OI + PCR ──────────────────────────────────────────────────────────
VIX_TOKEN = 264969  # NSE:INDIA VIX

def fetch_vix() -> float:
    """Fetch India VIX."""
    try:
        with kite_lock:
            kc = kite_session
        data = kc.ltp(["NSE:INDIA VIX"])
        val  = list(data.values())[0]
        return round(float(val["last_price"]), 2)
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")
        return 0.0

def fetch_oi_pcr(spot_price: float) -> dict:
    """
    Fetch OI for ATM strikes and calculate PCR.
    Returns: atm_ce_oi, atm_pe_oi, pcr, max_pain
    """
    try:
        with kite_lock:
            kc = kite_session
        atm = round(spot_price / 50) * 50
        # Fetch OI for 5 strikes each side
        strikes = [atm + (i * 50) for i in range(-5, 6)]
        ce_syms = [build_option_symbol(s, "CE") for s in strikes]
        pe_syms = [build_option_symbol(s, "PE") for s in strikes]
        all_syms = ce_syms + pe_syms

        # Use kite.quote() — ltp() does NOT return OI data
        data = kc.quote(all_syms)

        total_ce_oi = 0
        total_pe_oi = 0
        strike_oi   = {}  # for max pain calc

        for sym, val in data.items():
            oi = val.get("oi", 0) or 0
            lp = val.get("last_price", 0) or 0
            if "CE" in sym:
                total_ce_oi += oi
            elif "PE" in sym:
                total_pe_oi += oi

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        # ATM OI specifically
        atm_ce_sym = build_option_symbol(atm, "CE").replace("NFO:", "")
        atm_pe_sym = build_option_symbol(atm, "PE").replace("NFO:", "")
        atm_ce_oi  = 0
        atm_pe_oi  = 0
        for sym, val in data.items():
            if atm_ce_sym in sym: atm_ce_oi = val.get("oi", 0) or 0
            if atm_pe_sym in sym: atm_pe_oi = val.get("oi", 0) or 0

        # ── IV: fetch quote (not ltp) for ATM CE to get implied_volatility ──
        atm_iv_ce = 0.0
        atm_iv_pe = 0.0
        try:
            atm_ce_full = build_option_symbol(atm, "CE")
            atm_pe_full = build_option_symbol(atm, "PE")
            quote_data  = kc.quote([atm_ce_full, atm_pe_full])
            for sym, val in quote_data.items():
                iv = val.get("implied_volatility") or 0.0
                if "CE" in sym: atm_iv_ce = round(float(iv), 2)
                if "PE" in sym: atm_iv_pe = round(float(iv), 2)
        except Exception as e:
            log.debug(f"IV fetch: {e}")

        # Average IV of ATM CE + PE = fair IV
        atm_iv = round((atm_iv_ce + atm_iv_pe) / 2, 2) if atm_iv_ce and atm_iv_pe else max(atm_iv_ce, atm_iv_pe)

        # IV regime classification
        if atm_iv == 0:
            iv_regime = "UNKNOWN"
        elif atm_iv < 12:
            iv_regime = "VERY_LOW"     # < 12% → premium very cheap, straddle best
        elif atm_iv < 16:
            iv_regime = "LOW"          # 12-16% → ideal for straddle
        elif atm_iv < 20:
            iv_regime = "NORMAL"       # 16-20% → okay for straddle
        elif atm_iv < 25:
            iv_regime = "HIGH"         # 20-25% → risky for straddle, avoid buying
        else:
            iv_regime = "VERY_HIGH"    # > 25% → avoid selling premium, avoid straddle

        result = {
            "pcr":         pcr,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "atm_ce_oi":   atm_ce_oi,
            "atm_pe_oi":   atm_pe_oi,
            "sentiment":   "BULLISH" if pcr > 1.2 else "BEARISH" if pcr < 0.8 else "NEUTRAL",
            "atm_iv":      atm_iv,
            "atm_iv_ce":   atm_iv_ce,
            "atm_iv_pe":   atm_iv_pe,
            "iv_regime":   iv_regime,
        }
        log.info(f"  OI/PCR/IV: PCR={pcr} IV={atm_iv}% [{iv_regime}] | {result['sentiment']}")
        return result
    except Exception as e:
        log.warning(f"OI/PCR fetch failed: {e}")
        return {"pcr": 1.0, "total_ce_oi": 0, "total_pe_oi": 0,
                "atm_ce_oi": 0, "atm_pe_oi": 0, "sentiment": "NEUTRAL",
                "atm_iv": 0, "atm_iv_ce": 0, "atm_iv_pe": 0, "iv_regime": "UNKNOWN"}

# ─── SHARED STATE ──────────────────────────────────────────────────────────────
state_lock = threading.RLock()  # Reentrant — safe for nested acquisitions
state = {
    "nifty":        {"price": 0, "change": 0, "pct": 0, "prev": 0},
    "options":      {"atm": 0, "ce_ltp": 0, "pe_ltp": 0, "straddle_premium": 0,
                     "ce_sym": "", "pe_sym": "", "otm_ce_ltp": 0, "otm_pe_ltp": 0},
    "mood":         {"regime": "UNKNOWN", "label": "Waiting for data…", "color": "muted",
                     "adx": 0, "rsi": 0, "atr_pct": 0, "squeeze": False},
    "signal":       {"trade": None, "strategy": None, "score": 0, "details": []},
    "candles":      [],
    "last_scan":    None,
    "last_price_update": None,
    "backtest":     {},
    "alert_history": [],
    "market_open":  False,
    "last_comparison": {},
    "vix":          0.0,
    "oi_pcr":       {"pcr": 0, "sentiment": "NEUTRAL", "total_ce_oi": 0, "total_pe_oi": 0,
                     "atm_ce_oi": 0, "atm_pe_oi": 0},
}

# ─── PAPER TRADING ─────────────────────────────────────────────────────────────
PAPER_FILE  = "paper_trades_v5.json"   # fallback if no DB
paper_lock  = threading.RLock()
paper_state = {
    "open_trades":    [],
    "closed_trades":  [],
    "stats": {"total": 0, "wins": 0, "losses": 0, "pnl_rs": 0.0, "capital": 100000.0},
}

# ─── PostgreSQL helpers ────────────────────────────────────────────────────────
def _pg_conn():
    """Get a PostgreSQL connection from DATABASE_URL env var."""
    url = os.environ.get("DATABASE_URL", "")
    if not url or not HAS_PG:
        return None
    try:
        # Railway gives postgres:// — psycopg2 needs postgresql://
        url = url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url)
    except Exception as e:
        log.warning(f"PG connect failed: {e}")
        return None

def _pg_init():
    """Create tables if they don't exist."""
    conn = _pg_conn()
    if not conn:
        log.warning("⚠️  No DATABASE_URL — using local JSON fallback")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id          TEXT PRIMARY KEY,
                    data        JSONB NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'OPEN',
                    opened_at   TIMESTAMPTZ DEFAULT NOW(),
                    closed_at   TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS paper_stats (
                    id          INTEGER PRIMARY KEY DEFAULT 1,
                    data        JSONB NOT NULL
                );
                INSERT INTO paper_stats (id, data) VALUES (1, '{"total":0,"wins":0,"losses":0,"pnl_rs":0,"capital":100000}')
                ON CONFLICT (id) DO NOTHING;
            """)
        conn.commit()
        log.info("✅ PostgreSQL tables ready")
    except Exception as e:
        log.error(f"PG init error: {e}")
    finally:
        conn.close()

def _save_paper():
    """Save to PostgreSQL (primary) + JSON file (backup)."""
    # ── JSON backup always ─────────────────────────────────────────────
    try:
        with open(PAPER_FILE, "w") as f:
            json.dump(paper_state, f, default=str)
    except:
        pass

    # ── PostgreSQL ────────────────────────────────────────────────────
    conn = _pg_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            # Upsert all open trades
            for t in paper_state["open_trades"]:
                cur.execute("""
                    INSERT INTO paper_trades (id, data, status)
                    VALUES (%s, %s, 'OPEN')
                    ON CONFLICT (id) DO UPDATE
                    SET data = EXCLUDED.data, status = 'OPEN'
                """, (t["id"], json.dumps(t, default=str)))

            # Upsert closed trades
            for t in paper_state["closed_trades"]:
                cur.execute("""
                    INSERT INTO paper_trades (id, data, status, closed_at)
                    VALUES (%s, %s, 'CLOSED', NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET data = EXCLUDED.data, status = 'CLOSED', closed_at = NOW()
                """, (t["id"], json.dumps(t, default=str)))

            # Save stats
            cur.execute("""
                INSERT INTO paper_stats (id, data) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (json.dumps(paper_state["stats"], default=str),))

        conn.commit()
    except Exception as e:
        log.warning(f"PG save error: {e}")
    finally:
        conn.close()

def _load_paper():
    """Load from PostgreSQL (primary) → JSON fallback → fresh start."""
    global paper_state

    # ── Try PostgreSQL first ──────────────────────────────────────────
    conn = _pg_conn()
    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Load stats
                cur.execute("SELECT data FROM paper_stats WHERE id = 1")
                row = cur.fetchone()
                if row:
                    paper_state["stats"].update(row["data"])

                # Load open trades
                cur.execute("SELECT data FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at")
                paper_state["open_trades"] = [dict(r["data"]) for r in cur.fetchall()]

                # Load closed trades (last 200)
                cur.execute("SELECT data FROM paper_trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT 200")
                paper_state["closed_trades"] = [dict(r["data"]) for r in cur.fetchall()]

            log.info(f"✅ Loaded from PostgreSQL: {len(paper_state['open_trades'])} open, {len(paper_state['closed_trades'])} closed")
            conn.close()
            return
        except Exception as e:
            log.warning(f"PG load error: {e}")
            conn.close()

    # ── JSON fallback ─────────────────────────────────────────────────
    if os.path.exists(PAPER_FILE):
        try:
            with open(PAPER_FILE) as f:
                paper_state.update(json.load(f))
            log.info(f"📄 Loaded from JSON: {len(paper_state['closed_trades'])} closed trades")
        except:
            pass

def open_paper_trade(direction: str, symbol: str, entry_ltp: float,
                     strategy: str, spot_price: float, strike: int, opt_type: str):
    """Open an options paper trade."""
    with paper_lock:
        now = datetime.now(IST)
        sl  = round(entry_ltp * (1 - OPTION_SL_PCT), 2)
        tp  = round(entry_ltp * (1 + OPTION_TP_PCT), 2)
        trade = {
            "id":         now.strftime("%H%M%S%f"),
            "direction":  direction,
            "symbol":     symbol,
            "opt_type":   opt_type,
            "strike":     strike,
            "entry_ltp":  entry_ltp,
            "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "spot_entry": spot_price,
            "sl":         sl,
            "tp":         tp,
            "qty":        LOT_SIZE,
            "strategy":   strategy,
            "status":     "OPEN",
            "current_ltp": entry_ltp,
            "pnl_pts":    0.0,
            "pnl_rs":     0.0,
        }
        paper_state["open_trades"].append(trade)
        log.info(f"📄 PAPER TRADE OPENED: {direction} {symbol} @ ₹{entry_ltp} | SL ₹{sl} | TP ₹{tp}")
        _save_paper()
        return trade

def check_paper_trades(current_options: dict):
    """
    Check all open trades for TP/SL/time exit.
    Called every 5 sec — tight SL enforcement.
    Uses ACTUAL option LTP from Kite for accurate P&L.
    """
    with paper_lock:
        now    = datetime.now(IST)
        sq_off = now.hour > 15 or (now.hour == 15 and now.minute >= 15)
        still_open = []

        all_ltps = current_options.get("all_ltps", {})  # exact symbol → ltp map

        for t in paper_state["open_trades"]:
            # ── Get EXACT LTP for this specific strike (not ATM!) ─────
            sym     = t.get("symbol", "")
            cur_ltp = 0.0

            if t["opt_type"] == "STRADDLE":
                # Straddle: both legs stored separately, sum them
                ce_leg = t.get("ce_symbol", "")
                pe_leg = t.get("pe_symbol", "")
                ce_ltp_val = all_ltps.get(f"NFO:{ce_leg}", all_ltps.get(ce_leg, 0.0))
                pe_ltp_val = all_ltps.get(f"NFO:{pe_leg}", all_ltps.get(pe_leg, 0.0))
                if ce_ltp_val > 0 and pe_ltp_val > 0:
                    cur_ltp = round(ce_ltp_val + pe_ltp_val, 2)
                else:
                    # Fallback to ATM combined
                    ce = current_options.get("ce_ltp", 0.0)
                    pe = current_options.get("pe_ltp", 0.0)
                    cur_ltp = round(ce + pe, 2) if ce > 0 and pe > 0 else t["current_ltp"]
            else:
                # Directional: fetch exact strike LTP
                exact = all_ltps.get(f"NFO:{sym}", all_ltps.get(sym, 0.0))
                if exact > 0:
                    cur_ltp = exact  # ✅ exact strike, not ATM
                elif "CE" in sym:
                    cur_ltp = current_options.get("ce_ltp", 0.0)  # fallback ATM
                elif "PE" in sym:
                    cur_ltp = current_options.get("pe_ltp", 0.0)  # fallback ATM

            if cur_ltp <= 0:
                cur_ltp = t["current_ltp"]

            t["current_ltp"] = cur_ltp

            # ── P&L calculation ────────────────────────────────────────
            if "BUY" in t["direction"]:
                pnl_pts = cur_ltp - t["entry_ltp"]
            else:  # SELL (straddle — we want premium to decay)
                pnl_pts = t["entry_ltp"] - cur_ltp
            t["pnl_pts"] = round(pnl_pts, 2)
            t["pnl_rs"]  = round(pnl_pts * t["qty"], 2)
            t["pnl_pct"] = round(pnl_pts / t["entry_ltp"] * 100, 1) if t["entry_ltp"] else 0

            # ── Dynamic Trailing SL — 3 levels ────────────────────────
            # profit>40% → lock +10% | profit>60% → lock +25% | profit>100% → lock +50%
            if TRAILING_SL and "BUY" in t["direction"] and cur_ltp > 0:
                gain_pct = (cur_ltp - t["entry_ltp"]) / t["entry_ltp"]
                new_sl   = t["sl"]
                for trigger, lock in TRAILING_LEVELS:
                    if gain_pct >= trigger:
                        candidate = round(t["entry_ltp"] * (1 + lock), 2)
                        if candidate > new_sl:
                            new_sl = candidate
                if new_sl > t["sl"]:
                    if not t.get("trailing_active"):
                        log.info(f"🔒 TRAILING SL: {sym} | SL ₹{t['sl']} → ₹{new_sl} (gain {gain_pct*100:.0f}%)")
                    t["sl"]             = new_sl
                    t["trailing_active"] = True

            # ── SL / TP / Square-off check ─────────────────────────────
            hit = None
            if sq_off:
                hit = "SQUAREOFF"
            elif "BUY" in t["direction"]:
                if cur_ltp >= t["tp"]:
                    hit = "WIN"
                    log.info(f"🎯 TP HIT: {t['direction']} {sym} | Entry ₹{t['entry_ltp']} → ₹{cur_ltp} | P&L ₹{t['pnl_rs']:+.0f}")
                elif cur_ltp <= t["sl"]:
                    result_type = "WIN" if t.get("trailing_active") else "LOSS"
                    hit = result_type
                    log.info(f"{'🔒' if t.get('trailing_active') else '🛑'} {'TRAIL EXIT' if t.get('trailing_active') else 'SL HIT'}: {sym} | ₹{t['entry_ltp']} → ₹{cur_ltp} | P&L ₹{t['pnl_rs']:+.0f}")
            else:  # SELL straddle
                if cur_ltp <= t["sl"]:
                    hit = "WIN"   # premium decayed
                    log.info(f"🎯 STRADDLE TP: premium decayed ₹{t['entry_ltp']} → ₹{cur_ltp}")
                elif cur_ltp >= t["tp"]:
                    hit = "LOSS"  # premium expanded
                    log.info(f"🛑 STRADDLE SL: premium expanded ₹{t['entry_ltp']} → ₹{cur_ltp}")

            if hit:
                t["status"]    = "CLOSED"
                t["exit_ltp"]  = cur_ltp
                t["exit_time"] = now.strftime("%H:%M:%S")
                t["result"]    = hit
                paper_state["closed_trades"].insert(0, t)
                paper_state["stats"]["total"]   += 1
                paper_state["stats"]["pnl_rs"]   = round(paper_state["stats"]["pnl_rs"] + t["pnl_rs"], 2)
                paper_state["stats"]["capital"]  = round(paper_state["stats"]["capital"] + t["pnl_rs"], 2)
                if hit == "WIN":    paper_state["stats"]["wins"]   += 1
                elif hit == "LOSS": paper_state["stats"]["losses"] += 1
                icon = "✅" if hit == "WIN" else ("❌" if hit == "LOSS" else "⏱")
                log.info(f"📄 PAPER CLOSED: {icon} {hit} | {t['direction']} {sym} | P&L ₹{t['pnl_rs']:+.0f} ({t['pnl_pct']:+.1f}%)")
                tg_trade_closed(t)
                _save_paper()
            else:
                still_open.append(t)

        paper_state["open_trades"] = still_open

# ─── INDICATORS ────────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)

    df["EMA9"]  = c.ewm(span=9,  adjust=False).mean()
    df["EMA21"] = c.ewm(span=21, adjust=False).mean()
    df["EMA50"] = c.ewm(span=50, adjust=False).mean()

    # RSI
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))

    # VWAP
    tp = (h + l + c) / 3
    df["VWAP"] = (tp * v).cumsum() / v.cumsum().replace(0, 1)
    vwap_std   = (tp - df["VWAP"]).rolling(20).std().fillna(0)
    df["VWAP_UP"] = df["VWAP"] + vwap_std
    df["VWAP_DN"] = df["VWAP"] - vwap_std

    # Bollinger Bands
    sma20       = c.rolling(20).mean()
    std20       = c.rolling(20).std()
    df["BB_UP"] = sma20 + 2 * std20
    df["BB_DN"] = sma20 - 2 * std20
    df["BB_MID"]= sma20

    # ATR
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["ATR"]     = tr.ewm(span=14, adjust=False).mean()
    df["ATR_pct"] = df["ATR"] / c

    # ADX
    plus_dm  = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm.values]  = 0
    minus_dm[minus_dm < plus_dm.values] = 0
    atr14    = tr.ewm(span=14, adjust=False).mean()
    pdi      = 100 * plus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, 1e-9)
    mdi      = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, 1e-9)
    dx       = (100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9))
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()
    df["PDI"] = pdi
    df["MDI"] = mdi

    # Keltner Channel for squeeze detection
    df["KC_UP"] = df["VWAP"] + 1.5 * df["ATR"]
    df["KC_DN"] = df["VWAP"] - 1.5 * df["ATR"]

    # Volume MA
    df["Vol_MA"] = v.rolling(20).mean()

    # Supertrend
    hl2 = (h + l) / 2
    atr3 = tr.ewm(span=10, adjust=False).mean()
    upper = hl2 + 3.0 * atr3
    lower = hl2 - 3.0 * atr3
    st = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        u = upper.iloc[i]; l_ = lower.iloc[i]
        pu = upper.iloc[i-1]; pl = lower.iloc[i-1]
        lower.iloc[i] = l_ if (l_ > pl or c.iloc[i-1] < pl) else pl
        upper.iloc[i] = u if (u < pu or c.iloc[i-1] > pu) else pu
        if c.iloc[i] > upper.iloc[i-1]:   trend.iloc[i] = 1
        elif c.iloc[i] < lower.iloc[i-1]: trend.iloc[i] = -1
        else:                              trend.iloc[i] = trend.iloc[i-1]
        st.iloc[i] = lower.iloc[i] if trend.iloc[i] == 1 else upper.iloc[i]
    df["ST"]       = st
    df["ST_trend"] = trend

    return df.dropna(subset=["EMA9", "RSI", "ADX"])

# ─── MARKET MOOD ENGINE ────────────────────────────────────────────────────────
def detect_mood(df: pd.DataFrame) -> dict:
    """
    Hierarchical market regime detection.
    Order: TRENDING → SIDEWAYS → CHOPPY (only one fires)

    TRENDING UP   : 4/5 of (ADX>22, RSI>55, Price>VWAP, EMA9>EMA21, ST=bull)
    TRENDING DOWN : 4/5 of (ADX>22, RSI<45, Price<VWAP, EMA9<EMA21, ST=bear)
    SIDEWAYS      : ADX<20, ATR%<1.6%, Price within ±0.4% of VWAP
    CHOPPY        : everything else
    """
    if len(df) < 20:
        return {"regime": "UNKNOWN", "label": "Not enough data", "color": "muted",
                "adx": 0, "rsi": 0, "atr_pct": 0, "squeeze": False, "strategy": None}

    row   = df.iloc[-1]
    adx   = float(row["ADX"])
    rsi   = float(row["RSI"])
    price = float(row["Close"])
    vwap  = float(row["VWAP"])
    ema9  = float(row["EMA9"])
    ema21 = float(row["EMA21"])
    atr_p = float(row["ATR_pct"])
    st    = int(row["ST_trend"])
    squeeze = bool((row["BB_UP"] <= row["KC_UP"]) and (row["BB_DN"] >= row["KC_DN"]))

    # 5-condition trending score
    bull_score = sum([adx > 22, rsi > 55, price > vwap, ema9 > ema21, st == 1])
    bear_score = sum([adx > 22, rsi < 45, price < vwap, ema9 < ema21, st == -1])

    # SIDEWAYS: strict — all 3 must pass
    vwap_band  = abs(price - vwap) / vwap if vwap > 0 else 1
    is_sideways = adx < 20 and atr_p < 0.016 and vwap_band < 0.004

    # ── Hierarchical: trending first ───────────────────────────────────────
    if bull_score >= 4:
        return {"regime": "TRENDING_UP",   "label": "📈 Trending Up",   "color": "green",
                "adx": round(adx,1), "rsi": round(rsi,1), "atr_pct": round(atr_p*100,2),
                "squeeze": squeeze, "strategy": "BUY_CE",
                "confirms": f"{bull_score}/5 conditions"}

    if bear_score >= 4:
        return {"regime": "TRENDING_DOWN", "label": "📉 Trending Down", "color": "red",
                "adx": round(adx,1), "rsi": round(rsi,1), "atr_pct": round(atr_p*100,2),
                "squeeze": squeeze, "strategy": "BUY_PE",
                "confirms": f"{bear_score}/5 conditions"}

    if is_sideways:
        # ── ORB: compute opening range (first 3 bars = 9:15–9:30) ──────
        orb_bars  = df.between_time("09:15", "09:29") if hasattr(df.index, 'time') else df.head(3)
        orb_info  = {}
        if len(orb_bars) >= 2:
            orb_high = float(orb_bars["High"].max())
            orb_low  = float(orb_bars["Low"].min())
            cur_p    = float(df.iloc[-1]["Close"])
            orb_rng  = orb_high - orb_low
            orb_dir  = None
            orb_brk  = False
            if cur_p > orb_high + orb_rng * 0.1:
                orb_dir = "UP";  orb_brk = True
            elif cur_p < orb_low - orb_rng * 0.1:
                orb_dir = "DOWN"; orb_brk = True
            orb_info = {"ready": True, "high": round(orb_high,2), "low": round(orb_low,2),
                        "range": round(orb_rng,2), "direction": orb_dir, "breakout": orb_brk}
        return {"regime": "SIDEWAYS",      "label": "↔ Sideways",       "color": "gold",
                "adx": round(adx,1), "rsi": round(rsi,1), "atr_pct": round(atr_p*100,2),
                "squeeze": squeeze, "strategy": "STRADDLE" if not squeeze else "IRON_CONDOR"}

    else:
        return {"regime": "CHOPPY",        "label": "〰 Choppy — Wait",  "color": "muted",
                "adx": round(adx,1), "rsi": round(rsi,1), "atr_pct": round(atr_p*100,2),
                "squeeze": squeeze, "strategy": None}

# ─── SIGNAL ENGINE ─────────────────────────────────────────────────────────────
def run_signal_engine(df: pd.DataFrame, mood: dict, vix: float = 0, oi_pcr: dict = None) -> dict:
    """Generate trade signal based on mood + indicators + VIX + IV + OI/PCR."""
    if oi_pcr is None:
        oi_pcr = {}
    if df.empty or len(df) < 20 or mood["regime"] in ("UNKNOWN", "CHOPPY"):
        return {"trade": None, "strategy": None, "score": 0, "details": []}

    # ── IV filter ─────────────────────────────────────────────────────
    atm_iv     = oi_pcr.get("atm_iv", 0)
    iv_regime  = oi_pcr.get("iv_regime", "UNKNOWN")

    # IV regime for strategy switching
    # IV < 15  → buy options (cheap, good for directional)
    # IV 15-22 → normal
    # IV > 22  → prefer selling (expensive premium, good for straddle/condor)
    iv_buy_ok     = atm_iv < 15 or atm_iv == 0   # ideal for buying options
    iv_sell_ok    = atm_iv > 22                    # ideal for selling (straddle/condor)
    straddle_iv_ok    = atm_iv < 20 or atm_iv == 0
    straddle_iv_warn  = 20 <= atm_iv < 25
    straddle_iv_block = atm_iv >= 25

    vix_warn = ""
    if vix > 0:
        if vix > 20 and mood["regime"] in ("TRENDING_UP", "TRENDING_DOWN"):
            vix_warn = f"⚠️ VIX {vix} high — premium expensive"
        elif vix < 14 and mood["regime"] == "SIDEWAYS":
            vix_warn = f"✅ VIX {vix} low — ideal for straddle"

    # PCR sentiment
    pcr      = oi_pcr.get("pcr", 1.0)
    pcr_sent = oi_pcr.get("sentiment", "NEUTRAL")

    row    = df.iloc[-1]
    price  = float(row["Close"])
    adx    = float(row["ADX"])
    rsi    = float(row["RSI"])
    ema9   = float(row["EMA9"])
    ema21  = float(row["EMA21"])
    ema50  = float(row["EMA50"])
    vwap   = float(row["VWAP"])
    st     = int(row["ST_trend"])
    vol_ok = float(row["Volume"]) > float(row["Vol_MA"]) * 1.2 if row["Vol_MA"] > 0 else False

    details = []
    score   = 0

    # ── Weighted scoring: max=8, threshold=5 ─────────────────────────────────
    # EMA trend=2, VWAP=2, Supertrend=1, RSI=1, Volume=1, PCR=1
    if mood["regime"] == "TRENDING_UP":
        if ema9 > ema21 and ema21 > ema50: score += 2; details.append("✅ EMA aligned bull (+2)")
        elif ema9 > ema21:                 score += 1; details.append("✅ EMA9>EMA21 (+1)")
        else:                              details.append("❌ EMA not aligned")
        if price > vwap:                   score += 2; details.append("✅ Price>VWAP (+2)")
        else:                              details.append("❌ Price<VWAP")
        if st == 1:                        score += 1; details.append("✅ Supertrend Bull (+1)")
        else:                              details.append("❌ Supertrend Bear")
        if rsi > 55:                       score += 1; details.append(f"✅ RSI {rsi:.0f}>55 (+1)")
        elif rsi > 50:                     details.append(f"⚪ RSI {rsi:.0f} weak bull")
        else:                              details.append(f"❌ RSI {rsi:.0f} bearish")
        if vol_ok:                         score += 1; details.append("✅ Volume spike (+1)")
        else:                              details.append("⚪ Volume normal")
        if pcr_sent == "BULLISH":          score += 1; details.append(f"✅ PCR {pcr} bullish (+1)")
        elif pcr_sent == "BEARISH":        details.append(f"❌ PCR {pcr} bearish conflict")
        else:                              details.append(f"⚪ PCR {pcr} neutral")
        if vix_warn: details.append(vix_warn)
        details.append(f"📊 Score: {score}/8 (need ≥5)")
        if score >= MIN_SCORE:
            return {"trade": "BUY_CE", "strategy": "Directional BUY CE", "score": score, "details": details}

    elif mood["regime"] == "TRENDING_DOWN":
        if ema9 < ema21 and ema21 < ema50: score += 2; details.append("✅ EMA aligned bear (+2)")
        elif ema9 < ema21:                  score += 1; details.append("✅ EMA9<EMA21 (+1)")
        else:                               details.append("❌ EMA not aligned")
        if price < vwap:                    score += 2; details.append("✅ Price<VWAP (+2)")
        else:                               details.append("❌ Price>VWAP")
        if st == -1:                        score += 1; details.append("✅ Supertrend Bear (+1)")
        else:                               details.append("❌ Supertrend Bull")
        if rsi < 45:                        score += 1; details.append(f"✅ RSI {rsi:.0f}<45 (+1)")
        elif rsi < 50:                      details.append(f"⚪ RSI {rsi:.0f} weak bear")
        else:                               details.append(f"❌ RSI {rsi:.0f} bullish")
        if vol_ok:                          score += 1; details.append("✅ Volume spike (+1)")
        else:                               details.append("⚪ Volume normal")
        if pcr_sent == "BEARISH":           score += 1; details.append(f"✅ PCR {pcr} bearish (+1)")
        elif pcr_sent == "BULLISH":         details.append(f"❌ PCR {pcr} bullish conflict")
        else:                               details.append(f"⚪ PCR {pcr} neutral")
        if vix_warn: details.append(vix_warn)
        details.append(f"📊 Score: {score}/8 (need ≥5)")
        if score >= MIN_SCORE:
            return {"trade": "BUY_PE", "strategy": "Directional BUY PE", "score": score, "details": details}

    # ── EXPIRY DAY STRADDLE — fires regardless of mood ──────────────────────
    now    = datetime.now(IST)
    expiry = is_expiry_day()
    # Expiry day: 2 windows — 9:30-10:30 (theta starts) and 2:00-2:45 (theta peak)
    expiry_window1 = expiry and (now.hour == 9 and now.minute >= 30 or now.hour == 10 and now.minute <= 30)
    expiry_window2 = expiry and (now.hour == 14 and 0 <= now.minute <= 45)
    if expiry_window1 or expiry_window2:
        vix_ok = vix < 18 if vix > 0 else True
        window_label = "9:30-10:30 window" if expiry_window1 else "2:00-2:45 peak theta window"
        if straddle_iv_block:
            details.append(f"🚫 Expiry straddle BLOCKED — IV {atm_iv}% very high (event risk)")
        elif not vix_ok:
            details.append(f"⚠️ Expiry day but VIX {vix} too high — skip straddle")
        else:
            iv_note = (f"⚠️ IV {atm_iv}% elevated — straddle risky" if straddle_iv_warn
                       else f"✅ IV {atm_iv}% [{iv_regime}] — good for straddle" if atm_iv > 0
                       else "⚪ IV unknown")
            return {"trade": "STRADDLE", "strategy": f"Expiry Day Straddle 🎯 ({window_label})", "score": 5,
                    "details": [
                        f"✅ Expiry day — {window_label}",
                        f"✅ VIX {vix} — premium fair" if vix > 0 else "⚪ VIX unknown",
                        iv_note,
                        f"⚪ PCR {pcr} — {pcr_sent}",
                    ]}

    elif mood["regime"] == "SIDEWAYS":
        # Normal day: straddle only at 9:20 AM
        if now.hour == 9 and 18 <= now.minute <= 25:
            if straddle_iv_block:
                details.append(f"🚫 Straddle BLOCKED — IV {atm_iv}% too high")
            else:
                # Range filter: if opening 5-min range already > 0.4% → skip (too volatile)
                open_bars = df.between_time("09:15","09:19") if hasattr(df.index,"time") else df.head(1)
                open_range_pct = ((float(open_bars["High"].max()) - float(open_bars["Low"].min()))
                                  / float(open_bars["Low"].min()) * 100) if len(open_bars) > 0 else 0
                if open_range_pct > 0.4:
                    details.append(f"⚠️ Opening range {open_range_pct:.2f}% > 0.4% — straddle risky, skip")
                else:
                    iv_note = (f"⚠️ IV {atm_iv}% elevated" if straddle_iv_warn
                               else f"✅ IV {atm_iv}% ideal" if atm_iv > 0 else "⚪ IV unknown")
                    return {"trade": "STRADDLE", "strategy": "9:20 Short Straddle", "score": 5,
                            "details": ["✅ 9:20 AM window", "✅ ADX sideways", "✅ Low ATR",
                                        f"✅ Open range {open_range_pct:.2f}% < 0.4%",
                                        iv_note, f"⚪ PCR {pcr}"]}
        # Iron condor rest of day
        squeeze = mood.get("squeeze", False)
        if squeeze and adx < 20:
            return {"trade": "IRON_CONDOR", "strategy": "Iron Condor", "score": 4,
                    "details": ["✅ BB Squeeze", "✅ ADX<20", f"⚪ PCR {pcr}"]}

    # ── ORB: Opening Range Breakout — 9:30–10:30 only ───────────────────────
    if now.hour == 9 and now.minute >= 30 or (now.hour == 10 and now.minute <= 30):
        orb = mood.get("orb", {})
        if orb.get("ready"):
            orb_high = orb.get("high", 0)
            orb_low  = orb.get("low", 0)
            orb_dir  = orb.get("direction")   # "UP" / "DOWN" / None
            orb_brk  = orb.get("breakout")    # True if broken

            orb_vol_ok = float(df.iloc[-1]["Volume"]) > float(df.iloc[-1]["Vol_MA"]) * 1.5 if df.iloc[-1]["Vol_MA"] > 0 else True
            orb_already_fired = mood.get("orb_fired", False)
            if orb_brk and orb_dir == "UP" and not straddle_iv_block and orb_vol_ok and not orb_already_fired:
                iv_note = f"✅ IV {atm_iv}% ok" if atm_iv > 0 else "⚪ IV unknown"
                return {"trade": "BUY_CE", "strategy": "ORB Breakout 📈", "score": 5,
                        "details": [
                            f"✅ ORB High {orb_high} broken",
                            f"✅ Price {price:.0f} > ORB range",
                            iv_note,
                            f"⚪ PCR {pcr} — {pcr_sent}",
                        ]}
            elif orb_brk and orb_dir == "DOWN" and not straddle_iv_block:
                iv_note = f"✅ IV {atm_iv}% ok" if atm_iv > 0 else "⚪ IV unknown"
                return {"trade": "BUY_PE", "strategy": "ORB Breakout 📉", "score": 5,
                        "details": [
                            f"✅ ORB Low {orb_low} broken",
                            f"✅ Price {price:.0f} < ORB range",
                            iv_note,
                            f"⚪ PCR {pcr} — {pcr_sent}",
                        ]}

    return {"trade": None, "strategy": None, "score": score, "details": details}

# ─── MARKET HOURS ──────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 555 <= t <= 931   # 9:15 to 15:31

# ─── LIVE PRICE LOOP (every 3 sec) ────────────────────────────────────────────
# ─── WEBSOCKET TICKER ─────────────────────────────────────────────────────────
ticker_instance  = None
ticker_lock      = threading.Lock()
_last_option_fetch = 0   # timestamp of last option LTP fetch

def _on_ticks(ws, ticks):
    """Called on every WebSocket tick — updates price instantly."""
    global _last_option_fetch
    for tick in ticks:
        if tick.get("instrument_token") == NIFTY_TOKEN:
            price = float(tick["last_price"])
            prev  = _prev_close_cache.get("close") or price
            chg   = price - prev
            pct   = (chg / prev * 100) if prev else 0
            with state_lock:
                state["nifty"] = {
                    "price":  round(price, 2),
                    "change": round(chg,   2),
                    "pct":    round(pct,   2),
                    "prev":   round(prev,  2),
                }
                state["last_price_update"] = datetime.now(IST).strftime("%H:%M:%S")
                state["market_open"]       = True

            # Fetch option LTPs every 15 sec (REST API, not WebSocket)
            now_ts = time.time()
            if now_ts - _last_option_fetch >= 15:
                _last_option_fetch = now_ts
                def _fetch_opts(p=price):
                    try:
                        opts = fetch_atm_options(p)
                        with state_lock:
                            with state_lock:
                                state["options"] = opts
                        check_paper_trades(opts)
                    except Exception as e:
                        log.warning(f"Option fetch failed: {e}")
                threading.Thread(target=_fetch_opts, daemon=True).start()

def _on_connect(ws, response):
    log.info("🔌 WebSocket connected — subscribing Nifty 50")
    ws.subscribe([NIFTY_TOKEN])
    ws.set_mode(ws.MODE_LTP, [NIFTY_TOKEN])

def _on_close(ws, code, reason):
    log.warning(f"WebSocket closed: {code} {reason} — will reconnect")

def _on_error(ws, code, reason):
    log.warning(f"WebSocket error: {code} {reason}")

def start_ticker():
    """Start Kite WebSocket ticker for real-time price."""
    global ticker_instance
    if not _kite_active() or not KITE_AVAILABLE:
        return
    try:
        with kite_lock:
            kc = kite_session
        access_token = kc.access_token
        kt = KiteTicker(KITE_API_KEY, access_token)
        kt.on_ticks   = _on_ticks
        kt.on_connect = _on_connect
        kt.on_close   = _on_close
        kt.on_error   = _on_error
        with ticker_lock:
            ticker_instance = kt
        log.info("🚀 Starting WebSocket ticker...")
        kt.connect(threaded=True)
    except Exception as e:
        log.error(f"Ticker start failed: {e}")

def stop_ticker():
    global ticker_instance
    with ticker_lock:
        if ticker_instance:
            try:
                ticker_instance.close()
            except:
                pass
            ticker_instance = None

def price_loop():
    """
    Price update loop — zero lag design:
    1. WebSocket (KiteTicker) for Nifty spot — tick-by-tick, ~100ms latency
    2. If WS fails → REST polling every 1 sec (fallback)
    3. Option LTPs via REST every 5 sec (Kite rate limit safe)
    4. SL/TP checked on EVERY option fetch — no delayed exits
    """
    ticker_started  = False
    ticker_failed   = False
    last_option_ts  = 0
    fail_count      = 0
    ws_tick_count   = 0

    while True:
        try:
            if _kite_active() and is_market_open():

                # ── Step 1: Start WebSocket (once per session) ─────────────
                if not ticker_started and not ticker_failed:
                    start_ticker()
                    ticker_started = True
                    log.info("WebSocket started — waiting for first tick (10s)...")
                    time.sleep(10)
                    with state_lock:
                        last_upd = state.get("last_price_update")
                    if not last_upd:
                        log.warning("⚠️ WS silent — switching to REST polling")
                        stop_ticker()
                        ticker_failed = True
                    else:
                        log.info("✅ WebSocket delivering ticks — zero-lag price active")

                # ── Step 2: REST fallback (only when WS failed) ────────────
                if ticker_failed:
                    try:
                        q = kite_ltp_nifty()
                        with state_lock:
                            state["nifty"] = q
                            state["last_price_update"] = datetime.now(IST).strftime("%H:%M:%S")
                            state["market_open"] = True
                        fail_count = 0
                    except Exception as e:
                        fail_count += 1
                        if fail_count % 10 == 1:
                            log.warning(f"REST price fetch failed ({fail_count}x): {e}")
                else:
                    # WS is running — just keep market_open true
                    with state_lock:
                        state["market_open"] = True
                    # Watchdog: if WS has been silent for 30 sec, restart it
                    last_upd = state.get("last_price_update")
                    if last_upd:
                        try:
                            last_dt = datetime.strptime(last_upd, "%H:%M:%S").replace(
                                year=datetime.now(IST).year,
                                month=datetime.now(IST).month,
                                day=datetime.now(IST).day,
                                tzinfo=IST
                            )
                            gap = (datetime.now(IST) - last_dt).total_seconds()
                            if gap > 30:
                                log.warning(f"⚠️ WS silent for {gap:.0f}s — restarting...")
                                stop_ticker()
                                ticker_started = False
                        except:
                            pass

                # ── Step 3: Option LTPs every 5 sec (tight SL checks) ─────
                now_ts = time.time()
                if now_ts - last_option_ts >= 2:   # 2s — faster SL detection
                    last_option_ts = now_ts
                    def _fetch_opts():
                        try:
                            with state_lock:
                                p = state["nifty"].get("price", 0)
                            if p:
                                # Collect exact symbols of open trades → fetch in same batch
                                with paper_lock:
                                    open_syms = [t["symbol"] for t in paper_state["open_trades"]
                                                 if t.get("symbol") and "STRADDLE" not in t.get("opt_type","")]
                                opts = fetch_atm_options(p, extra_syms=open_syms)
                                with state_lock:
                                    state["options"] = opts
                                # SL/TP checked here — every 2 sec effectively
                                check_paper_trades(opts)
                        except Exception as e:
                            log.warning(f"Option fetch: {e}")
                    threading.Thread(target=_fetch_opts, daemon=True).start()

            elif not is_market_open():
                with state_lock:
                    with state_lock:
                        state["market_open"] = False
                if ticker_started:
                    stop_ticker()
                    ticker_started = False
                    ticker_failed  = False
                    log.info("Market closed — ticker stopped")

        except Exception as e:
            log.warning(f"Price loop error: {e}")

        time.sleep(1)

# ─── FULL SCAN LOOP (every 5 min) ──────────────────────────────────────────────
def run_scan():
    """Full scan: fetch history, compute indicators, detect mood, generate signal."""
    if not _kite_active():
        log.warning("Scan skipped — Kite not active")
        return
    if not is_market_open():
        log.info("Market closed — scan skipped")
        return
    try:
        df = kite_history_today()
        if df.empty or len(df) < 5:
            log.warning(f"Scan: only {len(df)} bars, skipping")
            return

        df   = compute_indicators(df)

        # Fetch VIX + OI/PCR (every scan)
        vix     = fetch_vix()
        with state_lock:
            spot = state["nifty"].get("price", 0)
        oi_pcr  = fetch_oi_pcr(spot) if spot else {}
        with state_lock:
            state["vix"]    = vix
            state["oi_pcr"] = oi_pcr

        mood = detect_mood(df)
        sig  = run_signal_engine(df, mood, vix=vix, oi_pcr=oi_pcr)

        # Build candles for chart
        candles = [{"t": ts.strftime("%H:%M"), "o": round(float(r["Open"]),2),
                    "h": round(float(r["High"]),2), "l": round(float(r["Low"]),2),
                    "c": round(float(r["Close"]),2)} for ts, r in df.tail(40).iterrows()]

        row = df.iloc[-1]
        with state_lock:
            state["mood"]      = mood
            state["signal"]    = sig
            state["candles"]   = candles
            state["last_scan"] = datetime.now(IST).strftime("%H:%M:%S")
            state["expiry_today"] = is_expiry_day()
            state["expiry_str"]   = get_weekly_expiry_str()

        log.info(f"  Mood: {mood['regime']} | ADX:{mood['adx']} RSI:{mood['rsi']} | Signal: {sig['trade'] or 'NONE'} ({sig['score']}/8 weighted)")

        # Auto open paper trade on signal
        if sig["trade"] and _kite_active():
            with state_lock:
                spot   = state["nifty"]["price"]
                opts   = state["options"]
            _auto_paper_trade(sig, spot, opts)

    except Exception as e:
        log.error(f"Scan error: {e}")

def _auto_paper_trade(sig: dict, spot: float, opts: dict):
    """Auto open paper trade based on signal — with all guardrails."""
    now = datetime.now(IST)

    with paper_lock:
        open_dirs = [t["direction"] for t in paper_state["open_trades"]]
        if sig["trade"] in open_dirs:
            return

        # ── Daily guardrails ────────────────────────────────────────────────
        stats     = paper_state["stats"]
        capital   = stats.get("capital", 100000)
        start_cap = stats.get("start_capital", 100000)

        # Daily loss limit (-3%)
        daily_pnl = stats.get("daily_pnl", 0)
        if daily_pnl < -(start_cap * MAX_DAILY_LOSS_PCT):
            log.info(f"🛑 Daily loss limit hit: ₹{daily_pnl:.0f} — no more trades today")
            return

        # Max trades per day
        today_str    = now.strftime("%Y-%m-%d")
        trades_today = sum(1 for t in paper_state["closed_trades"]
                          if t.get("entry_time","")[:10] == today_str)
        trades_today += len(paper_state["open_trades"])
        if trades_today >= MAX_TRADES_DAY:
            log.info(f"🛑 Max {MAX_TRADES_DAY} trades/day reached — skipping")
            return

        # Cooldown between trades
        last_trade_time = stats.get("last_trade_time")
        if last_trade_time:
            try:
                lt = datetime.fromisoformat(last_trade_time)
                if (now - lt).total_seconds() < TRADE_COOLDOWN_MIN * 60:
                    log.info(f"⏳ Trade cooldown — {TRADE_COOLDOWN_MIN}min between trades")
                    return
            except: pass

    # Send Telegram alert BEFORE opening trade
    tg_signal(sig, spot, opts)

    trade    = sig["trade"]
    strategy = sig["strategy"]
    atm      = opts.get("atm", round(spot / 50) * 50)

    if trade == "BUY_CE":
        # Try 1 strike ITM first (better delta, less theta)
        itm_strike = atm - 50 if spot > atm else atm
        itm_sym    = build_option_symbol(itm_strike, "CE")
        itm_ltp    = opts.get("all_ltps", {}).get(itm_sym, 0)
        if itm_ltp >= MIN_PREMIUM:
            ltp = round(itm_ltp * (1 + SLIPPAGE_PCT), 2)
            open_paper_trade("BUY_CE", itm_sym.replace("NFO:",""),
                             ltp, strategy, spot, itm_strike, "CE")
        else:
            # Fallback to ATM
            ltp = opts.get("ce_ltp", 0)
            if ltp < MIN_PREMIUM:
                log.info(f"⚠️ CE premium ₹{ltp} < min ₹{MIN_PREMIUM} — skip"); return
            ltp = round(ltp * (1 + SLIPPAGE_PCT), 2)
            open_paper_trade("BUY_CE", opts.get("ce_sym", f"NIFTY{atm}CE"),
                             ltp, strategy, spot, atm, "CE")

    elif trade == "BUY_PE":
        # Try 1 strike ITM first
        itm_strike = atm + 50 if spot < atm + 50 else atm
        itm_sym    = build_option_symbol(itm_strike, "PE")
        itm_ltp    = opts.get("all_ltps", {}).get(itm_sym, 0)
        if itm_ltp >= MIN_PREMIUM:
            ltp = round(itm_ltp * (1 + SLIPPAGE_PCT), 2)
            open_paper_trade("BUY_PE", itm_sym.replace("NFO:",""),
                             ltp, strategy, spot, itm_strike, "PE")
        else:
            ltp = opts.get("pe_ltp", 0)
            if ltp < MIN_PREMIUM:
                log.info(f"⚠️ PE premium ₹{ltp} < min ₹{MIN_PREMIUM} — skip"); return
            ltp = round(ltp * (1 + SLIPPAGE_PCT), 2)
            open_paper_trade("BUY_PE", opts.get("pe_sym", f"NIFTY{atm}PE"),
                             ltp, strategy, spot, atm, "PE")

    elif trade == "STRADDLE":
        ce_ltp = opts.get("ce_ltp", 0)
        pe_ltp = opts.get("pe_ltp", 0)
        if ce_ltp <= 0 or pe_ltp <= 0: return
        combined = round(ce_ltp + pe_ltp, 2)
        ce_sym_clean = opts.get("ce_sym", f"NFO:NIFTY{atm}CE").replace("NFO:","")
        pe_sym_clean = opts.get("pe_sym", f"NFO:NIFTY{atm}PE").replace("NFO:","")
        t = open_paper_trade("SELL_STRADDLE", ce_sym_clean,
                         combined, strategy, spot, atm, "STRADDLE")
        # Store both legs for exact LTP tracking
        if t:
            with paper_lock:
                for trade_obj in paper_state["open_trades"]:
                    if trade_obj.get("id") == t.get("id"):
                        trade_obj["ce_symbol"] = ce_sym_clean
                        trade_obj["pe_symbol"] = pe_sym_clean
                        trade_obj["ce_entry"]  = ce_ltp
                        trade_obj["pe_entry"]  = pe_ltp
                        break

def scan_loop():
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scan loop: {e}")
        time.sleep(SCAN_INTERVAL)

# ─── YAHOO FINANCE HISTORY (no Kite required) ─────────────────────────────────
def yf_history_multi(days: int = 60, from_date: str = None, to_date: str = None) -> pd.DataFrame:
    """
    Fetch Nifty 5-min OHLCV via yfinance — works WITHOUT Kite login.
    yfinance 5m limit: last 60 days. Beyond that uses daily candles.
    Falls back to Kite if yfinance fails.
    """
    try:
        import yfinance as yf
        now = datetime.now(IST)
        if from_date:
            # fetch 60 extra days before from_date for indicator warmup
            from_dt = datetime.strptime(from_date, "%Y-%m-%d")
            start   = (from_dt - timedelta(days=60)).strftime("%Y-%m-%d")
            end     = to_date or now.strftime("%Y-%m-%d")
        else:
            start = (now - timedelta(days=days + 3)).strftime("%Y-%m-%d")
            end   = now.strftime("%Y-%m-%d")

        span_days = (datetime.now() - datetime.strptime(start, "%Y-%m-%d")).days
        interval  = "5m" if span_days <= 58 else "1d"
        log.info(f"  yfinance ^NSEI: {interval} bars {start} → {end}")

        df = yf.Ticker("^NSEI").history(start=start, end=end, interval=interval, auto_adjust=True)
        if df.empty:
            raise ValueError("Empty response from yfinance")

        df = df[["Open","High","Low","Close","Volume"]].dropna()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)
        log.info(f"  yfinance: got {len(df)} bars")
        return df
    except ImportError:
        log.warning("yfinance not installed — run: pip install yfinance")
    except Exception as e:
        log.warning(f"yfinance failed: {e}")
    # Fallback to Kite
    if _kite_active():
        log.info("  Falling back to Kite history")
        return kite_history_multi(days)
    return pd.DataFrame()

# ─── BACKTEST (Daily bars — works for years of data via yfinance) ──────────────
# ─── NSE BHAVCOPY (real option prices + lot size) ─────────────────────────────

import urllib.request, zipfile, io as _io

_bhavcopy_cache: dict = {}   # date_str → DataFrame
_bhavcopy_lock = threading.Lock()


def _fetch_bhavcopy(date_str: str) -> pd.DataFrame:
    """
    Fetch NSE F&O bhavcopy for a given date (YYYYMMDD).
    Returns DataFrame with NIFTY options for that day.
    Thread-safe — uses lock to prevent duplicate fetches.
    """
    with _bhavcopy_lock:
        if date_str in _bhavcopy_cache:
            return _bhavcopy_cache[date_str]

    # Try new format first (2024+)
    urls = [
        f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip",
        # Older format fallback
        f"https://archives.nseindia.com/content/fo/fo{datetime.strptime(date_str,'%Y%m%d').strftime('%d%b%Y').upper()}bhav.csv.zip",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/",
    }
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                zdata = resp.read()
            with zipfile.ZipFile(_io.BytesIO(zdata)) as z:
                df = pd.read_csv(z.open(z.namelist()[0]))

            # Normalise column names across formats
            df.columns = [c.strip().upper() for c in df.columns]
            col_map = {
                "TCKRSYMB": "SYMBOL", "FINSTNM": "SYMBOL",
                "XPRY_DT": "EXPIRY_DT", "EXPDT": "EXPIRY_DT",
                "STRKPRIC": "STRIKE_PR", "STRK_PRC": "STRIKE_PR",
                "OPTNTP": "OPTION_TYP", "OPT_TYP": "OPTION_TYP",
                "OPNPRIC": "OPEN", "HGHPRIC": "HIGH", "LWPRIC": "LOW",
                "CLSPRIC": "CLOSE", "STTLPRIC": "SETTLE_PR",
                "TTLQTY": "CONTRACTS", "LOTSIZE": "LOT_SIZE",
            }
            df.rename(columns=col_map, inplace=True)

            # Filter NIFTY options only
            sym_col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
            nifty = df[df[sym_col].astype(str).str.strip() == "NIFTY"].copy()

            if nifty.empty:
                continue

            with _bhavcopy_lock:
                _bhavcopy_cache[date_str] = nifty
            log.info(f"  Bhavcopy {date_str}: {len(nifty)} NIFTY option rows")
            return nifty
        except Exception as e:
            log.debug(f"  Bhavcopy {url[:60]}… failed: {e}")

    with _bhavcopy_lock:
        _bhavcopy_cache[date_str] = pd.DataFrame()
    return pd.DataFrame()


def _get_option_price(date_str: str, strike: int, opt_type: str, expiry_str: str) -> float:
    """
    Get real ATM option close price from bhavcopy.
    Falls back to spot * 1.5% estimate if not available.
    """
    d_fmt = date_str.replace("-", "")
    bdf = _fetch_bhavcopy(d_fmt)
    if bdf.empty:
        return None

    try:
        # Match strike + option type
        mask = (
            (bdf["STRIKE_PR"].astype(float).astype(int) == int(strike)) &
            (bdf["OPTION_TYP"].astype(str).str.strip().str.upper() == opt_type.upper())
        )
        row = bdf[mask]
        if row.empty:
            return None
        price = float(row["SETTLE_PR"].iloc[0]) if "SETTLE_PR" in row.columns else float(row["CLOSE"].iloc[0])
        return price if price > 0 else None
    except Exception as e:
        log.debug(f"  Option price lookup failed: {e}")
        return None


def _get_lot_size_from_bhavcopy(date_str: str) -> int:
    """Get NIFTY lot size from bhavcopy for a specific date."""
    d_fmt = date_str.replace("-", "")
    bdf = _fetch_bhavcopy(d_fmt)
    if bdf.empty or "LOT_SIZE" not in bdf.columns:
        return LOT_SIZE
    try:
        lot = int(bdf["LOT_SIZE"].iloc[0])
        return lot if lot > 0 else LOT_SIZE
    except:
        return LOT_SIZE


def _fetch_daily(days: int = 365, from_date: str = None, to_date: str = None) -> pd.DataFrame:
    """
    Fetch daily OHLCV for Nifty via yfinance.
    No limit on days — years of data available.
    Falls back to Kite 5m if yfinance unavailable (auto-aggregates to daily).
    """
    try:
        import yfinance as yf
        now = datetime.now(IST)
        if from_date:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d")
            start   = (from_dt - timedelta(days=60)).strftime("%Y-%m-%d")
            end     = to_date or now.strftime("%Y-%m-%d")
        else:
            start = (now - timedelta(days=days + 60)).strftime("%Y-%m-%d")
            end   = now.strftime("%Y-%m-%d")
        log.info(f"  yfinance daily ^NSEI: {start} → {end}")
        df = yf.Ticker("^NSEI").history(start=start, end=end, interval="1d", auto_adjust=True)
        if df.empty:
            raise ValueError("Empty yfinance response")
        df = df[["Open","High","Low","Close","Volume"]].dropna()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)
        log.info(f"  yfinance daily: {len(df)} candles")
        return df
    except Exception as e:
        log.warning(f"yfinance daily failed: {e}")
        import traceback; traceback.print_exc()
    # Kite fallback — fetch 5m and resample to daily
    if _kite_active():
        log.info("  Falling back to Kite (5m → daily resample)")
        try:
            df5 = kite_history_multi(min(days, 390))
            if df5.empty:
                return pd.DataFrame()
            df = df5.resample("1D").agg({
                "Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"
            }).dropna()
            return df
        except Exception as e2:
            log.warning(f"Kite fallback failed: {e2}")
    return pd.DataFrame()


def _compute_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute indicators on daily bars."""
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)

    df = df.copy()
    df["EMA9"]  = c.ewm(span=9,  adjust=False).mean()
    df["EMA21"] = c.ewm(span=21, adjust=False).mean()
    df["EMA50"] = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))

    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["ATR"]     = tr.ewm(span=14, adjust=False).mean()
    df["ATR_pct"] = df["ATR"] / c

    plus_dm  = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm[plus_dm   < minus_dm.values] = 0
    minus_dm[minus_dm < plus_dm.values]  = 0
    atr14 = tr.ewm(span=14, adjust=False).mean()
    pdi   = 100 * plus_dm.ewm(span=14, adjust=False).mean()  / atr14.replace(0, 1e-9)
    mdi   = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, 1e-9)
    dx    = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()

    # VWAP on daily = (H+L+C)/3 rolling proxy
    tp = (h + l + c) / 3
    df["VWAP"] = tp.rolling(20).mean()

    # Supertrend (daily)
    hl2  = (h + l) / 2
    atr3 = tr.ewm(span=10, adjust=False).mean()
    upper = hl2 + 3.0 * atr3
    lower = hl2 - 3.0 * atr3
    trend = pd.Series(1, index=df.index)
    lower_vals = lower.values.copy()
    upper_vals = upper.values.copy()
    trend_vals = trend.values.copy()
    c_vals     = c.values
    for i in range(1, len(df)):
        u  = upper_vals[i]; l_ = lower_vals[i]
        lower_vals[i] = l_ if (l_ > lower_vals[i-1] or c_vals[i-1] < lower_vals[i-1]) else lower_vals[i-1]
        upper_vals[i] = u  if (u  < upper_vals[i-1] or c_vals[i-1] > upper_vals[i-1]) else upper_vals[i-1]
        if   c_vals[i] > upper_vals[i-1]: trend_vals[i] = 1
        elif c_vals[i] < lower_vals[i-1]: trend_vals[i] = -1
        else:                             trend_vals[i] = trend_vals[i-1]
    lower[:] = lower_vals
    upper[:] = upper_vals
    trend[:] = trend_vals
    df["ST_trend"] = trend
    df["Vol_MA"]   = v.rolling(20).mean()

    return df.dropna(subset=["EMA9","RSI","ADX"])


def _daily_mood(row) -> str:
    """Classify market mood from a single daily bar."""
    adx = float(row["ADX"]); rsi = float(row["RSI"])
    p   = float(row["Close"]); vwap = float(row["VWAP"])
    e9  = float(row["EMA9"]); e21 = float(row["EMA21"])
    st  = int(row["ST_trend"]); atr_p = float(row["ATR_pct"])
    bull = sum([p > vwap, e9 > e21, st == 1])
    bear = sum([p < vwap, e9 < e21, st == -1])
    if adx > 20 and rsi > 52 and bull >= 2:   return "TRENDING_UP"
    if adx > 20 and rsi < 48 and bear >= 2:   return "TRENDING_DOWN"
    if adx < 22 and atr_p < 0.020:            return "SIDEWAYS"
    return "CHOPPY"


def run_backtest(days: int = 365, from_date: str = None, to_date: str = None):
    """
    Daily-bar backtest — works for years of data via yfinance.
    No Kite login required. Strategy logic on daily OHLC.
    """
    try:
        log.info(f"📊 Backtest (daily bars): {days}d | {from_date or 'latest'} → {to_date or 'today'}")
        df_raw = _fetch_daily(days, from_date=from_date, to_date=to_date)
        print(f"[DEBUG] _fetch_daily returned {len(df_raw)} rows, empty={df_raw.empty}", flush=True)
        if df_raw.empty or len(df_raw) < 15:
            print(f"[DEBUG] Not enough data ({len(df_raw)} rows) — returning error", flush=True)
            return {"error": f"No data ({len(df_raw)} rows) — install yfinance or connect Kite"}

        df_raw = _compute_daily_indicators(df_raw)
        df_raw["date"] = df_raw.index.date

        all_dates = sorted(df_raw["date"].unique())
        if from_date and to_date:
            from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
            to_d   = datetime.strptime(to_date,   "%Y-%m-%d").date()
            dates  = [d for d in all_dates if from_d <= d <= to_d]
        elif from_date:
            from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
            dates  = [d for d in all_dates if d >= from_d]
        else:
            dates  = all_dates[-days:]

        log.info(f"  Backtest: {len(dates)} trading days")

        results   = {"directional":{"trades":0,"wins":0,"pnl":0},
                     "straddle":   {"trades":0,"wins":0,"pnl":0}}
        trade_log = []
        capital   = 100000.0
        LOT       = LOT_SIZE

        for i, d in enumerate(dates):
            row = df_raw[df_raw["date"] == d]
            if row.empty: continue
            row      = row.iloc[0]
            mood     = _daily_mood(row)
            weekday  = d.weekday()
            if weekday > 4: continue  # skip weekends (yfinance sometimes includes them)
            date_str = str(d)
            expiry_str = _expiry_str_for_date(d)
            # Use real lot size from bhavcopy if available
            LOT = _get_lot_size_from_bhavcopy(date_str)

            open_p  = float(row["Open"])
            high_p  = float(row["High"])
            low_p   = float(row["Low"])
            close_p = float(row["Close"])

            if mood in ("TRENDING_UP", "TRENDING_DOWN"):
                direction = 1 if mood == "TRENDING_UP" else -1
                opt_type  = "CE" if direction == 1 else "PE"
                entry_spot = open_p   # enter at open
                atm        = round(entry_spot / 50) * 50
                nfo_sym    = build_option_symbol(atm, opt_type, trade_date=d)
                # Try real option price from bhavcopy, fall back to estimate
                real_prem  = _get_option_price(date_str, atm, opt_type, expiry_str)
                prem_entry = real_prem if real_prem else _est_premium(entry_spot, dte=_dte(d))
                sl_prem    = round(prem_entry * (1 - OPTION_SL_PCT), 2)
                tp_prem    = round(prem_entry * (1 + OPTION_TP_PCT), 2)

                # Daily bar outcome simulation
                day_move_pct = (close_p - open_p) / open_p  # raw spot move
                dir_move     = day_move_pct * direction       # positive = trend held
                if direction == 1:
                    hit_tp = high_p  >= entry_spot * 1.018
                    hit_sl = low_p   <= entry_spot * 0.992
                else:
                    hit_tp = low_p   <= entry_spot * 0.982
                    hit_sl = high_p  >= entry_spot * 1.008

                if hit_tp and hit_sl:
                    if dir_move > 0:
                        result = "WIN";       exit_p = tp_prem
                    else:
                        result = "LOSS";      exit_p = sl_prem
                elif hit_tp:
                    result = "WIN";           exit_p = tp_prem
                elif hit_sl:
                    result = "LOSS";          exit_p = sl_prem
                else:
                    # Squareoff: option price reflects actual spot move (delta ~0.5 for ATM)
                    opt_move = dir_move * 5.0  # ATM option moves ~5x spot (delta+gamma)
                    opt_move = max(-0.60, min(0.80, opt_move))  # cap at -60%/+80%
                    exit_p   = round(prem_entry * (1 + opt_move), 2)
                    result   = "WIN" if opt_move > 0 else "SQUAREOFF"

                pnl_rs   = round((exit_p - prem_entry) * LOT, 2)
                capital += pnl_rs
                results["directional"]["trades"] += 1
                results["directional"]["pnl"]    += pnl_rs
                if result == "WIN": results["directional"]["wins"] += 1

                trade_log.append({
                    "date":       date_str,
                    "weekday":    ["Mon","Tue","Wed","Thu","Fri"][weekday],
                    "strategy":   f"Directional {opt_type}",
                    "mood":       mood,
                    "symbol":     nfo_sym.replace("NFO:",""),
                    "strike":     atm,
                    "opt_type":   opt_type,
                    "expiry":     expiry_str,
                    "spot_entry": round(entry_spot, 2),
                    "entry":      prem_entry,
                    "sl":         sl_prem,
                    "tp":         tp_prem,
                    "exit":       exit_p,
                    "entry_time": "09:15",
                    "exit_time":  "15:15",
                    "result":     result,
                    "pnl_rs":     round(pnl_rs, 0),
                    "pnl_pct":    round((exit_p - prem_entry) / prem_entry * 100, 1),
                    "capital":    round(capital, 0),
                    "adx":        round(float(row["ADX"]),1),
                    "rsi":        round(float(row["RSI"]),1),
                    "lots":       1,
                })

            elif mood == "SIDEWAYS":
                atm        = round(open_p / 50) * 50
                ce_sym     = build_option_symbol(atm, "CE", trade_date=d).replace("NFO:","")
                pe_sym     = build_option_symbol(atm, "PE", trade_date=d).replace("NFO:","")
                strad_sym  = f"{ce_sym} + {pe_sym}"
                ce_real = _get_option_price(date_str, atm, "CE", expiry_str)
                pe_real = _get_option_price(date_str, atm, "PE", expiry_str)
                strad_prem = round((ce_real + pe_real), 2) if ce_real and pe_real else round(_est_premium(open_p, dte=_dte(d)) * 2, 2)
                move_pct   = abs(close_p - open_p) / open_p

                if move_pct < 0.015:
                    result = "WIN";  exit_p = round(strad_prem * 0.50, 2)
                    pnl_rs = round((strad_prem - exit_p) * LOT, 2)
                else:
                    result = "LOSS"; exit_p = round(strad_prem * 1.25, 2)
                    pnl_rs = round((strad_prem - exit_p) * LOT, 2)

                capital += pnl_rs
                results["straddle"]["trades"] += 1
                results["straddle"]["pnl"]    += pnl_rs
                if result == "WIN": results["straddle"]["wins"] += 1

                trade_log.append({
                    "date":       date_str,
                    "weekday":    ["Mon","Tue","Wed","Thu","Fri"][weekday],
                    "strategy":   "Short Straddle",
                    "mood":       "SIDEWAYS",
                    "symbol":     strad_sym,
                    "strike":     atm,
                    "opt_type":   "STRADDLE",
                    "expiry":     expiry_str,
                    "spot_entry": round(open_p, 2),
                    "entry":      strad_prem,
                    "sl":         round(strad_prem * 1.25, 2),
                    "tp":         round(strad_prem * 0.50, 2),
                    "exit":       exit_p,
                    "entry_time": "09:20",
                    "exit_time":  "15:15",
                    "result":     result,
                    "pnl_rs":     round(pnl_rs, 0),
                    "pnl_pct":    round((strad_prem - exit_p) / strad_prem * 100, 1),
                    "capital":    round(capital, 0),
                    "adx":        round(float(row["ADX"]),1),
                    "rsi":        round(float(row["RSI"]),1),
                    "lots":       1,
                    "move_pct":   round(move_pct * 100, 2),
                })

        def wr(r):
            return round(r["wins"] / r["trades"] * 100, 1) if r["trades"] else 0

        total_trades = results["directional"]["trades"] + results["straddle"]["trades"]
        total_wins   = results["directional"]["wins"]   + results["straddle"]["wins"]
        total_pnl    = round(results["directional"]["pnl"] + results["straddle"]["pnl"], 0)

        summary = {
            "days":        len(dates),
            "period":      f"{dates[0]} to {dates[-1]}" if dates else "",
            "source":      "daily_bars",
            "directional": {**results["directional"], "win_rate": wr(results["directional"])},
            "straddle":    {**results["straddle"],    "win_rate": wr(results["straddle"])},
            "overall":     {"trades": total_trades, "wins": total_wins,
                            "win_rate": round(total_wins/total_trades*100,1) if total_trades else 0,
                            "pnl": total_pnl, "final_capital": round(capital, 0)},
            "trade_log":   list(reversed(trade_log)),
        }
        log.info(f"  Backtest done: {total_trades} trades | WR {summary['overall']['win_rate']}% | P&L ₹{total_pnl:+,.0f}")
        return summary
    except Exception as e:
        log.error(f"Backtest error: {e}")
        import traceback; traceback.print_exc()
        return {}


def backtest_strategy(strategy_id: str, days: int = 365) -> dict:
    """Run strategy backtest on daily bars — no Kite required."""
    try:
        log.info(f"📊 Strategy backtest: {strategy_id} ({days} days)")
        df_raw = _fetch_daily(days)
        if df_raw.empty or len(df_raw) < 15:
            return {}
        df_raw = _compute_daily_indicators(df_raw)
        df_raw["date"] = df_raw.index.date
        dates = sorted(df_raw["date"].unique())[-days:]

        capital = 100000.0; trade_log = []
        wins = losses = trades = 0; total_pnl = 0.0
        LOT = LOT_SIZE

        for i, d in enumerate(dates):
            row = df_raw[df_raw["date"] == d]
            if row.empty: continue
            row      = row.iloc[0]
            mood     = _daily_mood(row)
            date_str = str(d)
            weekday  = d.weekday()
            if weekday > 4: continue  # skip weekends (yfinance sometimes includes them)
            open_p   = float(row["Open"])
            high_p   = float(row["High"])
            low_p    = float(row["Low"])
            close_p  = float(row["Close"])

            result = None; pnl_rs = 0; prem_entry = 0; exit_p = 0

            if strategy_id == "orb":
                # On daily bars: ORB = first 30min range ≈ (H-L)*0.3
                orb_range  = (high_p - low_p) * 0.3
                entry_spot = open_p
                prem_entry = _est_premium(entry_spot, dte=_dte(d))
                tp_prem    = round(prem_entry * 1.80, 2)
                sl_prem    = round(prem_entry * 0.60, 2)
                # Breakout: if day moved > 0.5% from open
                move = (close_p - open_p) / open_p
                if abs(move) < 0.003: continue  # no clear breakout
                direction = 1 if move > 0 else -1
                hit_tp = high_p >= entry_spot * 1.012 if direction == 1 else low_p <= entry_spot * 0.988
                hit_sl = low_p  <= entry_spot * 0.994 if direction == 1 else high_p >= entry_spot * 1.006
                day_mv = (close_p - open_p) / open_p * direction
                if hit_tp and hit_sl:
                    if day_mv > 0:
                        result = "WIN";  exit_p = tp_prem; pnl_rs = round((tp_prem - prem_entry) * LOT, 2)
                    else:
                        result = "LOSS"; exit_p = sl_prem; pnl_rs = round((sl_prem - prem_entry) * LOT, 2)
                elif hit_tp:
                    result = "WIN";  exit_p = tp_prem; pnl_rs = round((tp_prem - prem_entry) * LOT, 2)
                elif hit_sl:
                    result = "LOSS"; exit_p = sl_prem; pnl_rs = round((sl_prem - prem_entry) * LOT, 2)
                else:
                    exit_p = prem_entry * 0.95; pnl_rs = round((exit_p - prem_entry) * LOT, 2); result = "SQUAREOFF"

            elif strategy_id in ("straddle_only", "expiry_straddle_only"):
                if strategy_id == "expiry_straddle_only" and weekday != 1: continue
                ce_real = _get_option_price(date_str, atm, "CE", expiry_str)
                pe_real = _get_option_price(date_str, atm, "PE", expiry_str)
                strad_prem = round((ce_real + pe_real), 2) if ce_real and pe_real else round(_est_premium(open_p, dte=_dte(d)) * 2, 2)
                move_pct   = abs(close_p - open_p) / open_p
                prem_entry = strad_prem
                if move_pct < 0.015:
                    result = "WIN";  pnl_rs = round(strad_prem * 0.5 * LOT, 2);  exit_p = round(strad_prem*0.5,2)
                else:
                    result = "LOSS"; pnl_rs = round(-strad_prem * 0.3 * LOT, 2); exit_p = round(strad_prem*1.3,2)

            elif strategy_id in ("directional_only", "strict_directional"):
                if mood not in ("TRENDING_UP","TRENDING_DOWN"): continue
                direction  = 1 if mood == "TRENDING_UP" else -1
                min_sc     = 4 if strategy_id == "strict_directional" else 3
                # Quick score on this bar
                sc = sum([
                    float(row["EMA9"]) > float(row["EMA21"]) if direction==1 else float(row["EMA9"]) < float(row["EMA21"]),
                    float(row["EMA21"]) > float(row["EMA50"]) if direction==1 else float(row["EMA21"]) < float(row["EMA50"]),
                    float(row["Close"]) > float(row["VWAP"]) if direction==1 else float(row["Close"]) < float(row["VWAP"]),
                    int(row["ST_trend"]) == direction,
                    float(row["RSI"]) > 50 if direction==1 else float(row["RSI"]) < 50,
                ])
                if sc < min_sc: continue
                entry_spot = open_p; prem_entry = _est_premium(entry_spot, dte=_dte(d))
                tp_prem = round(prem_entry * (1+OPTION_TP_PCT), 2)
                sl_prem = round(prem_entry * (1-OPTION_SL_PCT), 2)
                hit_tp = high_p >= entry_spot * 1.018 if direction==1 else low_p  <= entry_spot * 0.982
                hit_sl = low_p  <= entry_spot * 0.992 if direction==1 else high_p >= entry_spot * 1.008
                day_mv = (close_p - open_p) / open_p * direction
                if hit_tp and hit_sl:
                    if day_mv > 0:
                        result = "WIN";  exit_p = tp_prem; pnl_rs = round((tp_prem-prem_entry)*LOT,2)
                    else:
                        result = "LOSS"; exit_p = sl_prem; pnl_rs = round((sl_prem-prem_entry)*LOT,2)
                elif hit_tp:
                    result = "WIN";  exit_p = tp_prem; pnl_rs = round((tp_prem-prem_entry)*LOT,2)
                elif hit_sl:
                    result = "LOSS"; exit_p = sl_prem; pnl_rs = round((sl_prem-prem_entry)*LOT,2)
                else:
                    dir_mv3=(close_p-open_p)/open_p*direction
                    opt_mv3=max(-0.60,min(0.80,dir_mv3*5.0))
                    exit_p=round(prem_entry*(1+opt_mv3),2); pnl_rs=round((exit_p-prem_entry)*LOT,2)
                    result="WIN" if opt_mv3>0 else "SQUAREOFF"

            elif strategy_id == "our_combined":
                if mood in ("TRENDING_UP","TRENDING_DOWN"):
                    direction = 1 if mood=="TRENDING_UP" else -1
                    entry_spot = open_p; prem_entry = _est_premium(entry_spot, dte=_dte(d))
                    tp_prem = round(prem_entry*(1+OPTION_TP_PCT),2)
                    sl_prem = round(prem_entry*(1-OPTION_SL_PCT),2)
                    hit_tp = high_p>=entry_spot*1.018 if direction==1 else low_p<=entry_spot*0.982
                    hit_sl = low_p<=entry_spot*0.992  if direction==1 else high_p>=entry_spot*1.008
                    day_mv = (close_p-open_p)/open_p*direction
                    if hit_tp and hit_sl:
                        if day_mv>0: result="WIN"; exit_p=tp_prem; pnl_rs=round((tp_prem-prem_entry)*LOT,2)
                        else:        result="LOSS"; exit_p=sl_prem; pnl_rs=round((sl_prem-prem_entry)*LOT,2)
                    elif hit_tp:
                        result="WIN"; exit_p=tp_prem; pnl_rs=round((tp_prem-prem_entry)*LOT,2)
                    elif hit_sl:
                        result="LOSS"; exit_p=sl_prem; pnl_rs=round((sl_prem-prem_entry)*LOT,2)
                    else:
                        dir_mv2=(close_p-open_p)/open_p*(1 if mood=="TRENDING_UP" else -1)
                        opt_mv2=max(-0.60,min(0.80,dir_mv2*5.0))
                        exit_p=round(prem_entry*(1+opt_mv2),2); pnl_rs=round((exit_p-prem_entry)*LOT,2)
                        result="WIN" if opt_mv2>0 else "SQUAREOFF"
                elif mood=="SIDEWAYS":
                    prem_entry = round(open_p*0.02,2); move_pct=abs(close_p-open_p)/open_p
                    if move_pct<0.015:
                        result="WIN";  pnl_rs=round(prem_entry*0.5*LOT,2);  exit_p=round(prem_entry*0.5,2)
                    else:
                        result="LOSS"; pnl_rs=round(-prem_entry*0.3*LOT,2); exit_p=round(prem_entry*1.3,2)
                else: continue
            else:
                continue

            if result is None: continue
            capital += pnl_rs; total_pnl += pnl_rs; trades += 1
            if result=="WIN": wins+=1
            elif result=="LOSS": losses+=1
            trade_log.append({
                "date": date_str,
                "strategy": STRATEGIES.get(strategy_id,{}).get("name", strategy_id),
                "mood": mood, "entry": prem_entry, "exit": exit_p,
                "result": result, "pnl_rs": round(pnl_rs,0),
                "capital": round(capital,0),
            })

        wr = round(wins/trades*100,1) if trades else 0

        # Max drawdown
        capitals    = [100000.0] + [t["capital"] for t in trade_log]
        peak        = capitals[0]; max_dd = 0
        for cap in capitals:
            if cap > peak: peak = cap
            dd = (peak - cap) / peak * 100
            if dd > max_dd: max_dd = dd

        # Profit factor = gross wins / gross losses
        gross_win  = sum(t["pnl_rs"] for t in trade_log if t["pnl_rs"] > 0)
        gross_loss = abs(sum(t["pnl_rs"] for t in trade_log if t["pnl_rs"] < 0))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 999

        # Sharpe ratio (simplified — daily returns)
        if len(trade_log) > 1:
            import numpy as np
            rets = [t["pnl_rs"] / 100000 for t in trade_log]
            sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * (252**0.5), 2)
        else:
            sharpe = 0

        return {
            "strategy_id":    strategy_id,
            "strategy_name":  STRATEGIES.get(strategy_id,{}).get("name", strategy_id),
            "strategy_color": STRATEGIES.get(strategy_id,{}).get("color","#fff"),
            "days":           len(dates),
            "period":         f"{dates[0]} to {dates[-1]}" if dates else "",
            "trades":         trades, "wins": wins, "losses": losses,
            "win_rate":       wr, "total_pnl": round(total_pnl,0),
            "final_capital":  round(capital,0),
            "max_drawdown":   round(max_dd, 1),
            "profit_factor":  profit_factor,
            "sharpe":         sharpe,
            "trade_log":      list(reversed(trade_log)),
        }
    except Exception as e:
        log.error(f"Strategy backtest error ({strategy_id}): {e}")
        import traceback; traceback.print_exc()
        return {}

# ─── STRATEGY COMPARISON ENGINE ───────────────────────────────────────────────

STRATEGIES = {
    "our_combined": {
        "name": "Our Combined",
        "desc": "Directional (EMA+VWAP+RSI+ADX) + Expiry Straddle + Iron Condor",
        "color": "#00d4aa",
    },
    "straddle_only": {
        "name": "Straddle Only",
        "desc": "Sell ATM straddle every Tuesday expiry 9:20 AM",
        "color": "#f5c518",
    },
    "directional_only": {
        "name": "Directional Only",
        "desc": "BUY CE/PE based on EMA+VWAP+RSI+ADX trend signals",
        "color": "#4fa3f5",
    },
    "orb": {
        "name": "ORB (Opening Range Breakout)",
        "desc": "9:15-9:30 AM range, buy breakout above/below with 1:2 RR",
        "color": "#f97316",
    },
    "strict_directional": {
        "name": "Strict Directional (Score≥4)",
        "desc": "Same as directional but requires 4/5 score — fewer, higher quality trades",
        "color": "#a78bfa",
    },
    "expiry_straddle_only": {
        "name": "Expiry Day Straddle Only",
        "desc": "Only trade on Tuesday expiry, sell straddle 9:20 AM",
        "color": "#fb7185",
    },
}

def compare_strategies(strategy_ids: list, days: int = 30) -> dict:
    """Run multiple strategies and return comparison."""
    results = {}
    for sid in strategy_ids:
        results[sid] = backtest_strategy(sid, days)
    return {"strategies": results, "days": days, "strategy_list": STRATEGIES}

STRATEGIES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy Lab · Nifty Scanner v5</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07090c;--surf:#0d1117;--surf2:#141922;--border:#1a2030;
  --green:#00d4aa;--red:#ff4060;--gold:#f5c518;--blue:#4fa3f5;--orange:#f97316;--purple:#a78bfa;--pink:#fb7185;
  --text:#d8e0ec;--muted:#4a5568;--radius:8px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;min-height:100vh}
body::before{content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse at 10% 20%,rgba(0,212,170,.05) 0%,transparent 50%),
             radial-gradient(ellipse at 90% 80%,rgba(167,139,250,.04) 0%,transparent 50%);
  pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:0 24px 80px}

/* NAV */
.nav{display:flex;align-items:center;justify-content:space-between;padding:18px 0 16px;
  border-bottom:1px solid var(--border);margin-bottom:28px;flex-wrap:wrap;gap:12px}
.nav-brand{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:.05em;color:#fff}
.nav-brand span{color:var(--green)}
.nav-links{display:flex;gap:8px}
.nav-link{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  padding:6px 16px;border-radius:4px;border:1px solid var(--border);color:var(--muted);
  text-decoration:none;transition:.15s}
.nav-link:hover,.nav-link.active{color:var(--text);border-color:#444}
.nav-link.active{background:var(--surf2)}

/* PAGE HEADER */
.page-header{margin-bottom:32px}
.page-title{font-family:'Bebas Neue',sans-serif;font-size:48px;letter-spacing:.03em;line-height:1;
  background:linear-gradient(135deg,#fff 0%,var(--green) 100%);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text}
.page-sub{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-top:6px}

/* STRATEGY SELECTOR */
.selector-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:28px}
@media(max-width:900px){.selector-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.selector-grid{grid-template-columns:1fr}}

.strategy-card{background:var(--surf);border:2px solid var(--border);border-radius:var(--radius);
  padding:16px 18px;cursor:pointer;transition:.2s;user-select:none;position:relative;overflow:hidden}
.strategy-card::before{content:'';position:absolute;inset:0;opacity:0;transition:.2s;pointer-events:none}
.strategy-card:hover{border-color:#333}
.strategy-card.selected{border-color:var(--s-color,var(--green))!important}
.strategy-card.selected::before{opacity:.07;background:var(--s-color,var(--green))}
.s-dot{width:8px;height:8px;border-radius:50%;background:var(--s-color,var(--green));
  display:inline-block;margin-right:8px;flex-shrink:0}
.s-name{font-family:'DM Mono',monospace;font-size:11px;font-weight:500;letter-spacing:.05em;
  text-transform:uppercase;color:var(--text);display:flex;align-items:center}
.s-desc{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.5}
.s-check{position:absolute;top:12px;right:12px;width:18px;height:18px;border-radius:50%;
  border:1.5px solid var(--border);display:flex;align-items:center;justify-content:center;
  font-size:10px;transition:.2s}
.strategy-card.selected .s-check{background:var(--s-color,var(--green));border-color:var(--s-color,var(--green));color:#000}

/* CONTROLS */
.controls{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px 24px;margin-bottom:28px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.period-group{display:flex;gap:6px;flex-wrap:wrap}
.period-btn{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  padding:7px 16px;border-radius:4px;border:1px solid var(--border);background:transparent;
  color:var(--muted);cursor:pointer;transition:.15s}
.period-btn:hover{color:var(--text);border-color:#444}
.period-btn.active{background:var(--surf2);color:var(--text);border-color:#555}
.run-btn{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  padding:10px 28px;border-radius:4px;border:none;background:var(--green);color:#000;
  cursor:pointer;font-weight:600;transition:.15s;margin-left:auto}
.run-btn:hover{background:#00b894}
.run-btn:disabled{opacity:.4;cursor:not-allowed}
.ctrl-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
#running-indicator{font-family:'DM Mono',monospace;font-size:10px;color:var(--gold);
  display:none;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* COMPARISON RESULTS */
.results-section{display:none}
.results-section.visible{display:block}

/* SCOREBOARD */
.scoreboard{display:grid;gap:12px;margin-bottom:24px}

.score-row{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 22px;display:grid;grid-template-columns:220px 80px 100px 120px 120px 120px 1fr;
  align-items:center;gap:12px;transition:.2s;position:relative;overflow:hidden}
.score-row::after{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--s-color,var(--muted));border-radius:2px 0 0 2px}
.score-row:hover{border-color:#2a3040}

.score-name{font-family:'DM Mono',monospace;font-size:11px;font-weight:500;
  color:var(--text);display:flex;align-items:center;gap:8px}
.score-badge{font-family:'Bebas Neue',sans-serif;font-size:10px;letter-spacing:.08em;
  padding:2px 8px;border-radius:2px;background:rgba(255,255,255,.06);color:var(--muted)}

.score-wr{font-family:'Bebas Neue',sans-serif;font-size:28px;line-height:1}
.score-trades{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)}
.score-pnl{font-family:'Bebas Neue',sans-serif;font-size:22px;line-height:1}
.score-cap{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-top:2px}

.wr-bar-wrap{background:var(--border);height:4px;border-radius:2px;overflow:hidden}
.wr-bar{height:100%;border-radius:2px;transition:width .8s cubic-bezier(.4,0,.2,1)}

.rank-badge{position:absolute;top:10px;right:14px;font-family:'Bebas Neue',sans-serif;
  font-size:11px;color:var(--muted);letter-spacing:.05em}
.rank-1{color:var(--gold)}

/* DETAIL TABS */
.detail-section{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden;margin-bottom:16px}
.tab-bar{display:flex;border-bottom:1px solid var(--border);overflow-x:auto}
.tab{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  padding:12px 20px;cursor:pointer;color:var(--muted);white-space:nowrap;border-bottom:2px solid transparent;
  transition:.15s;flex-shrink:0}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--green)}
.tab-content{display:none;padding:20px}
.tab-content.active{display:block}

/* TRADE LOG TABLE */
.tlog-header{display:grid;grid-template-columns:90px 180px 80px 80px 70px 90px 100px;
  gap:8px;padding:8px 14px;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border)}
.tlog-row{display:grid;grid-template-columns:90px 180px 80px 80px 70px 90px 100px;
  gap:8px;padding:9px 14px;font-family:'DM Mono',monospace;font-size:11px;
  border-bottom:1px solid rgba(26,32,48,.5);border-left:3px solid transparent;transition:.1s}
.tlog-row:hover{background:var(--surf2)}
.tlog-row.win{border-left-color:var(--green);background:rgba(0,212,170,.02)}
.tlog-row.loss{border-left-color:var(--red);background:rgba(255,64,96,.02)}
.tlog-row.sq{border-left-color:var(--muted)}

/* EQUITY CURVE */
#equity-canvas{width:100%;height:200px;display:block}

/* STATS GRID */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:700px){.stats-grid{grid-template-columns:repeat(2,1fr)}}
.stat-box{background:var(--surf2);border:1px solid var(--border);border-radius:6px;padding:14px 16px}
.stat-val{font-family:'Bebas Neue',sans-serif;font-size:28px;line-height:1}
.stat-lbl{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-top:4px}

/* EMPTY STATE */
.empty-state{text-align:center;padding:60px 20px;color:var(--muted)}
.empty-icon{font-size:40px;margin-bottom:16px;opacity:.4}
.empty-text{font-family:'DM Mono',monospace;font-size:12px;letter-spacing:.05em}

@media(max-width:900px){
  .score-row{grid-template-columns:1fr 80px 100px 120px;gap:10px}
  .score-row > *:nth-child(n+6){display:none}
}
</style>
</head>
<body>
<div class="wrap">

  <!-- NAV -->
  <div class="nav">
    <div class="nav-brand">NIFTY SCANNER <span>v5</span></div>
    <div class="nav-links">
      <a href="/" class="nav-link">Dashboard</a>
      <a href="/strategies" class="nav-link active">Strategy Lab</a>
    </div>
  </div>

  <!-- HEADER -->
  <div class="page-header">
    <div class="page-title">Strategy Lab</div>
    <div class="page-sub">Compare · Backtest · Optimise · Pick the Winner</div>
  </div>

  <!-- STRATEGY SELECTOR -->
  <div class="ctrl-label">Select Strategies to Compare (pick 2–4)</div>
  <div class="selector-grid" id="strategy-grid"></div>

  <!-- CONTROLS -->
  <div class="controls">
    <div>
      <div class="ctrl-label">Period</div>
      <div class="period-group">
        <button class="period-btn" onclick="setPeriod(7)"  data-d="7">Last Week</button>
        <button class="period-btn active" onclick="setPeriod(30)" data-d="30">Last Month</button>
        <button class="period-btn" onclick="setPeriod(60)" data-d="60">60 Days</button>
        <button class="period-btn" onclick="setPeriod(90)" data-d="90">90 Days</button>
      </div>
    </div>
    <div id="running-indicator">⏳ Running backtests…</div>
    <button class="run-btn" id="run-btn" onclick="runComparison()">▶ Run Comparison</button>
  </div>

  <!-- RESULTS -->
  <div class="results-section" id="results-section">

    <!-- SCOREBOARD -->
    <div class="ctrl-label" style="margin-bottom:12px">Leaderboard</div>
    <div class="scoreboard" id="scoreboard"></div>

    <!-- DETAIL TABS -->
    <div class="detail-section">
      <div class="tab-bar" id="tab-bar"></div>
      <div id="tab-contents"></div>
    </div>

  </div>

  <!-- EMPTY -->
  <div id="empty-state" class="empty-state">
    <div class="empty-icon">⚗️</div>
    <div class="empty-text">Select 2–4 strategies above and click Run Comparison</div>
  </div>

</div>
<script>
const STRATEGY_META = {};
let selectedStrategies = ['our_combined', 'straddle_only'];
let selectedDays = 30;
let comparisonData = null;
let activeTab = null;
let pollInterval = null;

// ── Load strategy list ────────────────────────────────────────────────────
async function loadStrategies(){
  const res  = await fetch('/api/strategies/list');
  const list = await res.json();
  const grid = document.getElementById('strategy-grid');
  grid.innerHTML = '';
  Object.entries(list).forEach(([id, s]) => {
    STRATEGY_META[id] = s;
    const card = document.createElement('div');
    card.className = 'strategy-card' + (selectedStrategies.includes(id) ? ' selected' : '');
    card.style.setProperty('--s-color', s.color);
    card.dataset.id = id;
    card.innerHTML = `
      <div class="s-name"><span class="s-dot"></span>${s.name}</div>
      <div class="s-desc">${s.desc}</div>
      <div class="s-check">${selectedStrategies.includes(id) ? '✓' : ''}</div>`;
    card.onclick = () => toggleStrategy(id, card, s.color);
    grid.appendChild(card);
  });
}

function toggleStrategy(id, card, color){
  if(selectedStrategies.includes(id)){
    if(selectedStrategies.length <= 1) return; // keep at least 1
    selectedStrategies = selectedStrategies.filter(s => s !== id);
    card.classList.remove('selected');
    card.querySelector('.s-check').textContent = '';
  } else {
    if(selectedStrategies.length >= 4){ alert('Max 4 strategies at a time'); return; }
    selectedStrategies.push(id);
    card.classList.add('selected');
    card.querySelector('.s-check').textContent = '✓';
  }
}

function setPeriod(days){
  selectedDays = days;
  document.querySelectorAll('.period-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.d) === days);
  });
}

// ── Run comparison ────────────────────────────────────────────────────────
async function runComparison(){
  const btn = document.getElementById('run-btn');
  const ind = document.getElementById('running-indicator');
  btn.disabled = true;
  ind.style.display = 'block';
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('results-section').classList.remove('visible');
  document.getElementById('scoreboard').innerHTML = '<div style="padding:20px;color:var(--muted);font-family:monospace;font-size:11px;text-align:center">⏳ Fetching '+selectedDays+' days of data for '+selectedStrategies.length+' strategies…</div>';
  document.getElementById('results-section').classList.add('visible');

  await fetch('/api/strategies/compare', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({strategies: selectedStrategies, days: selectedDays})
  });

  if(pollInterval) clearInterval(pollInterval);
  let attempts = 0;
  pollInterval = setInterval(async () => {
    attempts++;
    const res = await fetch('/api/strategies/result');
    const d   = await res.json();
    if(d.strategies && Object.keys(d.strategies).length > 0 || attempts > 60){
      clearInterval(pollInterval);
      btn.disabled = false;
      ind.style.display = 'none';
      comparisonData = d;
      renderResults(d);
    }
  }, 2000);
}

// ── Render results ────────────────────────────────────────────────────────
function renderResults(data){
  const strategies = data.strategies || {};
  const entries = Object.entries(strategies)
    .filter(([,v]) => v && v.trades > 0)
    .sort((a,b) => (b[1].total_pnl||0) - (a[1].total_pnl||0));

  // Scoreboard
  const sb = document.getElementById('scoreboard');
  sb.innerHTML = entries.map(([id, s], i) => {
    const color = STRATEGY_META[id]?.color || '#fff';
    const wr    = s.win_rate || 0;
    const wrColor = wr >= 60 ? 'var(--green)' : wr >= 45 ? 'var(--gold)' : 'var(--red)';
    const pnl   = s.total_pnl || 0;
    const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    return `<div class="score-row" style="--s-color:${color}">
      <div class="score-name">
        <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0"></span>
        ${s.strategy_name || id}
      </div>
      <div>
        <div class="score-wr" style="color:${wrColor}">${wr}%</div>
        <div class="score-trades">${s.trades} trades</div>
      </div>
      <div>
        <div class="score-pnl" style="color:${pnlColor}">${pnl>=0?'+':''}₹${fmt(Math.abs(pnl),0)}</div>
        <div class="score-cap">Final ₹${fmt(s.final_capital||100000,0)}</div>
      </div>
      <div style="min-width:80px">
        <div class="wr-bar-wrap"><div class="wr-bar" style="width:${wr}%;background:${wrColor}"></div></div>
        <div style="font-family:monospace;font-size:9px;color:var(--muted);margin-top:3px">${s.wins}W ${s.losses}L</div>
      </div>
      <div style="font-family:monospace;font-size:10px;color:var(--muted)">${s.period||''}</div>
      <div style="font-family:monospace;font-size:9px;color:var(--muted)">${s.days||0} days</div>
      ${i===0?'<div class="rank-badge rank-1">🥇 BEST</div>':''}
    </div>`;
  }).join('');

  // Tab bar
  const tabBar  = document.getElementById('tab-bar');
  const tabCont = document.getElementById('tab-contents');
  tabBar.innerHTML  = '';
  tabCont.innerHTML = '';

  // Add equity curve tab first
  const eqTab = document.createElement('div');
  eqTab.className = 'tab active';
  eqTab.textContent = 'Equity Curve';
  eqTab.onclick = () => switchTab('equity');
  eqTab.dataset.tab = 'equity';
  tabBar.appendChild(eqTab);
  const eqContent = document.createElement('div');
  eqContent.className = 'tab-content active';
  eqContent.id = 'tab-equity';
  eqContent.innerHTML = '<canvas id="equity-canvas"></canvas>';
  tabCont.appendChild(eqContent);

  entries.forEach(([id, s]) => {
    const color = STRATEGY_META[id]?.color || '#fff';
    const tab = document.createElement('div');
    tab.className = 'tab';
    tab.innerHTML = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${color};margin-right:6px;vertical-align:middle"></span>${s.strategy_name||id}`;
    tab.onclick = () => switchTab(id);
    tab.dataset.tab = id;
    tabBar.appendChild(tab);

    const content = document.createElement('div');
    content.className = 'tab-content';
    content.id = 'tab-' + id;
    content.innerHTML = renderStrategyDetail(id, s, color);
    tabCont.appendChild(content);
  });

  activeTab = 'equity';
  setTimeout(() => drawEquityCurve(entries), 100);
}

function switchTab(id){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === id));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-'+id));
  activeTab = id;
  if(id === 'equity') setTimeout(() => drawEquityCurve(Object.entries(comparisonData.strategies||{}).filter(([,v])=>v&&v.trades>0).sort((a,b)=>(b[1].total_pnl||0)-(a[1].total_pnl||0))), 50);
}

function renderStrategyDetail(id, s, color){
  const wr = s.win_rate || 0;
  const wrColor = wr >= 60 ? 'var(--green)' : wr >= 45 ? 'var(--gold)' : 'var(--red)';
  const pnl = s.total_pnl || 0;
  const log = s.trade_log || [];

  return `
    <div class="stats-grid" style="margin-bottom:20px">
      <div class="stat-box"><div class="stat-val" style="color:${wrColor}">${wr}%</div><div class="stat-lbl">Win Rate</div></div>
      <div class="stat-box"><div class="stat-val" style="color:${pnl>=0?'var(--green)':'var(--red)'}">${pnl>=0?'+':''}₹${fmt(Math.abs(pnl),0)}</div><div class="stat-lbl">Total P&L</div></div>
      <div class="stat-box"><div class="stat-val">${s.trades||0}</div><div class="stat-lbl">Trades</div></div>
      <div class="stat-box"><div class="stat-val">₹${fmt(s.final_capital||100000,0)}</div><div class="stat-lbl">Final Capital</div></div>
    </div>
    <div style="font-family:monospace;font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px">Trade Log</div>
    <div style="max-height:320px;overflow-y:auto">
      <div class="tlog-header"><span>Date</span><span>Strategy</span><span>Mood</span><span>Entry ₹</span><span>Exit ₹</span><span>P&L ₹</span><span>Capital ₹</span></div>
      ${log.length ? log.map(t => {
        const cls = t.result==='WIN'?'win':t.result==='LOSS'?'loss':'sq';
        const icon = t.result==='WIN'?'✅':t.result==='LOSS'?'❌':'⏱';
        const p = t.pnl_rs||0;
        const mc = t.mood==='TRENDING_UP'?'var(--green)':t.mood==='TRENDING_DOWN'?'var(--red)':t.mood==='SIDEWAYS'?'var(--gold)':'var(--muted)';
        return `<div class="tlog-row ${cls}">
          <span style="color:var(--muted)">${t.date}</span>
          <span>${t.strategy}</span>
          <span style="color:${mc};font-size:9px">${(t.mood||'').replace('_',' ')}</span>
          <span>₹${fmt(t.entry)}</span>
          <span>₹${fmt(t.exit||0)}</span>
          <span style="color:${p>=0?'var(--green)':'var(--red)'}">${icon} ${p>=0?'+':''}₹${fmt(Math.abs(p),0)}</span>
          <span style="color:var(--muted)">₹${fmt(t.capital||0,0)}</span>
        </div>`;
      }).join('') : '<div style="padding:20px;text-align:center;color:var(--muted);font-family:monospace;font-size:11px">No trades in this period</div>'}
    </div>`;
}

// ── Equity Curve ──────────────────────────────────────────────────────────
function drawEquityCurve(entries){
  const canvas = document.getElementById('equity-canvas');
  if(!canvas) return;
  const W = canvas.offsetWidth || 800;
  const H = 200;
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);

  // Background grid
  ctx.strokeStyle = 'rgba(26,32,48,.8)';
  ctx.lineWidth = 1;
  for(let i=0;i<=4;i++){
    const y = (H/4)*i;
    ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke();
  }

  if(!entries.length) return;

  // Collect all capital curves
  const curves = entries.map(([id, s]) => {
    const color = STRATEGY_META[id]?.color || '#fff';
    const log = [...(s.trade_log||[])].reverse(); // oldest first
    const points = [100000, ...log.map(t => t.capital||100000)];
    return {id, color, points, name: s.strategy_name||id};
  });

  const allPoints = curves.flatMap(c => c.points);
  const mn = Math.min(...allPoints) * 0.98;
  const mx = Math.max(...allPoints) * 1.02;
  const range = mx - mn || 1;
  const maxLen = Math.max(...curves.map(c => c.points.length));

  const x = i => (i / Math.max(maxLen-1, 1)) * W;
  const y = v => H - ((v - mn) / range) * (H - 20) - 10;

  // Draw baseline at 100k
  const baseY = y(100000);
  ctx.strokeStyle = 'rgba(74,85,104,.5)';
  ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(0, baseY); ctx.lineTo(W, baseY); ctx.stroke();
  ctx.setLineDash([]);

  // Draw each curve
  curves.forEach(({color, points}) => {
    if(points.length < 2) return;
    ctx.beginPath();
    points.forEach((p,i) => i===0 ? ctx.moveTo(x(i),y(p)) : ctx.lineTo(x(i),y(p)));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
    // Dot at end
    const last = points[points.length-1];
    ctx.beginPath();
    ctx.arc(x(points.length-1), y(last), 4, 0, Math.PI*2);
    ctx.fillStyle = color; ctx.fill();
  });

  // Legend
  curves.forEach(({color, name}, i) => {
    const lx = 12 + i * 160;
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(lx+4, 14, 4, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = 'rgba(216,224,236,.7)';
    ctx.font = '10px monospace';
    ctx.fillText(name, lx+12, 18);
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────
function fmt(n, d=2){ return Number(n).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}); }

// ── Init ──────────────────────────────────────────────────────────────────
loadStrategies();
window.addEventListener('resize', () => {
  if(activeTab === 'equity' && comparisonData) {
    const entries = Object.entries(comparisonData.strategies||{}).filter(([,v])=>v&&v.trades>0).sort((a,b)=>(b[1].total_pnl||0)-(a[1].total_pnl||0));
    drawEquityCurve(entries);
  }
});
</script>
</body>
</html>"""


HISTORY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade History · Nifty Scanner v5</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090c;--surf:#0d1117;--surf2:#141922;--border:#1a2030;
  --green:#00d4aa;--red:#ff4060;--gold:#f5c518;--blue:#4fa3f5;
  --text:#d8e0ec;--muted:#4a5568;--radius:8px}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px}
.wrap{max-width:1200px;margin:0 auto;padding:0 24px 60px}
.nav{display:flex;align-items:center;justify-content:space-between;padding:18px 0 16px;
  border-bottom:1px solid var(--border);margin-bottom:28px;flex-wrap:wrap;gap:12px}
.nav-brand{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:.05em;color:#fff}
.nav-brand span{color:var(--green)}
.nav-links{display:flex;gap:8px}
.nav-link{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  padding:6px 16px;border-radius:4px;border:1px solid var(--border);color:var(--muted);
  text-decoration:none;transition:.15s}
.nav-link:hover,.nav-link.active{color:var(--text);border-color:#444}
.nav-link.active{background:var(--surf2)}
.page-title{font-family:'Bebas Neue',sans-serif;font-size:48px;
  background:linear-gradient(135deg,#fff,var(--green));-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text;margin-bottom:6px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;align-items:center}
.filter-btn{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  padding:6px 14px;border-radius:4px;border:1px solid var(--border);background:transparent;
  color:var(--muted);cursor:pointer;transition:.15s}
.filter-btn:hover{color:var(--text);border-color:#444}
.filter-btn.active{background:var(--surf2);color:var(--text);border-color:#555}
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
@media(max-width:600px){.stats-row{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.stat-val{font-family:'Bebas Neue',sans-serif;font-size:28px}
.stat-lbl{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-top:3px}
.table-wrap{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.th-row{display:grid;grid-template-columns:95px 70px 140px 155px 80px 80px 90px 80px;gap:8px;
  padding:10px 14px;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--surf2)}
.td-row{display:grid;grid-template-columns:95px 70px 140px 155px 80px 80px 90px 80px;gap:8px;
  padding:9px 14px;font-family:'DM Mono',monospace;font-size:11px;
  border-bottom:1px solid rgba(26,32,48,.5);border-left:3px solid transparent}
.td-row:hover{background:var(--surf2)}
.td-row.win{border-left-color:var(--green);background:rgba(0,212,170,.02)}
.td-row.loss{border-left-color:var(--red);background:rgba(255,64,96,.02)}
.loading{padding:40px;text-align:center;color:var(--muted);font-family:'DM Mono',monospace;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav">
    <div class="nav-brand">NIFTY SCANNER <span>v5</span></div>
    <div class="nav-links">
      <a href="/" class="nav-link">Dashboard</a>
      <a href="/strategies" class="nav-link">Strategy Lab</a>
      <a href="/history" class="nav-link active">History</a>
    </div>
  </div>
  <div class="page-title">Trade History</div>
  <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);margin-bottom:20px">All paper trades from database</div>

  <div class="filters">
    <span style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase">Period:</span>
    <button class="filter-btn" onclick="setDays(7)">7D</button>
    <button class="filter-btn active" onclick="setDays(30)" id="f-30">30D</button>
    <button class="filter-btn" onclick="setDays(90)">90D</button>
    <button class="filter-btn" onclick="setDays(999)">All</button>
    <span style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-left:12px">Result:</span>
    <button class="filter-btn active" onclick="setResult('')" id="r-all">All</button>
    <button class="filter-btn" onclick="setResult('WIN')">Wins</button>
    <button class="filter-btn" onclick="setResult('LOSS')">Losses</button>
    <button class="filter-btn" onclick="setResult('SQUAREOFF')">Squareoff</button>
  </div>

  <div class="stats-row">
    <div class="stat"><div class="stat-val" id="h-total">--</div><div class="stat-lbl">Total Trades</div></div>
    <div class="stat"><div class="stat-val" id="h-wr" style="color:var(--muted)">--%</div><div class="stat-lbl">Win Rate</div></div>
    <div class="stat"><div class="stat-val" id="h-pnl" style="color:var(--muted)">₹--</div><div class="stat-lbl">Total P&L</div></div>
    <div class="stat"><div class="stat-val" id="h-wins">--</div><div class="stat-lbl">Wins / Losses</div></div>
  </div>

  <div class="table-wrap">
    <div class="th-row"><span>Date</span><span>Time</span><span>Direction</span><span>Symbol</span><span>Entry ₹</span><span>Exit ₹</span><span>P&L ₹</span><span>Result</span></div>
    <div id="trade-body"><div class="loading">Loading…</div></div>
  </div>
</div>
<script>
let cDays=30, cResult='';
function setDays(d){
  cDays=d;
  document.querySelectorAll('.filter-btn').forEach(b=>{
    const labels={'7':'7D','30':'30D','90':'90D','999':'All'};
    if(Object.values(labels).includes(b.textContent))
      b.className='filter-btn'+(b.textContent===labels[String(d)]?' active':'');
  });
  load();
}
function setResult(r){
  cResult=r;
  document.querySelectorAll('.filter-btn').forEach(b=>{
    const map={'':'All','WIN':'Wins','LOSS':'Losses','SQUAREOFF':'Squareoff'};
    if(Object.values(map).includes(b.textContent))
      b.className='filter-btn'+(b.textContent===map[r]?' active':'');
  });
  load();
}
function fmt(n,d=2){return Number(n).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d})}
async function load(){
  document.getElementById('trade-body').innerHTML='<div class="loading">Loading…</div>';
  try{
    const res=await fetch(`/api/paper/history?days=${cDays}&result=${cResult}`);
    const d=await res.json();
    const trades=d.trades||[];
    const wins=trades.filter(t=>t.result==='WIN').length;
    const losses=trades.filter(t=>t.result==='LOSS').length;
    const total=trades.length;
    const wr=total?Math.round(wins/total*100):0;
    const pnl=trades.reduce((s,t)=>s+(t.pnl_rs||0),0);
    document.getElementById('h-total').textContent=total;
    const wrEl=document.getElementById('h-wr');
    wrEl.textContent=wr+'%'; wrEl.style.color=wr>=55?'var(--green)':wr>=40?'var(--gold)':'var(--red)';
    const pEl=document.getElementById('h-pnl');
    pEl.textContent=(pnl>=0?'+':'')+'₹'+fmt(Math.abs(pnl),0); pEl.style.color=pnl>=0?'var(--green)':'var(--red)';
    document.getElementById('h-wins').textContent=wins+'W / '+losses+'L';
    const body=document.getElementById('trade-body');
    if(!trades.length){body.innerHTML='<div class="loading">No trades found</div>';return;}
    body.innerHTML=trades.map(t=>{
      const cls=t.result==='WIN'?'win':t.result==='LOSS'?'loss':'';
      const icon=t.result==='WIN'?'✅':t.result==='LOSS'?'❌':'⏱';
      const p=t.pnl_rs||0;
      const pc=p>=0?'color:var(--green)':'color:var(--red)';
      const dc=t.direction?.includes('CE')?'color:var(--green)':'color:var(--red)';
      return `<div class="td-row ${cls}">
        <span style="color:var(--muted)">${(t.entry_time||'--').slice(0,10)}</span>
        <span style="color:var(--muted)">${(t.entry_time||'--').slice(11,19)||t.entry_time||'--'}</span>
        <span style="${dc}">${t.direction||'--'}</span>
        <span style="font-size:9px;color:var(--blue)">${(t.symbol||'--').replace('NFO:','')}</span>
        <span>₹${fmt(t.entry_ltp||0)}</span>
        <span>₹${fmt(t.exit_ltp||0)}</span>
        <span style="${pc}">${icon} ${p>=0?'+':''}₹${fmt(Math.abs(p),0)}</span>
        <span style="${pc}">${t.result||'--'}</span>
      </div>`;
    }).join('');
  }catch(e){document.getElementById('trade-body').innerHTML='<div class="loading">Error loading trades</div>';}
}
load();
</script>
</body>
</html>"""

# ─── FLASK ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty Options Scanner v5</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07090c;--surf:#0d1117;--surf2:#141922;--border:#1a2030;
  --green:#00d4aa;--red:#ff4060;--gold:#f5c518;--blue:#4fa3f5;--orange:#f97316;
  --text:#d8e0ec;--muted:#4a5568;--radius:8px;
  --trending-up:#00d4aa;--trending-down:#ff4060;--sideways:#f5c518;--choppy:#4a5568;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;min-height:100vh}
body::before{content:'';position:fixed;inset:0;
  background-image:radial-gradient(ellipse at 20% 20%, rgba(0,212,170,.04) 0%, transparent 50%),
                   radial-gradient(ellipse at 80% 80%, rgba(79,163,245,.04) 0%, transparent 50%);
  pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:0 20px 60px}

/* TOPBAR */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:16px 0 14px;
  border-bottom:1px solid var(--border);margin-bottom:20px;flex-wrap:wrap;gap:10px}
.brand{display:flex;flex-direction:column;gap:2px}
.brand-title{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:.05em;color:#fff}
.brand-title span{color:var(--green);font-size:16px}
.brand-sub{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase}
.topbar-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{font-family:'DM Mono',monospace;font-size:10px;padding:4px 12px;border-radius:3px;
  letter-spacing:.08em;text-transform:uppercase;border:1px solid;cursor:pointer}
.badge-green{color:var(--green);border-color:rgba(0,212,170,.3);background:rgba(0,212,170,.07)}
.badge-red{color:var(--red);border-color:rgba(255,64,96,.3);background:rgba(255,64,96,.07)}
.badge-muted{color:var(--muted);border-color:var(--border);background:var(--surf)}
.badge-orange{color:var(--orange);border-color:rgba(249,115,22,.3);background:rgba(249,115,22,.07)}
.pulse{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:5px}
.pulse.live{background:var(--green);animation:pulse 1.2s infinite}
.pulse.off{background:var(--muted)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(0,212,170,.5)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}

/* GRID */
.grid-main{display:grid;grid-template-columns:1fr 380px;gap:16px;margin-bottom:16px}
.grid-bottom{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:1000px){.grid-main{grid-template-columns:1fr}.grid-bottom{grid-template-columns:1fr}}

/* CARDS */
.card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-title{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase}

/* PRICE CARD */
.price-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);
  padding:24px 28px;margin-bottom:16px;display:grid;grid-template-columns:1fr auto auto;
  align-items:center;gap:20px}
.price-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.15em;text-transform:uppercase;margin-bottom:6px}
.price-value{font-family:'Bebas Neue',sans-serif;font-size:60px;line-height:1;letter-spacing:.02em;color:#fff}
.price-change{font-family:'DM Mono',monospace;font-size:13px;margin-top:4px}
.up{color:var(--green)}.down{color:var(--red)}.flat{color:var(--muted)}
canvas#sparkline{width:100%;height:70px;opacity:.8}

/* MOOD CARD */
.mood-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.mood-regime{font-family:'Bebas Neue',sans-serif;font-size:32px;letter-spacing:.04em;margin:8px 0}
.mood-regime.TRENDING_UP{color:var(--trending-up)}
.mood-regime.TRENDING_DOWN{color:var(--trending-down)}
.mood-regime.SIDEWAYS{color:var(--sideways)}
.mood-regime.CHOPPY{color:var(--choppy)}
.mood-strategy{font-family:'DM Mono',monospace;font-size:11px;padding:4px 12px;border-radius:3px;
  display:inline-block;margin-top:6px}
.mood-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:16px}
.mood-stat{background:var(--surf2);border:1px solid var(--border);border-radius:4px;
  padding:10px 12px;text-align:center}
.mood-stat .val{font-family:'Bebas Neue',sans-serif;font-size:22px}
.mood-stat .lbl{font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-top:2px}

/* OPTION CHAIN */
.option-grid{display:grid;grid-template-columns:1fr 80px 1fr;gap:4px;margin-top:8px}
.opt-ce{background:rgba(0,212,170,.06);border:1px solid rgba(0,212,170,.15);border-radius:4px;
  padding:10px 14px;text-align:left}
.opt-pe{background:rgba(255,64,96,.06);border:1px solid rgba(255,64,96,.15);border-radius:4px;
  padding:10px 14px;text-align:right}
.opt-atm{background:var(--surf2);border:1px solid var(--border);border-radius:4px;
  padding:10px 8px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:center}
.opt-ltp{font-family:'Bebas Neue',sans-serif;font-size:28px;line-height:1}
.opt-ltp.ce{color:var(--green)}.opt-ltp.pe{color:var(--red)}
.opt-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px}
.opt-strike{font-family:'Bebas Neue',sans-serif;font-size:18px;color:var(--text)}
.opt-type{font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);letter-spacing:.1em}
.straddle-prem{font-family:'DM Mono',monospace;font-size:11px;color:var(--gold);
  text-align:center;margin-top:8px;padding:6px;background:rgba(245,197,24,.06);
  border:1px solid rgba(245,197,24,.15);border-radius:4px}

/* SIGNAL */
.signal-card{background:var(--surf);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.signal-box{display:flex;align-items:center;gap:16px;padding:14px 18px;border-radius:6px;margin-bottom:12px}
.signal-box.buy{background:rgba(0,212,170,.08);border:1px solid rgba(0,212,170,.25)}
.signal-box.sell{background:rgba(255,64,96,.08);border:1px solid rgba(255,64,96,.25)}
.signal-box.straddle{background:rgba(245,197,24,.08);border:1px solid rgba(245,197,24,.25)}
.signal-box.none{background:var(--surf2);border:1px solid var(--border)}
.signal-dir{font-family:'Bebas Neue',sans-serif;font-size:36px;line-height:1}
.signal-dir.buy{color:var(--green)}.signal-dir.sell{color:var(--red)}
.signal-dir.straddle{color:var(--gold)}.signal-dir.none{color:var(--muted)}
.signal-meta{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-top:2px}
.signal-details{display:flex;flex-direction:column;gap:4px}
.sig-detail{font-family:'DM Mono',monospace;font-size:10px;padding:3px 8px;
  border-radius:3px;background:var(--surf2);color:var(--muted)}
.sig-detail.pass{background:rgba(0,212,170,.08);border:1px solid rgba(0,212,170,.2);color:var(--green)}
.sig-detail.fail{background:rgba(255,64,96,.08);border:1px solid rgba(255,64,96,.2);color:var(--red)}

/* PAPER TRADES */
.pt-open{background:rgba(0,212,170,.06);border:1px solid rgba(0,212,170,.2);
  border-radius:6px;padding:14px 16px;margin-bottom:8px}
.pt-closed-row{display:grid;
  gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);font-size:12px;
  font-family:'DM Mono',monospace;border-left:3px solid transparent}
.pt-closed-row:last-child{border-bottom:none}
.pt-closed-row.row-win{border-left-color:var(--green);background:rgba(0,212,170,.03)}
.pt-closed-row.row-loss{border-left-color:var(--red);background:rgba(255,64,96,.03)}
.pt-closed-row.row-sq{border-left-color:var(--muted)}
.pt-win{color:var(--green)}.pt-loss{color:var(--red)}.pt-sq{color:var(--muted)}
.pt-stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.pt-stat{background:var(--surf2);border:1px solid var(--border);border-radius:4px;
  padding:10px;text-align:center}
.pt-stat .val{font-family:'Bebas Neue',sans-serif;font-size:24px}
.pt-stat .lbl{font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-top:2px}

/* BACKTEST */
.bt-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.bt-box{background:var(--surf2);border:1px solid var(--border);border-radius:6px;padding:14px}
.bt-wr{font-family:'Bebas Neue',sans-serif;font-size:42px;line-height:1}
.bt-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.bt-sub{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-top:4px}

/* MANUAL TRADE BUTTONS */
.trade-btn{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;padding:8px 16px;border-radius:4px;border:1px solid;
  cursor:pointer;transition:.15s}
.btn-ce{color:var(--green);border-color:rgba(0,212,170,.4);background:rgba(0,212,170,.08)}
.btn-ce:hover{background:rgba(0,212,170,.18)}
.btn-pe{color:var(--red);border-color:rgba(255,64,96,.4);background:rgba(255,64,96,.08)}
.btn-pe:hover{background:rgba(255,64,96,.18)}
.btn-close{color:var(--muted);border-color:var(--border);background:var(--surf2)}
.btn-close:hover{color:var(--text);border-color:#666}

/* LOGIN OVERLAY */
.login-overlay{position:fixed;inset:0;background:rgba(7,9,12,.92);z-index:100;
  display:flex;align-items:center;justify-content:center}
.login-card{background:var(--surf);border:1px solid var(--border);border-radius:16px;
  padding:48px;text-align:center;max-width:420px;width:90%}
.login-icon{font-size:48px;margin-bottom:20px}
.login-title{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:.05em;margin-bottom:8px}
.login-sub{color:var(--muted);font-size:13px;margin-bottom:28px;line-height:1.6}
.login-btn{display:block;width:100%;padding:14px;background:var(--green);color:#000;
  font-family:'DM Mono',monospace;font-size:13px;letter-spacing:.1em;text-transform:uppercase;
  border:none;border-radius:6px;cursor:pointer;font-weight:600;text-decoration:none;
  transition:.15s}
.login-btn:hover{background:#00b894}

/* FOOTER */
.footer{text-align:center;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
  padding:32px 0 0;letter-spacing:.08em}

/* SCROLLBAR */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<!-- Login overlay (shown when Kite not active) -->
<div class="login-overlay" id="login-overlay" style="display:none">
  <div class="login-card">
    <div class="login-icon">🔗</div>
    <div class="login-title">Connect Zerodha</div>
    <div class="login-sub">Login with your Zerodha account to activate real-time data and start paper trading options.</div>
    <a href="/zerodha/login" class="login-btn">Connect Zerodha Kite →</a>
  </div>
</div>

<div class="wrap">

  <!-- TOPBAR -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-title">NIFTY OPTIONS SCANNER <span>v5</span></div>
      <div class="brand-sub">Intraday Options · Kite Connect · Market Mood · Paper Trading</div>
    </div>
    <div class="topbar-right">
      <a href="/" style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
        padding:5px 14px;border-radius:4px;border:1px solid var(--green);color:var(--green);
        background:rgba(0,212,170,.08);text-decoration:none">Dashboard</a>
      <a href="/strategies" style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
        padding:5px 14px;border-radius:4px;border:1px solid var(--border);color:var(--muted);
        background:var(--surf2);text-decoration:none">⚗️ Strategy Lab</a>
      <a href="/history" style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
        padding:5px 14px;border-radius:4px;border:1px solid var(--border);color:var(--muted);
        background:var(--surf2);text-decoration:none">📋 History</a>
      <div id="kite-badge" class="badge badge-muted" onclick="checkKite()">⬤ Kite: Checking…</div>
      <div id="mkt-badge" class="badge badge-muted">⬤ Market</div>
      <div style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">
        <span class="pulse off" id="pulse"></span><span id="last-update">--:--:--</span> IST
      </div>
    </div>
  </div>

  <!-- PRICE + CHART -->
  <div class="price-card">
    <div>
      <div class="price-label">Nifty 50</div>
      <div class="price-value" id="price">--</div>
      <div style="display:flex;align-items:baseline;gap:12px;margin-top:4px;flex-wrap:wrap">
      <div class="price-change flat" id="price-change">+0.00 pts</div>
      <div style="font-family:'DM Mono',monospace;font-size:13px;color:var(--muted)" id="price-pct">(0.00%)</div>
      <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)" id="price-prev">Prev: --</div>
    </div>
    </div>
    <canvas id="sparkline"></canvas>
    <div style="text-align:right">
      <div class="price-label">Next Scan</div>
      <div style="font-family:'Bebas Neue',sans-serif;font-size:28px;color:var(--text)" id="next-scan">--:--:--</div>
      <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-top:4px" id="last-scan">Last: --</div>
      <div style="background:var(--border);height:2px;border-radius:1px;margin-top:8px;width:160px;margin-left:auto">
        <div id="scan-progress" style="background:var(--green);height:100%;width:0%;border-radius:1px;transition:width .5s"></div>
      </div>
    </div>
  </div>

  <!-- MAIN GRID -->
  <div class="grid-main">

    <!-- LEFT: Option Chain + Signal -->
    <div style="display:flex;flex-direction:column;gap:16px">

      <!-- OPTION CHAIN -->
      <div class="card">
        <div class="card-head">
          <span class="card-title">Option Chain · Weekly Expiry</span>
          <span id="expiry-label" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">--</span>
        </div>
        <div class="option-grid">
          <div class="opt-ce" id="ce-box">
            <div class="opt-label">Call (CE)</div>
            <div style="display:flex;align-items:baseline;gap:6px">
              <div class="opt-ltp ce" id="ce-ltp">--</div>
              <div id="ce-arrow" style="font-size:14px;color:var(--green)"></div>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-top:2px" id="ce-sym">--</div>
          </div>
          <div class="opt-atm">
            <div class="opt-type">ATM</div>
            <div class="opt-strike" id="atm-strike">--</div>
          </div>
          <div class="opt-pe" id="pe-box">
            <div class="opt-label" style="text-align:right">Put (PE)</div>
            <div style="display:flex;align-items:baseline;gap:6px;justify-content:flex-end">
              <div id="pe-arrow" style="font-size:14px;color:var(--red)"></div>
              <div class="opt-ltp pe" id="pe-ltp">--</div>
            </div>
            <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-top:2px;text-align:right" id="pe-sym">--</div>
          </div>
        </div>
        <div class="straddle-prem" id="straddle-prem">Straddle Premium: --</div>
        <!-- OTM row -->
        <div style="display:grid;grid-template-columns:1fr 80px 1fr;gap:4px;margin-top:6px">
          <div style="background:var(--surf2);border:1px solid var(--border);border-radius:4px;padding:8px 12px">
            <div style="font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);margin-bottom:2px">+100 OTM CE</div>
            <div style="font-family:'Bebas Neue',sans-serif;font-size:18px;color:var(--green)" id="otm-ce">--</div>
          </div>
          <div></div>
          <div style="background:var(--surf2);border:1px solid var(--border);border-radius:4px;padding:8px 12px;text-align:right">
            <div style="font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);margin-bottom:2px">-100 OTM PE</div>
            <div style="font-family:'Bebas Neue',sans-serif;font-size:18px;color:var(--red)" id="otm-pe">--</div>
          </div>
        </div>
      </div>

      <!-- SIGNAL -->
      <div class="signal-card">
        <div class="card-head">
          <span class="card-title">Signal Engine</span>
          <span id="score-display" style="font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)">0/5</span>
        </div>
        <div class="signal-box none" id="signal-box">
          <div>
            <div class="signal-dir none" id="signal-dir">NO SIGNAL</div>
            <div class="signal-meta" id="signal-strategy">Waiting for scan…</div>
          </div>
        </div>
        <div class="signal-details" id="signal-details"></div>
        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
          <button class="trade-btn btn-ce" onclick="manualTrade('BUY_CE')">+ Buy CE</button>
          <button class="trade-btn btn-pe" onclick="manualTrade('BUY_PE')">+ Buy PE</button>
          <button class="trade-btn btn-close" onclick="closeAllTrades()">Close All</button>
        </div>
      </div>

    </div>

    <!-- RIGHT: Mood + Stats -->
    <div style="display:flex;flex-direction:column;gap:16px">

      <!-- MARKET MOOD -->
      <div class="mood-card" id="mood-card" style="border-left:4px solid var(--muted);transition:border-color .5s">
        <div class="card-title">Market Mood</div>
        <div class="mood-regime UNKNOWN" id="mood-regime">DETECTING…</div>
        <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)" id="mood-label">Waiting for data</div>
        <div class="mood-strategy badge-muted badge" id="mood-strategy" style="margin-top:10px">No strategy yet</div>
        <div class="mood-grid">
          <div class="mood-stat">
            <div class="val" id="mood-adx">--</div>
            <div class="lbl">ADX</div>
          </div>
          <div class="mood-stat">
            <div class="val" id="mood-rsi">--</div>
            <div class="lbl">RSI</div>
          </div>
          <div class="mood-stat">
            <div class="val" id="mood-atr">--</div>
            <div class="lbl">ATR%</div>
          </div>
        </div>
        <div style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);margin-top:12px;padding-top:12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:6px">
          <div style="display:flex;justify-content:space-between">
            <span id="squeeze-status">Squeeze: --</span>
            <span id="vix-display" style="color:var(--muted)">VIX: --</span>
          </div>
          <div style="display:flex;justify-content:space-between">
            <span id="pcr-display" style="color:var(--muted)">PCR: --</span>
            <span id="pcr-sentiment" style="color:var(--muted)">--</span>
          </div>
          <div style="display:flex;justify-content:space-between">
            <span style="color:var(--muted)">CE OI</span>
            <span id="ce-oi" style="color:var(--red)">--</span>
          </div>
          <div style="display:flex;justify-content:space-between">
            <span style="color:var(--muted)">PE OI</span>
            <span id="pe-oi" style="color:var(--green)">--</span>
          </div>
        </div>
      </div>

      <!-- BACKTEST SUMMARY -->
      <div class="card">
        <div class="card-head"><span class="card-title">Backtest (60 days)</span></div>
        <div class="bt-grid">
          <div class="bt-box">
            <div class="bt-label">Directional</div>
            <div class="bt-wr" id="bt-dir-wr" style="color:var(--green)">--%</div>
            <div class="bt-sub" id="bt-dir-trades">-- trades</div>
          </div>
          <div class="bt-box">
            <div class="bt-label">Straddle</div>
            <div class="bt-wr" id="bt-str-wr" style="color:var(--gold)">--%</div>
            <div class="bt-sub" id="bt-str-trades">-- trades</div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <!-- BACKTEST PANEL -->
  <div class="card" style="margin-bottom:16px">
    <div class="card-head">
      <span class="card-title">Interactive Backtest <span style="font-size:9px;color:var(--muted);font-weight:normal">(Daily bars · yfinance · Years of data)</span></span>
      <div id="bt-running" style="font-family:'DM Mono',monospace;font-size:10px;color:var(--gold);display:none">⏳ Running…</div>
    </div>
    <!-- Row 1: Quick buttons -->
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <button class="trade-btn btn-close" id="bt-btn-1d"   onclick="runBacktestDays(1)">Yesterday</button>
      <button class="trade-btn btn-close" id="bt-btn-7d"   onclick="runBacktestDays(7)">Last Week</button>
      <button class="trade-btn btn-close" id="bt-btn-30d"  onclick="runBacktestDays(30)">Last Month</button>
      <button class="trade-btn btn-close" id="bt-btn-60d"  onclick="runBacktestDays(60)">60 Days</button>
      <button class="trade-btn btn-close" id="bt-btn-120d" onclick="runBacktestDays(120)">120 Days</button>
      <button class="trade-btn btn-close" id="bt-btn-365d" onclick="runBacktestDays(365)">1 Year</button>
      <button class="trade-btn btn-close" id="bt-btn-730d" onclick="runBacktestDays(730)">2 Years</button>
      <button class="trade-btn btn-close" id="bt-btn-1825d" onclick="runBacktestDays(1825)">5 Years</button>
    </div>
    <!-- Row 2: Custom date range — always fully visible -->
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px;
      padding:10px 14px;background:var(--surf2);border:1px solid var(--border);border-radius:6px">
      <span style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
        letter-spacing:.1em;text-transform:uppercase">Custom Range:</span>
      <input type="date" id="bt-from" style="font-family:'DM Mono',monospace;font-size:11px;padding:6px 10px;
        border-radius:4px;border:1px solid var(--border);background:var(--bg);color:#e0e0e0;cursor:pointer">
      <span style="font-size:12px;color:var(--muted)">→</span>
      <input type="date" id="bt-to" style="font-family:'DM Mono',monospace;font-size:11px;padding:6px 10px;
        border-radius:4px;border:1px solid var(--border);background:var(--bg);color:#e0e0e0;cursor:pointer">
      <button class="trade-btn btn-ce" onclick="runBacktestRange()" style="padding:6px 16px;font-size:10px">▶ Run Range</button>
    </div>

    <!-- Summary stats -->
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px" id="bt-summary-grid">
      <div class="pt-stat"><div class="val" id="bt-total-trades">--</div><div class="lbl">Trades</div></div>
      <div class="pt-stat"><div class="val" id="bt-overall-wr" style="color:var(--muted)">--%</div><div class="lbl">Win Rate</div></div>
      <div class="pt-stat"><div class="val" id="bt-total-pnl" style="color:var(--muted)">₹--</div><div class="lbl">Total P&L</div></div>
      <div class="pt-stat"><div class="val" id="bt-dir-wr2" style="color:var(--green)">--%</div><div class="lbl">Directional WR</div></div>
      <div class="pt-stat"><div class="val" id="bt-str-wr2" style="color:var(--gold)">--%</div><div class="lbl">Straddle WR</div></div>
    </div>

    <!-- Win rate bar -->
    <div style="background:var(--border);height:6px;border-radius:3px;overflow:hidden;margin-bottom:4px">
      <div id="bt-wr-bar2" style="height:100%;width:0%;background:var(--green);border-radius:3px;transition:width .6s"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-bottom:14px">
      <span id="bt-period">Select a period above</span>
      <span id="bt-capital-end">Final Capital: --</span>
    </div>

    <!-- Trade log table -->
    <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">Trade Log</div>
    <div style="max-height:300px;overflow-y:auto">
      <div style="display:grid;grid-template-columns:75px 40px 120px 160px 90px 80px 80px 90px 90px;gap:6px;
        padding:8px 12px;font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);
        border-bottom:1px solid var(--border)">
        <span>Date</span><span>Day</span><span>Strategy</span><span>Symbol</span>
        <span>Expiry</span><span>Entry ₹</span><span>Exit ₹</span><span>P&L ₹</span><span>Capital ₹</span>
      </div>
      <div id="bt-trade-log">
        <div style="padding:20px;text-align:center;color:var(--muted);font-family:'DM Mono',monospace;font-size:11px">
          Click a period above to run backtest
        </div>
      </div>
    </div>
  </div>

  <!-- PAPER TRADING -->
  <div class="card" style="margin-bottom:16px">
    <div class="card-head">
      <span class="card-title">Paper Trading · Options</span>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="pt-capital" style="font-family:'DM Mono',monospace;font-size:11px;color:var(--green)">₹1,00,000</span>
        <button class="trade-btn btn-close" style="font-size:9px;padding:4px 10px" onclick="resetPaper()">Reset</button>
      </div>
    </div>

    <!-- Stats row -->
    <div class="pt-stat-grid">
      <div class="pt-stat">
        <div class="val" id="pt-total">0</div>
        <div class="lbl">Trades</div>
      </div>
      <div class="pt-stat">
        <div class="val pt-win" id="pt-wins">0</div>
        <div class="lbl">Wins</div>
      </div>
      <div class="pt-stat">
        <div class="val pt-loss" id="pt-losses">0</div>
        <div class="lbl">Losses</div>
      </div>
      <div class="pt-stat" id="pt-wr-box">
        <div class="val" id="pt-wr" style="color:var(--muted)">--%</div>
        <div class="lbl">Win Rate</div>
      </div>
    </div>
    <div style="margin-bottom:10px">
      <div style="background:var(--border);height:6px;border-radius:3px;overflow:hidden">
        <div id="pt-wr-bar" style="height:100%;width:0%;background:var(--green);border-radius:3px;transition:width .6s"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);margin-top:4px">
        <span id="pt-pnl-label">Total P&L: ₹0</span>
        <span id="pt-capital-label">Capital: ₹1,00,000</span>
      </div>
    </div>

    <!-- Open trades -->
    <div id="open-trades-section" style="margin-bottom:12px"></div>

    <!-- Closed trades table -->
    <div style="font-family:'DM Mono',monospace;font-size:9px;color:var(--muted);
      letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">Closed Trades</div>
    <div style="max-height:260px;overflow-y:auto">
      <div class="pt-closed-row" style="color:var(--muted);font-size:10px;border-bottom:1px solid var(--border)">
        <span>Time</span><span>Direction</span><span>Strike</span><span>Entry ₹</span><span>Exit ₹</span><span>P&L ₹</span>
      </div>
      <div id="closed-trades-body">
        <div style="padding:20px;text-align:center;color:var(--muted);font-family:'DM Mono',monospace;font-size:11px">
          No closed trades yet — paper trades open automatically on signals
        </div>
      </div>
    </div>
  </div>

  <div class="footer">
    NIFTY OPTIONS SCANNER v5 · Zerodha Kite Connect · Intraday Paper Trading · Educational Use Only<br>
    Not SEBI registered advice · Options trading involves significant risk · Always use stop-loss
  </div>
</div>

<script>
// ── Init date pickers ──────────────────────────────────────────────────────
(function(){
  const today   = new Date();
  const toStr   = today.toISOString().split('T')[0];
  const from30  = new Date(today); from30.setDate(from30.getDate()-30);
  const fromStr = from30.toISOString().split('T')[0];
  const min400  = new Date(today); min400.setDate(min400.getDate()-400);
  const minStr  = min400.toISOString().split('T')[0];
  const fe = document.getElementById('bt-from');
  const te = document.getElementById('bt-to');
  if(fe){fe.value=fromStr; fe.max=toStr; fe.min=minStr;}
  if(te){te.value=toStr;   te.max=toStr; te.min=minStr;}
})();

function _clearBtButtons(){
  ['1d','7d','30d','60d','120d','365d','730d','1825d'].forEach(id=>{
    const b=document.getElementById('bt-btn-'+id);
    if(b) b.className='trade-btn btn-close';
  });
}

function runBacktestDays(days){
  _clearBtButtons();
  const btn=document.getElementById('bt-btn-'+days+'d');
  if(btn) btn.className='trade-btn btn-ce';
  // Sync date pickers
  const today=new Date();
  const from=new Date(today); from.setDate(from.getDate()-days);
  const fe=document.getElementById('bt-from'), te=document.getElementById('bt-to');
  if(te) te.value=today.toISOString().split('T')[0];
  if(fe) fe.value=from.toISOString().split('T')[0];
  _runBacktest({days});
}

function runBacktestRange(){
  _clearBtButtons();
  const from=document.getElementById('bt-from')?.value;
  const to=document.getElementById('bt-to')?.value;
  if(!from||!to){alert('Select both start and end date');return;}
  if(from>to){alert('Start date must be before end date');return;}
  const days=Math.ceil((new Date(to)-new Date(from))/86400000)+1;
  if(days>3650){alert('Max range is 10 years');return;}
  _runBacktest({from_date:from, to_date:to, days});
}

let _currentRunId = null;

async function _runBacktest(params){
  // Cancel any previous poll immediately
  if(_btPolling){ clearInterval(_btPolling); _btPolling=null; }
  _currentRunId = null;

  const label=params.from_date ? `${params.from_date} → ${params.to_date}` : `${params.days} days`;
  document.getElementById('bt-running').style.display='block';
  document.getElementById('bt-trade-log').innerHTML=
    `<div style="padding:20px;text-align:center;color:var(--muted);font-family:monospace;font-size:11px">⏳ Fetching ${label} of data…</div>`;

  // Reset summary stats
  ['bt-total-trades','bt-overall-wr','bt-total-pnl','bt-dir-wr2','bt-str-wr2'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.textContent='…';
  });
  document.getElementById('bt-period').textContent=`Loading ${label}…`;
  document.getElementById('bt-capital-end').textContent='Final Capital: …';

  // Fire request — server returns a run_id
  let runId = null;
  try{
    const res = await fetch('/api/backtest/run',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});
    const meta = await res.json();
    runId = meta.run_id || null;
    _currentRunId = runId;
  }catch(e){
    console.warn('Failed to start backtest:', e);
    document.getElementById('bt-running').style.display='none';
    return;
  }

  let attempts=0;
  _btPolling=setInterval(async()=>{
    attempts++;
    // If another run started after us, abort this poll
    if(_currentRunId !== runId){ clearInterval(_btPolling); _btPolling=null; return; }
    try{
      const res=await fetch('/api/backtest');
      const d=await res.json();
      // Accept result only if: has overall, not _running, and run_id matches
      const isReady = d.overall && !d._running && (
        !runId ||           // no runId (shouldn't happen)
        !d._run_id ||       // server result has no run_id tag (old result)
        d._run_id === runId // exact match
      );
      if(isReady){
        clearInterval(_btPolling); _btPolling=null;
        document.getElementById('bt-running').style.display='none';
        renderBacktestPanel(d);
        if(d.directional){
          document.getElementById('bt-dir-wr').textContent=(d.directional.win_rate||0)+'%';
          document.getElementById('bt-dir-trades').textContent=(d.directional.trades||0)+' trades · '+(d.days||'--')+' days';
        }
        if(d.straddle){
          document.getElementById('bt-str-wr').textContent=(d.straddle.win_rate||0)+'%';
          document.getElementById('bt-str-trades').textContent=(d.straddle.trades||0)+' trades';
        }
      } else if(attempts>90){
        clearInterval(_btPolling); _btPolling=null;
        document.getElementById('bt-running').style.display='none';
        document.getElementById('bt-trade-log').innerHTML=
          '<div style="padding:20px;text-align:center;color:var(--red);font-family:monospace;font-size:11px">⚠️ Backtest timed out — check yfinance or Kite connection</div>';
      }
    }catch(e){ console.warn('Backtest poll error:', e); }
  },2000);
}

async function runBacktest(days){ runBacktestDays(days); }

const SCAN_INTERVAL_MS = 300000;
let nextScan = Date.now() + SCAN_INTERVAL_MS;
let kiteActive = false;

// ── Helpers ────────────────────────────────────────────────────────────────
function fmt(n, d=2){ return Number(n).toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d}); }
function setText(id, v){ const e=document.getElementById(id); if(e) e.textContent=v; }
function setClass(id, cls){ const e=document.getElementById(id); if(e){ e.className=e.className.replace(/\S*up\S*|\S*down\S*|\S*flat\S*/g,'').trim(); e.classList.add(cls); } }

// ── Price + State fetch (every 3 sec) ─────────────────────────────────────
async function fetchState(){
  try{
    const res  = await fetch('/api/state');
    const d    = await res.json();
    renderPrice(d.nifty);
    renderOptions(d.options);
    renderMood(d.mood);
    renderSignal(d.signal);
    renderScanTimer(d.last_scan);
    updateBadges(d.market_open, d.last_price_update);
    renderVixPcr(d.vix, d.oi_pcr);
  }catch(e){ console.warn('State fetch failed', e); }
}

let _prevNifty = 0;
function renderPrice(n){
  if(!n || !n.price) return;
  setText('price', fmt(n.price, 2));
  const chg  = n.change || 0;
  const pct  = n.pct    || 0;
  const prev = n.prev   || 0;
  const cls  = chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat';
  const sign = chg >= 0 ? '+' : '';
  const arrow = chg > 0 ? ' ▲' : chg < 0 ? ' ▼' : '';
  // Points change
  const chgEl = document.getElementById('price-change');
  if(chgEl){
    chgEl.textContent = `${sign}${fmt(Math.abs(chg),2)} pts${arrow}`;
    chgEl.className   = 'price-change '+cls;
    chgEl.style.fontSize = '18px';
  }
  const pctEl = document.getElementById('price-pct');
  if(pctEl){ pctEl.textContent=`(${sign}${fmt(pct,2)}%)`; pctEl.style.color=chg>0?'var(--green)':chg<0?'var(--red)':'var(--muted)'; }
  const prevEl = document.getElementById('price-prev');
  if(prevEl) prevEl.textContent = `Prev Close: ${prev ? fmt(prev,2) : '--'}`;
  // Flash price on change
  if(_prevNifty && n.price !== _prevNifty){
    const priceEl = document.getElementById('price');
    if(priceEl){
      priceEl.style.transition = 'color .1s';
      priceEl.style.color = n.price > _prevNifty ? 'var(--green)' : 'var(--red)';
      setTimeout(()=>{ priceEl.style.color=''; }, 600);
    }
  }
  _prevNifty = n.price;
}

let _prevCE = 0, _prevPE = 0;
function flashEl(id, dir){
  const el = document.getElementById(id);
  if(!el) return;
  const col = dir > 0 ? 'rgba(0,212,170,.3)' : 'rgba(255,64,96,.3)';
  el.style.transition = 'background .1s';
  el.style.background = col;
  setTimeout(() => { el.style.background = ''; }, 400);
}
function renderOptions(o){
  if(!o || !o.atm) return;
  const ceLtp = o.ce_ltp || 0, peLtp = o.pe_ltp || 0;
  // Arrows + flash on change
  if(_prevCE && ceLtp !== _prevCE){
    const dir = ceLtp > _prevCE ? 1 : -1;
    setText('ce-arrow', dir > 0 ? '▲' : '▼');
    document.getElementById('ce-arrow').style.color = dir > 0 ? 'var(--green)' : 'var(--red)';
    flashEl('ce-box', dir);
  }
  if(_prevPE && peLtp !== _prevPE){
    const dir = peLtp > _prevPE ? 1 : -1;
    setText('pe-arrow', dir > 0 ? '▲' : '▼');
    document.getElementById('pe-arrow').style.color = dir > 0 ? 'var(--green)' : 'var(--red)';
    flashEl('pe-box', dir);
  }
  _prevCE = ceLtp; _prevPE = peLtp;
  setText('ce-ltp', ceLtp ? '₹'+fmt(ceLtp) : '--');
  setText('pe-ltp', peLtp ? '₹'+fmt(peLtp) : '--');
  setText('atm-strike', o.atm ? fmt(o.atm, 0) : '--');
  setText('ce-sym', o.ce_sym ? o.ce_sym.replace('NFO:','') : '--');
  setText('pe-sym', o.pe_sym ? o.pe_sym.replace('NFO:','') : '--');
  setText('otm-ce', o.otm_ce_ltp ? '₹'+fmt(o.otm_ce_ltp) : '--');
  setText('otm-pe', o.otm_pe_ltp ? '₹'+fmt(o.otm_pe_ltp) : '--');
  setText('straddle-prem', o.straddle_premium ? `Straddle Premium: ₹${fmt(o.straddle_premium)} (₹${fmt(o.straddle_premium*50,0)}/lot)` : 'Straddle Premium: --');
  setText('expiry-label', o.expiry ? `Expiry: ${o.expiry}` : '--');
}

function renderVixPcr(vix, oi){
  if(!oi) return;
  // VIX
  const vixEl = document.getElementById('vix-display');
  if(vixEl && vix){
    const vixColor = vix > 20 ? 'var(--red)' : vix > 15 ? 'var(--gold)' : 'var(--green)';
    vixEl.textContent = `VIX: ${vix}`;
    vixEl.style.color = vixColor;
  }
  // PCR
  const pcr = oi.pcr || 0;
  const sent = oi.sentiment || 'NEUTRAL';
  const pcrEl = document.getElementById('pcr-display');
  const sentEl = document.getElementById('pcr-sentiment');
  if(pcrEl) { pcrEl.textContent=`PCR: ${pcr}`; pcrEl.style.color=sent==='BULLISH'?'var(--green)':sent==='BEARISH'?'var(--red)':'var(--muted)'; }
  if(sentEl){ sentEl.textContent=sent; sentEl.style.color=sent==='BULLISH'?'var(--green)':sent==='BEARISH'?'var(--red)':'var(--muted)'; }
  // OI
  const fmt2 = n => n > 1e7 ? (n/1e7).toFixed(1)+'Cr' : n > 1e5 ? (n/1e5).toFixed(1)+'L' : n;
  setText('ce-oi', oi.atm_ce_oi ? fmt2(oi.atm_ce_oi) : '--');
  setText('pe-oi', oi.atm_pe_oi ? fmt2(oi.atm_pe_oi) : '--');
}

function renderMood(m){
  if(!m) return;
  const regimeEl = document.getElementById('mood-regime');
  if(regimeEl){ regimeEl.textContent = m.label || m.regime; regimeEl.className = 'mood-regime '+(m.regime||'UNKNOWN'); }
  setText('mood-adx', m.adx || '--');
  setText('mood-rsi', m.rsi || '--');
  setText('mood-atr', m.atr_pct ? m.atr_pct+'%' : '--');
  setText('squeeze-status', m.squeeze ? '🟡 BB Squeeze Active' : '⚪ No Squeeze');
  // Color mood card border based on regime
  const moodCard = document.getElementById('mood-card');
  if(moodCard){
    const borderMap = {
      'TRENDING_UP':   'var(--green)',
      'TRENDING_DOWN': 'var(--red)',
      'SIDEWAYS':      'var(--gold)',
      'CHOPPY':        'var(--muted)',
      'UNKNOWN':       'var(--muted)',
    };
    moodCard.style.borderLeftColor = borderMap[m.regime] || 'var(--muted)';
  }
  const stratEl = document.getElementById('mood-strategy');
  if(stratEl){
    stratEl.textContent = m.strategy ? `→ ${m.strategy}` : 'No strategy';
    const clsMap = {
      'BUY_CE':'badge-green','BUY_PE':'badge-red',
      'STRADDLE':'badge-orange','IRON_CONDOR':'badge-orange'
    };
    stratEl.className = 'mood-strategy badge ' + (clsMap[m.strategy] || 'badge-muted');
  }
}

function renderSignal(s){
  if(!s) return;
  const box   = document.getElementById('signal-box');
  const dir   = document.getElementById('signal-dir');
  const strat = document.getElementById('signal-strategy');
  const dets  = document.getElementById('signal-details');
  const score = document.getElementById('score-display');
  if(score) score.textContent = (s.score||0)+'/5';
  if(s.trade){
    const isBuy = s.trade.includes('BUY');
    const isStr = s.trade.includes('STRADDLE') || s.trade.includes('CONDOR');
    if(box) box.className = 'signal-box '+(isBuy?'buy':isStr?'straddle':'sell');
    if(dir){ dir.textContent=s.trade.replace('_',' '); dir.className='signal-dir '+(isBuy?'buy':isStr?'straddle':'sell'); }
    if(strat) strat.textContent = s.strategy || '';
  } else {
    if(box) box.className='signal-box none';
    if(dir){ dir.textContent='NO SIGNAL'; dir.className='signal-dir none'; }
    if(strat) strat.textContent = 'Waiting for market conditions…';
  }
  if(dets && s.details){
    dets.innerHTML = s.details.map(d=>{
      const cls = d.startsWith('✅') ? 'pass' : d.startsWith('❌') ? 'fail' : '';
      return `<div class="sig-detail ${cls}">${d}</div>`;
    }).join('');
  }
}

function renderScanTimer(lastScan){
  const now = new Date();
  const next = new Date(nextScan);
  const diff = Math.max(0, Math.floor((nextScan - Date.now()) / 1000));
  const mm = String(Math.floor(diff/60)).padStart(2,'0');
  const ss = String(diff%60).padStart(2,'0');
  // Show IST time
  const istNow = new Date(now.toLocaleString('en-US', {timeZone:'Asia/Kolkata'}));
  const h = String(istNow.getHours()).padStart(2,'0');
  const m = String(istNow.getMinutes()).padStart(2,'0');
  const s = String(istNow.getSeconds()).padStart(2,'0');
  setText('next-scan', `${h}:${m}:${s}`);
  setText('last-scan', lastScan ? `Last: ${lastScan}` : 'Last: --');
  const pct = Math.max(0, 100 - (diff / 300 * 100));
  const prog = document.getElementById('scan-progress');
  if(prog) prog.style.width = pct+'%';
  if(Date.now() >= nextScan){ nextScan = Date.now() + SCAN_INTERVAL_MS; }
}

function updateBadges(marketOpen, lastUpdate){
  const mkt = document.getElementById('mkt-badge');
  if(mkt){
    mkt.textContent = marketOpen ? '⬤ Market Open' : '⬤ Market Closed';
    mkt.className   = 'badge ' + (marketOpen ? 'badge-green' : 'badge-muted');
  }
  const upd = document.getElementById('last-update');
  const pls = document.getElementById('pulse');
  if(upd) upd.textContent = lastUpdate || '--:--:--';
  if(pls) pls.className = 'pulse '+(lastUpdate ? 'live' : 'off');
}

// ── Kite status ────────────────────────────────────────────────────────────
async function checkKite(){
  const badge   = document.getElementById('kite-badge');
  const overlay = document.getElementById('login-overlay');
  try{
    const res = await fetch('/zerodha/status');
    const d   = await res.json();
    kiteActive = d.status === 'active';
    if(!d.api_configured){
      badge.textContent='⬤ Kite: Not Configured'; badge.className='badge badge-muted';
      if(overlay) overlay.style.display='flex';
    } else if(kiteActive){
      const name = d.profile ? ' · '+d.profile.name.split(' ')[0] : '';
      badge.textContent=`⬤ Kite: Live${name}`; badge.className='badge badge-green';
      if(overlay) overlay.style.display='none';
    } else {
      badge.textContent='⬤ Kite: Login'; badge.className='badge badge-orange';
      badge.onclick=()=>window.location.href='/zerodha/login';
      if(overlay) overlay.style.display='flex';
    }
  }catch(e){ badge.textContent='⬤ Kite: Error'; }
}

// ── Paper trades ───────────────────────────────────────────────────────────
async function fetchPaper(){
  try{
    const res = await fetch('/api/paper');
    const d   = await res.json();
    renderPaper(d);
  }catch(e){}
}

function renderPaper(d){
  const stats = d.stats || {};
  const total = stats.total || 0;
  const wins  = stats.wins  || 0;
  const losses= stats.losses|| 0;
  const wr    = total > 0 ? Math.round(wins/total*100) : 0;
  setText('pt-total',   total);
  setText('pt-wins',    wins);
  setText('pt-losses',  losses);
  // Win rate
  const wrEl = document.getElementById('pt-wr');
  if(wrEl){ wrEl.textContent=wr+'%'; wrEl.style.color = wr>=55?'var(--green)':wr>=40?'var(--gold)':'var(--red)'; }
  const wrBar = document.getElementById('pt-wr-bar');
  if(wrBar){ wrBar.style.width=wr+'%'; wrBar.style.background=wr>=55?'var(--green)':wr>=40?'var(--gold)':'var(--red)'; }
  const pnl = stats.pnl_rs || 0;
  const cap = stats.capital || 100000;
  setText('pt-pnl-label', `Total P&L: ${pnl>=0?'+':''}₹${fmt(Math.abs(pnl),0)}`);
  document.getElementById('pt-pnl-label').style.color = pnl>=0?'var(--green)':'var(--red)';
  setText('pt-capital-label', `Capital: ₹${fmt(cap,0)}`);
  setText('pt-capital', '₹'+fmt(cap,0));

  // Open trades
  const openSec = document.getElementById('open-trades-section');
  if(openSec){
    if(d.open_trades && d.open_trades.length > 0){
      openSec.innerHTML = d.open_trades.map(t => {
        const pnl   = t.pnl_rs || 0;
        const pnlCls = pnl >= 0 ? 'pt-win' : 'pt-loss';
        const sign  = pnl >= 0 ? '+' : '';
        return `<div class="pt-open">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div>
              <span style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">${t.entry_time}</span>
              <span style="font-family:'Bebas Neue',sans-serif;font-size:18px;margin:0 10px;color:var(--green)">${t.direction}</span>
              <span style="font-family:'DM Mono',monospace;font-size:11px">${(t.symbol||'').replace('NFO:','')}</span>
            </div>
            <div style="text-align:right">
              <div style="font-family:'DM Mono',monospace;font-size:10px;color:var(--muted)">Entry ₹${fmt(t.entry_ltp)} → Now ₹${fmt(t.current_ltp)} &nbsp;|&nbsp; SL ₹${fmt(t.sl)} TP ₹${fmt(t.tp)}${t.trailing_active?' 🔒 Trail':''}  </div>
              <div style="font-family:'Bebas Neue',sans-serif;font-size:20px" class="${pnlCls}">${sign}₹${fmt(Math.abs(pnl),0)} <span style="font-size:12px;font-family:monospace">(${t.pnl_pct||0}%)</span></div>
            </div>
          </div>
          <div style="display:flex;gap:16px;margin-top:8px;font-family:'DM Mono',monospace;font-size:9px;color:var(--muted)">
            <span>SL ₹${fmt(t.sl)} · TP ₹${fmt(t.tp)} · Qty ${t.qty}</span>
          </div>
        </div>`;
      }).join('');
    } else {
      openSec.innerHTML='';
    }
  }

  // Closed trades
  const tbody = document.getElementById('closed-trades-body');
  if(tbody){
    if(d.closed_trades && d.closed_trades.length > 0){
      tbody.innerHTML = d.closed_trades.slice(0,20).map(t => {
        const cls    = t.result==='WIN'?'pt-win':t.result==='LOSS'?'pt-loss':'pt-sq';
        const rowCls = t.result==='WIN'?'row-win':t.result==='LOSS'?'row-loss':'row-sq';
        const icon   = t.result==='WIN'?'✅':t.result==='LOSS'?'❌':'⏱';
        const pnl    = t.pnl_rs || 0;
        return `<div class="pt-closed-row ${rowCls}">
          <span>${t.entry_time||'--'}</span>
          <span>${t.direction||'--'}</span>
          <span>${t.strike||'--'}</span>
          <span>₹${fmt(t.entry_ltp)}</span>
          <span>₹${fmt(t.exit_ltp||0)}</span>
          <span class="${cls}">${icon} ${pnl>=0?'+':''}₹${fmt(Math.abs(pnl),0)}</span>
        </div>`;
      }).join('');
    } else {
      tbody.innerHTML='<div style="padding:16px;text-align:center;color:var(--muted);font-family:\'DM Mono\',monospace;font-size:11px">No closed trades yet</div>';
    }
  }
}

// Backtest
async function fetchBacktest(){
  try{
    const res = await fetch('/api/backtest');
    const d   = await res.json();
    window._lastBtData = d;
    if(d.directional){
      setText('bt-dir-wr',    (d.directional.win_rate||0)+'%');
      setText('bt-dir-trades',(d.directional.trades||0)+' trades · '+(d.days||'--')+' days');
    }
    if(d.straddle){
      setText('bt-str-wr',    (d.straddle.win_rate||0)+'%');
      setText('bt-str-trades',(d.straddle.trades||0)+' trades');
    }
    if(d.overall) renderBacktestPanel(d);
    return d;
  }catch(e){ return {}; }
}

let _btPolling = null;

function renderBacktestPanel(d){
  if(!d || !d.overall) return;
  const ov  = d.overall;
  const wr  = ov.win_rate || 0;
  const pnl = ov.pnl || 0;

  setText('bt-total-trades', ov.trades || 0);
  const wrEl = document.getElementById('bt-overall-wr');
  if(wrEl){ wrEl.textContent=wr+'%'; wrEl.style.color=wr>=55?'var(--green)':wr>=40?'var(--gold)':'var(--red)'; }
  const pnlEl = document.getElementById('bt-total-pnl');
  if(pnlEl){ pnlEl.textContent=(pnl>=0?'+':'')+fmt(Math.abs(pnl),0); pnlEl.style.color=pnl>=0?'var(--green)':'var(--red)'; }
  setText('bt-dir-wr2',  (d.directional?.win_rate||0)+'%');
  setText('bt-str-wr2',  (d.straddle?.win_rate||0)+'%');
  setText('bt-period',   d.period || '');
  setText('bt-capital-end', 'Final Capital: ₹'+fmt(ov.final_capital||100000, 0));

  const bar = document.getElementById('bt-wr-bar2');
  if(bar){ bar.style.width=wr+'%'; bar.style.background=wr>=55?'var(--green)':wr>=40?'var(--gold)':'var(--red)'; }

  // Trade log
  const log = document.getElementById('bt-trade-log');
  if(log && d.trade_log && d.trade_log.length > 0){
    log.innerHTML = d.trade_log.map(t => {
      const cls    = t.result==='WIN'?'pt-win':t.result==='LOSS'?'pt-loss':'pt-sq';
      const rowCls = t.result==='WIN'?'row-win':t.result==='LOSS'?'row-loss':'row-sq';
      const icon   = t.result==='WIN'?'✅':t.result==='LOSS'?'❌':'⏱';
      const pnl    = t.pnl_rs || 0;
      const pnlPct = t.pnl_pct || 0;
      const moodColor = t.mood==='TRENDING_UP'?'var(--green)':t.mood==='TRENDING_DOWN'?'var(--red)':t.mood==='SIDEWAYS'?'var(--gold)':'var(--muted)';
      const sym = (t.symbol||'--');
      const COLS = '75px 40px 120px 160px 90px 80px 80px 90px 90px';
      return `<div class="pt-closed-row ${rowCls}" style="grid-template-columns:${COLS};font-size:10px">
        <span style="color:var(--muted)">${t.date}</span>
        <span style="color:var(--muted)">${t.weekday||''}</span>
        <span style="color:${moodColor}">${t.strategy}</span>
        <span style="color:var(--blue);font-size:9px;word-break:break-all" title="Strike:${t.strike} Expiry:${t.expiry}">${sym}</span>
        <span style="font-size:9px;color:var(--muted)">${t.expiry||'--'}</span>
        <span>₹${fmt(t.entry)}<br><span style="font-size:8px;color:var(--muted)">${t.entry_time||'09:30'}</span></span>
        <span>₹${fmt(t.exit||0)}<br><span style="font-size:8px;color:var(--muted)">${t.exit_time||'15:15'}</span></span>
        <span class="${cls}">${icon} ${pnl>=0?'+':''}₹${fmt(Math.abs(pnl),0)}<br><span style="font-size:8px">${pnlPct>0?'+':''}${pnlPct}%</span></span>
        <span style="color:var(--muted)">₹${fmt(t.capital||0,0)}</span>
      </div>`;
    }).join('');
  } else if(log){
    log.innerHTML = '<div style="padding:16px;text-align:center;color:var(--muted);font-family:monospace;font-size:11px">No trades found for this period</div>';
  }
}

// Sparkline
function drawSparkline(candles){
  const canvas = document.getElementById('sparkline');
  if(!canvas || !candles || candles.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 300, H = 70;
  canvas.width = W; canvas.height = H;
  ctx.clearRect(0,0,W,H);
  const prices = candles.map(c=>c.c);
  const mn = Math.min(...prices), mx = Math.max(...prices);
  const range = mx - mn || 1;
  const x = i => (i / (prices.length-1)) * W;
  const y = v => H - ((v - mn) / range) * (H - 8) - 4;
  const last = prices[prices.length-1];
  const first = prices[0];
  const color = last >= first ? '#00d4aa' : '#ff4060';
  ctx.beginPath();
  prices.forEach((p,i) => i===0 ? ctx.moveTo(x(i),y(p)) : ctx.lineTo(x(i),y(p)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
  // Fill
  ctx.lineTo(x(prices.length-1), H); ctx.lineTo(x(0), H); ctx.closePath();
  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, last>=first?'rgba(0,212,170,.25)':'rgba(255,64,96,.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad; ctx.fill();
}

// Manual trades
async function manualTrade(direction){
  await fetch('/api/paper/open', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({direction, strategy:'Manual'})});
  fetchPaper();
}

async function closeAllTrades(){
  await fetch('/api/paper/close_all', {method:'POST'});
  fetchPaper();
}

async function resetPaper(){
  if(!confirm('Reset all paper trades?')) return;
  await fetch('/api/paper/reset', {method:'POST'});
  fetchPaper();
}

// Candles state for sparkline
let lastCandles = [];
async function fetchCandles(){
  try{
    const res = await fetch('/api/state');
    const d = await res.json();
    if(d.candles && d.candles.length > 1){
      lastCandles = d.candles;
      drawSparkline(lastCandles);
    }
  }catch(e){}
}

// ── Init ───────────────────────────────────────────────────────────────────
checkKite();
fetchState();
fetchPaper();
fetchCandles();
// On load: fetch existing result, if empty auto-run 30d
fetchBacktest().then(()=>{
  const bt = window._lastBtData;
  if(!bt || !bt.overall) runBacktestDays(365);
});

// Backtest period buttons
document.querySelectorAll('[data-days]').forEach(btn => {
  btn.addEventListener('click', () => runBacktest(parseInt(btn.dataset.days)));
});

// Price updates every 3s
setInterval(fetchState, 3000);
// Paper trades every 10s
setInterval(fetchPaper, 10000);
// Backtest every 5min
setInterval(fetchBacktest, 300000);
// Kite status every 5min
setInterval(checkKite, 300000);
// Candles every 30s
setInterval(fetchCandles, 30000);
// Scan timer tick every second
setInterval(() => renderScanTimer(null), 1000);
</script>
</body>
</html>"""

# ─── API ROUTES ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/history")
def history_page():
    """Full trade history page — all paper trades from DB."""
    return render_template_string(HISTORY_HTML)

@app.route("/api/state")
def api_state():
    import math
    def safe(obj, depth=0):
        if depth > 8: return str(obj)
        if obj is None or isinstance(obj, bool): return obj
        if isinstance(obj, float):
            return None if (math.isnan(obj) or math.isinf(obj)) else obj
        if isinstance(obj, (int, str)): return obj
        if hasattr(obj, 'item'):
            v = obj.item()
            return None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v
        if hasattr(obj, 'isoformat'): return obj.isoformat()
        if isinstance(obj, dict): return {str(k): safe(v, depth+1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)): return [safe(i, depth+1) for i in obj]
        try: return str(obj)
        except: return None
    with state_lock:
        return jsonify(safe(dict(state)))

@app.route("/api/backtest")
def api_backtest():
    with state_lock:
        return jsonify(state.get("backtest", {}))

@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    """Run backtest for a specific period — accepts days OR from_date+to_date."""
    body      = request.get_json() or {}
    days      = int(body.get("days", 30))
    from_date = body.get("from_date")
    to_date   = body.get("to_date")
    days = min(max(days, 1), 730)

    import uuid
    run_id = str(uuid.uuid4())[:8]

    # Clear previous result — include run_id so JS knows when fresh result arrives
    with state_lock:
        state["backtest"] = {"_running": True, "_run_id": run_id}

    def _run(rid=run_id):
        print(f"[BT] Thread started rid={rid} days={days}", flush=True)
        try:
            bt = run_backtest(days, from_date=from_date, to_date=to_date)
            print(f"[BT] run_backtest returned: {type(bt)} keys={list(bt.keys()) if isinstance(bt,dict) else 'N/A'}", flush=True)
            if not isinstance(bt, dict):
                bt = {"error": "Backtest returned invalid result"}
            bt["_run_id"] = rid
        except Exception as e:
            print(f"[BT] EXCEPTION: {e}", flush=True)
            import traceback; traceback.print_exc()
            bt = {"error": str(e), "_run_id": rid}
        with state_lock:
            state["backtest"] = bt
        print(f"[BT] Done — state updated", flush=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "running", "days": days, "run_id": run_id})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "triggered"})

@app.route("/api/paper")
def api_paper():
    import math
    def safe(obj, depth=0):
        if depth > 8: return str(obj)
        if obj is None or isinstance(obj, bool): return obj
        if isinstance(obj, float): return None if (math.isnan(obj) or math.isinf(obj)) else obj
        if isinstance(obj, (int, str)): return obj
        if hasattr(obj, 'item'): return obj.item()
        if hasattr(obj, 'isoformat'): return obj.isoformat()
        if isinstance(obj, dict): return {str(k): safe(v, depth+1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)): return [safe(i, depth+1) for i in obj]
        return str(obj)
    with paper_lock:
        return jsonify(safe(dict(paper_state)))

@app.route("/api/paper/open", methods=["POST"])
def api_paper_open():
    """Manual paper trade open."""
    body      = request.get_json() or {}
    direction = body.get("direction", "BUY_CE")
    strategy  = body.get("strategy", "Manual")
    with state_lock:
        spot = state["nifty"].get("price", 0)
        opts = state["options"]
    if not spot:
        return jsonify({"error": "No price data"}), 400
    atm = opts.get("atm", round(spot / 50) * 50)
    if direction == "BUY_CE":
        ltp = opts.get("ce_ltp", 0) or 100.0
        open_paper_trade("BUY_CE", opts.get("ce_sym", f"NIFTY{atm}CE"), ltp, strategy, spot, atm, "CE")
    else:
        ltp = opts.get("pe_ltp", 0) or 100.0
        open_paper_trade("BUY_PE", opts.get("pe_sym", f"NIFTY{atm}PE"), ltp, strategy, spot, atm, "PE")
    return jsonify({"status": "opened"})

@app.route("/api/paper/close_all", methods=["POST"])
def api_paper_close_all():
    with paper_lock:
        now = datetime.now(IST)
        for t in paper_state["open_trades"]:
            t["status"]    = "CLOSED"
            t["exit_ltp"]  = t["current_ltp"]
            t["exit_time"] = now.strftime("%H:%M:%S")
            t["result"]    = "MANUAL"
            paper_state["closed_trades"].insert(0, t)
            paper_state["stats"]["total"] += 1
            paper_state["stats"]["pnl_rs"] = round(paper_state["stats"]["pnl_rs"] + t["pnl_rs"], 2)
        paper_state["open_trades"] = []
        _save_paper()
    return jsonify({"status": "closed"})

@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    with paper_lock:
        paper_state["open_trades"]   = []
        paper_state["closed_trades"] = []
        paper_state["stats"] = {"total":0,"wins":0,"losses":0,"pnl_rs":0.0,"capital":100000.0}
        _save_paper()
    return jsonify({"status": "reset"})

# ─── ZERODHA AUTH ─────────────────────────────────────────────────────────────
@app.route("/zerodha/login")
def zerodha_login():
    if not KITE_API_KEY:
        return "<h2>KITE_API_KEY not set in Railway variables</h2>", 400
    kc = KiteConnect(api_key=KITE_API_KEY)
    return redirect(kc.login_url())

@app.route("/zerodha/callback")
def zerodha_callback():
    global kite_session
    request_token = request.args.get("request_token")
    if not request_token or request.args.get("status") != "success":
        return f"<h2>Login failed: {request.args.get('message','')}</h2>", 400
    try:
        kc   = KiteConnect(api_key=KITE_API_KEY)
        data = kc.generate_session(request_token, api_secret=KITE_API_SECRET)
        kc.set_access_token(data["access_token"])
        profile = kc.profile()
        with kite_lock:
            kite_session = kc
        _save_token(data["access_token"])
        log.info(f"✅ Manual login: {profile.get('user_name')}")
        fetch_lot_size()
        # Trigger scan + backtest
        def _post():
            time.sleep(1); bt = run_backtest()
            with state_lock: state["backtest"] = bt
            run_scan()
        threading.Thread(target=_post, daemon=True).start()
        return redirect("/")
    except Exception as e:
        return f"<h2>Session error: {e}</h2>", 500

@app.route("/zerodha/status")
def zerodha_status():
    profile = None
    if _kite_active():
        try:
            with kite_lock: kc = kite_session
            p = kc.profile()
            profile = {"name": p.get("user_name"), "email": p.get("email")}
        except: pass
    ts = "active" if _kite_active() else "logged_out"
    return jsonify({"status": ts, "profile": profile,
                    "api_configured": bool(KITE_API_KEY), "kite_available": KITE_AVAILABLE})

@app.route("/strategies")
def strategies_page():
    return render_template_string(STRATEGIES_HTML)

@app.route("/api/strategies/list")
def api_strategies_list():
    return jsonify(STRATEGIES)

@app.route("/api/strategies/compare", methods=["POST"])
def api_strategies_compare():
    body = request.get_json() or {}
    ids  = body.get("strategies", ["our_combined", "straddle_only"])
    days = min(max(int(body.get("days", 30)), 1), 90)
    def _run():
        result = compare_strategies(ids, days)
        with state_lock:
            state["last_comparison"] = result
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "running", "strategies": ids, "days": days})

@app.route("/api/strategies/result")
def api_strategies_result():
    with state_lock:
        return jsonify(state.get("last_comparison", {}))

@app.route("/api/paper/history")
def api_paper_history():
    """Query closed trades with filters: days, result, strategy."""
    days     = int(request.args.get("days", 30))
    result   = request.args.get("result", "")
    strategy = request.args.get("strategy", "")

    conn = _pg_conn()
    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                where = ["status = 'CLOSED'"]
                if days < 999:
                    where.append(f"closed_at >= NOW() - INTERVAL '{days} days'")
                if result:
                    where.append(f"data->>'result' = '{result}'")
                if strategy:
                    where.append(f"data->>'strategy' ILIKE '%{strategy}%'")
                sql = ("SELECT data FROM paper_trades WHERE " +
                       " AND ".join(where) +
                       " ORDER BY closed_at DESC LIMIT 500")
                cur.execute(sql)
                trades = [dict(r["data"]) for r in cur.fetchall()]
            conn.close()
            return jsonify({"trades": trades, "source": "db"})
        except Exception as e:
            log.warning(f"History query failed: {e}")
            try: conn.close()
            except: pass

    # Fallback: filter from in-memory closed trades
    with paper_lock:
        trades = list(paper_state["closed_trades"])
    if result:
        trades = [t for t in trades if t.get("result") == result]
    if strategy:
        trades = [t for t in trades if strategy.lower() in (t.get("strategy") or "").lower()]
    return jsonify({"trades": trades[:200], "source": "memory"})


@app.route("/zerodha/logout", methods=["POST"])
def zerodha_logout():
    global kite_session
    with kite_lock:
        try:
            if kite_session: kite_session.invalidate_access_token()
        except: pass
        kite_session = None
    if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    return jsonify({"status": "logged_out"})

# ─── STARTUP ──────────────────────────────────────────────────────────────────
def scheduled_login_loop():
    """
    Runs daily auto-login at 8:55 AM IST.
    Only triggers if TOTP credentials are set AND not already logged in today.
    This minimises session disruption — fires once per day at a predictable time.
    """
    logged_today = None
    while True:
        now   = datetime.now(IST)
        today = now.date()
        # Fire at 8:55 AM IST (± 30 sec window)
        if (now.hour == 8 and now.minute == 55 and logged_today != today):
            if not _kite_active():
                log.info("⏰ 8:55 AM — triggering scheduled auto-login...")
                if _auto_login():
                    logged_today = today
                    # Give user 5 min to re-login on mobile before market opens
                    log.info("✅ API login done. Re-login on Zerodha mobile now (before 9:15 AM)")
                    # Kick off backtest + scan after login
                    def _post():
                        time.sleep(2)
                        bt = run_backtest()
                        with state_lock:
                            state["backtest"] = bt
                        run_scan()
                    threading.Thread(target=_post, daemon=True).start()
            else:
                log.info("⏰ 8:55 AM — Kite already active, skipping auto-login")
                logged_today = today
        time.sleep(30)


def _start_background():
    _load_paper()
    # On startup: try to restore today's saved token only (no auto-login)
    # Auto-login happens at 8:55 AM via scheduled_login_loop
    _load_token()

    def _init():
        time.sleep(2)
        if _kite_active():
            bt = run_backtest()
            with state_lock:
                state["backtest"] = bt
            run_scan()
        else:
            log.info("Kite not active on startup — waiting for 8:55 AM auto-login or manual /zerodha/login")

    threading.Thread(target=_init,              daemon=True).start()
    threading.Thread(target=scan_loop,          daemon=True).start()
    threading.Thread(target=price_loop,         daemon=True).start()
    threading.Thread(target=scheduled_login_loop, daemon=True).start()
    log.info(f"🚀 Nifty Options Scanner v5 | Port {PORT} | Kite: {_kite_active()}")

_start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)