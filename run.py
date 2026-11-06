"""
啟動入口 (門市 / 單機 / 開發共用)。
- python run.py              正式營業：Waitress，預設 0.0.0.0:5000 (區網內的點餐機 / POS / 看板可連線)
- .env 設 FLASK_DEBUG=1 時   開發除錯：Flask 內建伺服器 + 除錯頁，只監聽本機
- 線上端 (Docker) 由 gunicorn 載入 run:app，不會執行下面的 main()
- Flask CLI 也以這個檔案為入口：flask --app run setup、flask --app run db upgrade …
"""
import logging
import os

from app import create_app

app = create_app()   # 記錄檔與主控台輸出由 app/services/log_service.py 設定 (LOG_FILE)

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')


def _banner(port, extra):
    # 主控台只顯示警告以上的記錄 (完整記錄在 database/logs/app.log)：啟動成功的提示直接印出，才看得出伺服器已在執行
    print(f'前台首頁: http://127.0.0.1:{port}/    店家後台: http://127.0.0.1:{port}/admin    ({extra}；按 Ctrl+C 停止)', flush=True)


def main():
    port = int(os.getenv('PORT', '5000'))
    if os.getenv('FLASK_DEBUG', '0') == '1':
        host = os.getenv('HOST') or '127.0.0.1'
        if host not in LOCAL_HOSTS:
            # 除錯模式的錯誤頁可以執行程式碼：只允許本機連線，不開放給區網
            print(f'FLASK_DEBUG=1 時只監聽本機 (忽略 HOST={host})')
            host = '127.0.0.1'
        _banner(port, 'Flask 開發伺服器，除錯模式')
        app.run(host=host, port=port, debug=True, use_reloader=False)
        return

    from waitress import serve
    threads = app.config['WAITRESS_THREADS']
    host = os.getenv('HOST', '0.0.0.0')
    logging.getLogger(__name__).info('Waitress 啟動於 %s:%d (執行緒 %d；每條 SSE 長連線占用 1 條)', host, port, threads)
    _banner(port, f'Waitress，監聽 {host}:{port}')
    # connection_limit 預設只有 100：看板的 SSE 長連線很多時，一般請求會排隊，因此跟著執行緒數放寬
    serve(app, host=host, port=port, threads=threads, channel_timeout=60, connection_limit=threads + 100)


if __name__ == '__main__':
    main()
