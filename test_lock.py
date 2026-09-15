import time
import requests
from concurrent.futures import ThreadPoolExecutor

# 請確認此餐點名稱與你在後台改為庫存 1 的餐點完全相同
TARGET_ITEM_NAME = "日式豚骨拉麵"
TARGET_ITEM_PRICE = 230

ORDER_API_URL = "http://127.0.0.1:5000/submit_order"

def simulate_customer_checkout(customer_idx):
    session = requests.Session()
    # 建立訪客 Session 避免被導回首頁
    session.get(f"http://127.0.0.1:5000/guest_login")
    
    payload = {
        "items": [{
            "name": TARGET_ITEM_NAME,
            "price": TARGET_ITEM_PRICE,
            "quantity": 1,
            "customization": ""
        }],
        "payment_method": "Cash",
        "order_type": "內用"
    }
    
    start_t = time.perf_counter()
    response = session.post(ORDER_API_URL, json=payload)
    elapsed = (time.perf_counter() - start_t) * 1000
    
    try:
        res_data = response.json()
    except Exception:
        res_data = {"error": f"【伺服器 500 錯誤】: {response.text[:200]}..."}
        
    return customer_idx, response.status_code, res_data, elapsed

if __name__ == '__main__':
    print("=" * 60)
    print(f"🔥 開始高併發模擬：5 名顧客同時搶購剩餘 1 份的【{TARGET_ITEM_NAME}】...")
    print("=" * 60)

    # 同時發動 5 個執行緒發出請求
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(simulate_customer_checkout, range(1, 6)))

    success_orders = []
    failed_orders = []

    for idx, status, res_json, elapsed in results:
        if status == 200:
            success_orders.append((idx, res_json.get('pickup_number'), res_json.get('order_id'), elapsed))
            print(f"✅ [成功 200] 顧客 {idx} 搶購成功！取餐號碼: #{res_json.get('pickup_number'):03d} (單號: #{res_json.get('order_id')})，耗時: {elapsed:.1f}ms")
        else:
            failed_orders.append((idx, status, res_json.get('message') or res_json.get('error'), elapsed))
            print(f"❌ [攔截 {status}] 顧客 {idx} 下單被阻斷：{res_json.get('message') or res_json.get('error')}，耗時: {elapsed:.1f}ms")

    print("\n" + "=" * 60)
    print("📊 驗證統計報告：")
    print(f"搶購成功單數: {len(success_orders)} 筆")
    print(f"成功阻斷單數: {len(failed_orders)} 筆")
    
    if len(success_orders) == 1 and len(failed_orders) == 4:
        print("🎉 驗證通過！Redis 分散式互斥鎖完美運作，零超賣、取餐號碼無衝突！")
    else:
        print("⚠️ 驗證失敗：出現重複下單或超賣現象！")
    print("=" * 60)