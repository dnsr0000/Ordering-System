"""開發用啟動入口 (Flask 內建伺服器)。正式營業請用 serve_windows.py (Waitress)。

也可以使用 Flask CLI：flask --app run run --debug、flask --app run db upgrade
"""
import os

from app import create_app

app = create_app()

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    host = os.getenv('HOST') or os.getenv('FLASK_HOST') or ('127.0.0.1' if debug else '0.0.0.0')
    if debug and host not in ('127.0.0.1', 'localhost', '::1'):
        # 除錯模式的錯誤頁可以執行程式碼：只允許本機連線，不開放給區網
        print(f'FLASK_DEBUG=1 時只監聽本機 (忽略 HOST={host})')
        host = '127.0.0.1'
    port = int(os.getenv('PORT', '5000'))
    print(f'前台首頁: http://127.0.0.1:{port}/    店家後台: http://127.0.0.1:{port}/admin')
    app.run(host=host, port=port, debug=debug, use_reloader=False)
