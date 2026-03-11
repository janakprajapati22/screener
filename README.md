# Nifty 50 Signal Scanner v4
7 Strategies + Zerodha Kite Connect + Paper Trade Tracker

## Deploy to Railway

### 1. Set environment variables in Railway dashboard:
```
KITE_API_KEY     = your_zerodha_api_key
KITE_API_SECRET  = your_zerodha_api_secret
RAILWAY_URL      = https://nifty-screener-production.up.railway.app
PORT             = (set automatically by Railway)
```

### 2. Set Zerodha redirect URL
In developers.kite.trade → your app → Redirect URL:
```
https://nifty-screener-production.up.railway.app/zerodha/callback
```

### 3. Push this folder to GitHub → Railway auto-deploys

---

## Daily Login (every morning 8:50–9:10 AM IST)
Open in browser:
```
https://nifty-screener-production.up.railway.app/zerodha/login
```
Log in with Zerodha credentials → real-time data activates for the day.

---

## Routes
| Route | Description |
|---|---|
| `/` | Main dashboard |
| `/api/state` | Full scanner state JSON |
| `/api/scan` (POST) | Manual scan trigger |
| `/api/backtest` | Backtest results JSON |
| `/api/paper` | Paper trade state |
| `/api/paper/open` (POST) | Open manual paper trade |
| `/api/paper/close` (POST) | Close open paper trade |
| `/api/paper/reset` (POST) | Reset paper trade history |
| `/zerodha/login` | Redirect to Zerodha login |
| `/zerodha/callback` | OAuth callback (set this in Kite developer portal) |
| `/zerodha/status` | Kite token status JSON |
| `/zerodha/logout` (POST) | Logout from Kite |

---

## Data Source
- **With Kite token**: Real-time LTP + exact 5-min OHLCV bars from Zerodha
- **Without token**: yfinance ~15-min delayed data (fallback, always works)

## Strategies
1. ORB — Opening Range Breakout
2. EMA 9/21/50 Crossover
3. VWAP Band
4. S&R Reversal
5. Supertrend (ATR-10, Factor-3)
6. 9:20 Short Straddle (88% win rate)
7. Iron Condor (85% win rate)

## Keep Alive
Set up UptimeRobot to ping `/api/state` every 5 minutes to prevent Railway sleep.
