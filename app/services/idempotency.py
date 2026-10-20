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
        for k in [k for k, e in self._entries.items() if e['value'] is not None and now - e['ts'] > self._ttl]:
            self._entries.pop(k, None)

    def run(self, key, fn):
        """執行 fn() -> (result, status_code)；回傳 (result, status_code, replayed)"""
        now = time.time()
        with self._guard:
            self._purge(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = {'lock': threading.Lock(), 'value': None, 'ts': 0.0}
        with entry['lock']:
            now = time.time()
            if entry['value'] is not None and now - entry['ts'] <= self._ttl:
                result, status = entry['value']
                return result, status, True
            result, status = fn()
            if status == 200:
                entry['value'] = (result, status)
                entry['ts'] = time.time()
            else:
                entry['value'] = None
            return result, status, False


submit_order_cache = IdempotencyCache(ttl_seconds=10)
