import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path

    chrome = shutil.which("chrome") or shutil.which("google-chrome")
    if chrome:
        return chrome

    raise FileNotFoundError(
        "Google Chrome not found. Install Chrome or set the BROWSER environment variable manually."
    )


def main() -> int:
    # Set environment for Streamlit
    env = os.environ.copy()
    env["STREAMLIT_LOGGER_LEVEL"] = "error"
    env["TZ"] = "UTC"
    
    # Try to find Chrome
    try:
        chrome_path = find_chrome()
        env["BROWSER"] = chrome_path
        auto_open = True
    except FileNotFoundError:
        auto_open = False

    cmd = [sys.executable, "-m", "streamlit", "run", "dashboard.py", "--logger.level=error"]
    
    print("\n" + "="*70)
    print("🚀 CITADEL BOT DASHBOARD")
    print("="*70)
    print("\n📊 Dashboard is starting...\n")
    print("🔐 Default credentials:")
    print("   • Username: admin")
    print("   • Password: change_me_now")
    print("\n💡 Change these in production by setting environment variables:")
    print("   • CITADEL_DASHBOARD_USER=your_username")
    print("   • CITADEL_DASHBOARD_PASS=your_password")
    
    if auto_open:
        print(f"\n🌐 Browser will open automatically using: {chrome_path}")
    else:
        print("\n🌐 Open your browser to: http://localhost:8501")
    
    print("\n⏹️  Press Ctrl+C to stop the dashboard")
    print("="*70 + "\n")

    proc = subprocess.Popen(cmd, env=env)
    try:
        proc.wait()
        return proc.returncode
    except KeyboardInterrupt:
        print("\n✅ Dashboard stopped.")
        proc.terminate()
        proc.wait()
        return proc.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
