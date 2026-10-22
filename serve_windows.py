"""正式營業啟動入口 (Waitress，Windows / Linux 皆可)。"""
import logging
import os

from waitress import serve

from app import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')

app = create_app()

if __name__ == '__main__':
    threads = app.config['WAITRESS_THREADS']
    port = int(os.getenv('PORT', '5000'))
    logging.getLogger(__name__).info('Waitress 啟動於 http://127.0.0.1:%d (執行緒 %d；每條 SSE 長連線占用 1 條)', port, threads)
    # connection_limit 預設只有 100：看板的 SSE 長連線很多時，一般請求會排隊，因此跟著執行緒數放寬
    serve(app, host=os.getenv('HOST', '0.0.0.0'), port=port, threads=threads, channel_timeout=60,
          connection_limit=threads + 100)
