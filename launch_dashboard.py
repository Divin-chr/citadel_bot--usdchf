#!/usr/bin/env python3
"""
Citadel Bot Dashboard Launcher

Run this script to start the dashboard with proper Streamlit configuration.
The dashboard will open in your default browser with the trading interface.

Usage:
    python launch_dashboard.py
"""

import os
import subprocess
import sys
from pathlib import Path

def main():
    """Launch the Citadel Bot dashboard."""
    dashboard_path = Path(__file__).parent / "citadel_bot" / "dashboard.py"
    
    if not dashboard_path.exists():
        print(f"❌ Error: Dashboard file not found at {dashboard_path}")
        return 1
    
    print("=" * 70)
    print("Starting Citadel Bot Dashboard")
    print("=" * 70)
    print("\nDashboard is starting in your browser...")
    print("   Login credentials (default):")
    print("   • Username: admin")
    print("   • Password: change_me_now")
    print("\nSet environment variables for production:")
    print("   • CITADEL_DASHBOARD_USER=your_username")
    print("   • CITADEL_DASHBOARD_PASS=your_password")
    print("\nPress Ctrl+C to stop the dashboard\n")
    print("=" * 70 + "\n")
    
    try:
        port = os.environ.get('PORT', '8501')
        cmd = [sys.executable, "-m", "streamlit", "run", str(dashboard_path), "--server.port", port, "--server.headless", "true", "--server.address", "0.0.0.0"]
        proc = subprocess.run(cmd, cwd=str(Path(__file__).parent))
        return proc.returncode
    except KeyboardInterrupt:
        print("\n\nDashboard stopped.")
        return 0
    except Exception as e:
        print(f"Error starting dashboard: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
