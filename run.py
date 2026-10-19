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

    is_debug = os.getenv("FLASK_DEBUG", "0") == "1"
    bind_host = os.getenv("FLASK_HOST", "127.0.0.1" if is_debug else "0.0.0.0")
    app.run(host=bind_host, port=5000, debug=is_debug, use_reloader=False)
