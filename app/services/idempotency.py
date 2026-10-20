"""
送單冪等保護：同一個顧客連按兩次「送出」或網路重送時，內容完全相同的請求在短時間內只會建立一張訂單，
第二次起直接回傳第一次的結果 (含同一個訂單編號)，不會重複扣庫存、重複扣點、重複出單。

- 鍵 = (店家, 顧客工作階段, 請求內容雜湊)：內容不同 (改了購物車) 就是新的訂單。
- 只快取「成功 (HTTP 200)」的結果；失敗的請求不會被快取，可以立刻重試。
- 同一把鍵同時進來的請求會排隊：第一個處理完後，其餘直接取得同一份結果。
"""
import hashlib
import json
import threading
import time


class IdempotencyCache:
    def __init__(self, ttl_seconds=10, max_entries=2000):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._guard = threading.Lock()
        self._entries = {}

    @staticmethod
    def make_key(*parts):
        raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _purge(self, now):
        if len(self._entries) < self._max_entries:
            return
        # 清除「已超過 TTL 且目前沒有請求持有/等待處理」的項目，成功 (有 value) 與失敗 (value 為 None) 都一視同仁；
        # 否則失敗請求的 key 永遠不會被清掉，累積到上限後每次請求都會做一次全表掃描卻刪不掉任何東西。
        # ts 在建立與完成時都會更新，因此「處理中」的項目不會被誤刪；lock.locked() 再多擋一層保險。
        expired = [
            k for k, e in self._entries.items()
            if now - e['ts'] > self._ttl and not e['lock'].locked()
        ]
        for k in expired:
            self._entries.pop(k, None)

    def run(self, key, fn):
        """執行 fn() -> (result, status_code)；回傳 (result, status_code, replayed)"""
        now = time.time()
        with self._guard:
            self._purge(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = {'lock': threading.Lock(), 'value': None, 'ts': now}
        with entry['lock']:
            now = time.time()
            if entry['value'] is not None and now - entry['ts'] <= self._ttl:
                result, status = entry['value']
                return result, status, True
            result, status = fn()
            if status == 200:
                entry['value'] = (result, status)
            else:
                entry['value'] = None
            # 不論成功或失敗都記錄完成時間，讓 _purge 能依 TTL 回收。
            # (不在失敗時直接 pop：排在同一把鎖後面的重送請求仍需共用這個 entry 才能被序列化，避免同時建立兩張單)
            entry['ts'] = time.time()
            return result, status, False


submit_order_cache = IdempotencyCache(ttl_seconds=10)