import logging
from waitress import serve
from app import create_app
from app.config import Config

# 開啟 Waitress 的標準存取日誌輸出
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('waitress')
logger.setLevel(logging.INFO)

app = create_app()

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 [Windows 生產模式] Waitress 伺服器啟動中...")
    print("服務位址: http://127.0.0.1:5000")
    print(f"執行緒數 (Threads): {Config.WAITRESS_THREADS} (每條 SSE 長連線占用 1 條，需大於 SSE 連線上限)")
    print("="*50 + "\n")

    serve(app, host='0.0.0.0', port=5000, threads=Config.WAITRESS_THREADS, channel_timeout=60)