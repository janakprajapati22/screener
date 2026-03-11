# Nifty 50 Signal Scanner — Railway Deploy Guide

## Deploy in 5 steps (no CLI needed)

### Step 1 — Push to GitHub
1. Go to **github.com** → New repository → name it `nifty-scanner`
2. Upload all files in this folder (drag & drop on GitHub UI)
   - `nifty_scanner.py`
   - `requirements.txt`
   - `Procfile`
   - `railway.toml`
   - `.gitignore`

### Step 2 — Deploy on Railway
1. Go to **railway.app** → Sign up free (use GitHub login)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your `nifty-scanner` repo
4. Railway auto-detects Python + installs requirements
5. Wait ~2 minutes for build to complete

### Step 3 — Get your public URL
1. In Railway dashboard → click your service
2. Go to **Settings** → **Networking** → **Generate Domain**
3. You'll get a URL like: `https://nifty-scanner-production.up.railway.app`
4. Open it — your live dashboard is running!

### Step 4 — Keep it alive (important!)
Railway free tier sleeps after 30 min of inactivity.
To prevent this, use **UptimeRobot** (free):
1. Go to **uptimerobot.com** → Add Monitor
2. Type: HTTP(s)
3. URL: `https://your-app.up.railway.app/api/state`
4. Interval: every 5 minutes
5. This pings your app so it never sleeps

---

## File structure
```
nifty-scanner/
├── nifty_scanner.py    ← main app (Flask + signal engine)
├── requirements.txt    ← Python dependencies
├── Procfile            ← tells Railway how to start
├── railway.toml        ← Railway config
└── .gitignore
```

## Free tier limits
- Railway free tier: **500 hours/month** (~20 days)
- To get unlimited: add a credit card (still free up to $5/month usage)
- The scanner uses very little CPU — stays well within free limits

## URLs once deployed
| URL | What |
|-----|------|
| `https://your-app.up.railway.app/` | Live dashboard |
| `https://your-app.up.railway.app/api/state` | JSON state |
| `POST https://your-app.up.railway.app/api/scan` | Force scan |

## Upgrade to real-time later
When you open an Angel One account, just update these 3 functions
in `nifty_scanner.py`:
- `fetch_nifty_quote()` → use SmartAPI quote endpoint
- `fetch_nifty_history_yf()` → use SmartAPI candle endpoint  
- `fetch_nse_option_chain()` → use SmartAPI option chain

The entire signal engine stays the same.
