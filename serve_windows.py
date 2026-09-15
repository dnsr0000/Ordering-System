import logging
from waitress import serve
from app import create_app

# 開啟 Waitress 的標準存取日誌輸出
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('waitress')
logger.setLevel(logging.INFO)

app = create_app()

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 [Windows 生產模式] Waitress 伺服器啟動中...")
    print("服務位址: http://127.0.0.1:5000")
    print("執行緒數 (Threads): 32 (支援 SSE 與高併發)")
    print("="*50 + "\n")

    serve(app, host='0.0.0.0', port=5000, threads=32, channel_timeout=60)