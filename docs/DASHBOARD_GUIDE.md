# Citadel Bot Dashboard

The Citadel Bot Dashboard is a Streamlit-based web interface for managing the trading bot, monitoring positions, and configuring settings.

## Quick Start

### Option 1: Recommended (Auto-launch with Browser)
From the project root:
```bash
python launch_dashboard.py
```

This will:
- Launch the Streamlit server
- Automatically open the dashboard in your default browser
- Display login credentials and helpful information

### Option 2: Manual Streamlit
From the `citadel_bot/` directory:
```bash
streamlit run dashboard.py
```

Then open your browser to: `http://localhost:8501`

### Option 3: Using the Runner Script
```bash
python citadel_bot/run_dashboard.py
```

## Default Credentials

**Username:** `admin`  
**Password:** ``

## Production Setup

For production, set environment variables to secure the dashboard:

```bash
# Windows (PowerShell)
$env:CITADEL_DASHBOARD_USER="your_username"
$env:CITADEL_DASHBOARD_PASS="your_secure_password"

# Windows (Command Prompt)
set CITADEL_DASHBOARD_USER=your_username
set CITADEL_DASHBOARD_PASS=your_secure_password

# Linux/macOS
export CITADEL_DASHBOARD_USER=your_username
export CITADEL_DASHBOARD_PASS=your_secure_password
```

## Features

- **🔐 Secure Login** — Environment variable-based credentials
- **📊 Account Metrics** — Real-time equity, balance, and P&L
- **🛠️ MT5 Configuration** — Connect/disconnect with broker credentials
- **📈 Instrument Setup** — Select indices, forex, commodities from catalog
- **💱 Trading Mode** — Toggle between paper and live trading
- **📋 Position Viewer** — Open positions and orders tables
- **💰 Trade History** — Ledger with filters by date and instrument
- **⚠️ Risk Monitor** — Real-time risk utilization and suggestions
- **🔄 Auto-Refresh** — Configurable refresh interval

## Troubleshooting

### "ScriptRunContext" Warnings
These warnings occur if you run `python dashboard.py` directly instead of using `streamlit run`. Simply use one of the recommended launch methods above.

### Dashboard Not Opening
1. Check that Streamlit is installed: `pip install streamlit`
2. If using `launch_dashboard.py`, verify Chrome is installed or manually open `http://localhost:8501`
3. Check for firewall rules blocking localhost:8501

### Login Issues
- Verify credentials (default: admin / change_me_now)
- Check environment variables are set correctly if using custom credentials

## Architecture

- **Framework:** Streamlit (Python web framework)
- **Backend:** Citadel Bot core (asyncio-based)
- **Database:** PostgreSQL (optional, for analytics)
- **MT5 Connection:** MetaTrader 5 API

## Next Steps

1. Configure your MT5 broker credentials in the dashboard
2. Select your trading instruments (indices/forex/commodities)
3. Choose trading mode (paper or live)
4. Monitor positions and risk metrics in real-time
