import os
import sys
from app import create_app

app = create_app()

if __name__ == '__main__':
    os.environ["PYTHONLEGACYWINDOWSSTDIO"] = "1"
    os.environ["NO_COLOR"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    try:
        from flask import cli
        cli.show_server_banner = lambda *args, **kwargs: None
    except Exception:
        pass

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    print("\n" + "="*50)
    print("🚀 自助點餐系統模組化架構啟動中...")
    print("前台首頁: http://127.0.0.1:5000/")
    print("店家後台: http://127.0.0.1:5000/admin")
    print("="*50 + "\n")

    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)