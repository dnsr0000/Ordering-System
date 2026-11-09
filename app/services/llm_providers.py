"""
多家 AI 服務：金鑰自動辨識廠商、自動挑模型、依序備援 (呼叫流程在 app/services/ai_service.cloud_generate)。

金鑰來源 (依序，重複的略過)：系統設定「AI 服務」的金鑰 1~3 (每店各自設定) → .env 的 AI_API_KEYS (逗號分隔) → GEMINI_API_KEY。
辨識廠商：先看金鑰開頭 (sk-ant- = Anthropic、gsk_ = Groq…)；開頭不唯一 (例如 sk- 可能是 OpenAI 或 DeepSeek) 時，
依序呼叫各家的「模型清單」API，接受這組金鑰的就是。
挑模型：各家的模型清單 + 挑選規則 (寫「系列」不寫死版本，新版推出自動換)；設定裡有指定模型時以指定為準。
每組金鑰挑兩檔：models (主力，翻譯菜單等店家願意等的工作) 與 fast (快速便宜，客服 / 語音點餐 / 短文案)。
結果快取 24 小時 (失敗快取 10 分鐘；已知廠商暫時連不上時沿用上次成功的結果、只快取 30 秒)，快取鍵含指定的模型 / 廠商
(改了設定的指定模型立刻生效)；呼叫回「模型不存在」時重新查一次。系統監控頁只讀快取，需要辨識時改在背景執行緒做。
Anthropic (Claude) 走官方 anthropic SDK；其餘多為 OpenAI 相容格式 (/chat/completions)，以 requests 呼叫。
多行程部署 (設定 REDIS_URL) 時，辨識結果、冷卻與模型健康紀錄經 Redis 共用 (shared_*)；沒有 Redis 時只存在本行程記憶體。
"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field, replace

import requests

logger = logging.getLogger(__name__)

PROVIDERS = {   # 名稱: (顯示名稱, 呼叫方式, API 基底網址)
    'gemini': ('Google Gemini', 'gemini', 'https://generativelanguage.googleapis.com/v1beta'),
    'anthropic': ('Anthropic Claude', 'anthropic', ''),
    'openai': ('OpenAI', 'openai', 'https://api.openai.com/v1'),
    'deepseek': ('DeepSeek', 'openai', 'https://api.deepseek.com/v1'),
    'groq': ('Groq', 'openai', 'https://api.groq.com/openai/v1'),
    'xai': ('xAI Grok', 'openai', 'https://api.x.ai/v1'),
    'openrouter': ('OpenRouter', 'openai', 'https://openrouter.ai/api/v1'),
    'mistral': ('Mistral', 'openai', 'https://api.mistral.ai/v1'),
}
# 金鑰開頭 → 可能的廠商 (依序比對，第一個符合的規則為準)
PREFIXES = (
    ('sk-ant-', ('anthropic',)), ('sk-or-', ('openrouter',)), ('gsk_', ('groq',)), ('xai-', ('xai',)),
    ('AIza', ('gemini',)), ('AQ.', ('gemini',)), ('sk-proj-', ('openai',)), ('sk-svcacct-', ('openai',)),
    ('sk-', ('openai', 'deepseek')),
)
OTHER_FORMATS = ('gemini', 'mistral')   # 開頭都不符合時依序試
# 不是一般文字對話的模型 (嵌入、語音、圖片、即時、審核…)
_NOT_CHAT = re.compile(r'embed|whisper|tts|audio|speech|transcri|image|vision|dall|moderation|guard|realtime|search|rerank|'
                       r'ocr|banana|lyria|veo|imagen|research|customtools|computer|codex|omni|live', re.I)
DETECT_TTL = 24 * 3600
ERROR_TTL = 600
TRANSIENT_TTL = 30   # 已知廠商查模型清單暫時失敗 (過載 / 限流 / 連線)：只短暫快取，很快再查
LIST_TIMEOUT = 10
NOT_DETECTED = '尚未辨識 (背景辨識中，請稍後重新整理)'
INVALID_KEY_CHARS = '金鑰含無效字元 (例如零寬空白、全形字)，請重新貼上金鑰'


class ProviderError(Exception):
    """呼叫失敗。reason：auth (金鑰錯誤 / 無權限)、rate (限流)、overload (過載 / 5xx / 逾時 / 連線)、notfound (模型不存在)、
    refusal (模型拒答)、other"""

    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}')
        self.reason, self.detail = reason, str(detail)[:200]


@dataclass(frozen=True)
class Credential:
    key: str
    source: str          # 'settings:1' / 'env:AI_API_KEYS' / 'env:GEMINI_API_KEY'
    model: str = ''      # 指定模型 (空 = 自動挑選；逗號分隔 = 主要, 備援)
    provider: str = ''   # 已知廠商 (.env 的 GEMINI_API_KEY 一定是 Gemini)；空 = 自動辨識

    @property
    def id(self):
        return hashlib.sha256(self.key.encode()).hexdigest()[:12]

    @property
    def detect_id(self):
        """辨識結果的快取鍵：指定的模型 / 廠商不同就是不同的結果 (改了設定的指定模型立刻生效，
        同一把金鑰在不同店家指定不同模型也不會互相套用)；自動挑選時與 id 相同 (沿用既有的快取)"""
        if not self.model and not self.provider:
            return self.id
        return hashlib.sha256(f'{self.key}|{self.model}|{self.provider}'.encode()).hexdigest()[:12]

    @property
    def masked(self):
        return f'{self.key[:6]}…{self.key[-4:]}' if len(self.key) > 12 else '***'


@dataclass
class Resolved:
    provider: str = ''
    models: list = field(default_factory=list)   # 主力 [主要, 備援...]
    error: str = ''
    at: float = 0.0
    auto: bool = True
    fast: list = field(default_factory=list)     # 快速便宜 [主要, 備援...]；空 = 同主力
    ttl: float = 0.0                             # 快取幾秒 (0 = 依成敗用 DETECT_TTL / ERROR_TTL)

    def for_tier(self, tier):
        return list(self.fast or self.models) if tier == 'fast' else list(self.models)

    @property
    def label(self):
        return PROVIDERS[self.provider][0] if self.provider in PROVIDERS else '無法辨識'

    @property
    def ok(self):
        return bool(self.provider and self.models)


class Reply(str):
    """模型的回答文字，另外帶 usage = {'input', 'output', 'cache_read', 'cache_write'} (token 數，統計費用用)"""
    usage: dict = {}

    @classmethod
    def of(cls, text, usage=None):
        reply = cls(text)
        reply.usage = {k: int(v or 0) for k, v in (usage or {}).items()}
        return reply


_cache: dict[str, Resolved] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 多行程共用狀態 (REDIS_URL)：沒有 Redis 或連不上時回 None / 不做事，呼叫端只用本行程記憶體
# ---------------------------------------------------------------------------
_redis = None
_redis_url = None


def _shared():
    global _redis, _redis_url
    try:
        from flask import current_app
        url = current_app.config.get('REDIS_URL') or ''
    except RuntimeError:   # 沒有 app context
        return _redis
    if url != _redis_url:
        _redis_url, _redis = url, None
        if url:
            try:
                import redis
                _redis = redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
                _redis.ping()
            except Exception as exc:  # noqa: BLE001
                logger.warning('AI 共用狀態無法使用 Redis (%s)，只存在本行程', exc)
                _redis = None
    return _redis


def shared_get(key):
    r = _shared()
    if r is None:
        return None
    try:
        raw = r.get(f'ai:{key}')
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def shared_set(key, value, ttl):
    r = _shared()
    if r is not None:
        try:
            r.set(f'ai:{key}', json.dumps(value), ex=max(1, int(ttl)))
        except Exception:  # noqa: BLE001
            pass


def shared_delete(key):
    r = _shared()
    if r is not None:
        try:
            r.delete(f'ai:{key}')
        except Exception:  # noqa: BLE001
            pass


def shared_push(key, value, maxlen, ttl):
    """清單左端加入一筆 (保留最新 maxlen 筆)"""
    r = _shared()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.lpush(f'ai:{key}', json.dumps(value))
            pipe.ltrim(f'ai:{key}', 0, maxlen - 1)
            pipe.expire(f'ai:{key}', int(ttl))
            pipe.execute()
        except Exception:  # noqa: BLE001
            pass


def shared_list(key):
    """回清單 (最新的在前)；沒有 Redis 回 None"""
    r = _shared()
    if r is None:
        return None
    try:
        return [json.loads(x) for x in r.lrange(f'ai:{key}', 0, -1)]
    except Exception:  # noqa: BLE001
        return None


def candidates(key):
    for prefix, names in PREFIXES:
        if key.startswith(prefix):
            return names
    return OTHER_FORMATS


# ---------------------------------------------------------------------------
# 模型清單
# ---------------------------------------------------------------------------
def _http_error(status, body):
    detail = ''
    if isinstance(body, dict):
        err = body.get('error')
        detail = (err.get('message') if isinstance(err, dict) else err) or body.get('message') or ''
    reason = 'auth' if status in (401, 403) else 'notfound' if status == 404 else 'rate' if status == 429 \
        else 'overload' if status >= 500 else 'other'
    return ProviderError(reason, f'HTTP {status} {detail}'.strip())


def _get_json(url, headers, params=None):
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=LIST_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError('overload', f'連線異常 {exc.__class__.__name__}')
    except UnicodeError:   # 金鑰含零寬空白 / 全形字等，HTTP 標頭無法編碼
        raise ProviderError('auth', INVALID_KEY_CHARS)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        raise _http_error(resp.status_code, body)
    return body


def list_models(provider, key):
    """回 [{'id', 'created'}]；金鑰不被接受時拋 ProviderError"""
    kind, base = PROVIDERS[provider][1], PROVIDERS[provider][2]
    if kind == 'gemini':
        body = _get_json(f'{base}/models', {'x-goog-api-key': key}, {'pageSize': 1000})
        return [{'id': m.get('name', '').split('/')[-1], 'created': 0} for m in body.get('models') or []
                if 'generateContent' in (m.get('supportedGenerationMethods') or [])]
    if kind == 'anthropic':
        anthropic = _anthropic()
        try:
            client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=LIST_TIMEOUT)
            return [{'id': m.id, 'created': m.created_at.timestamp() if m.created_at else 0}
                    for m in client.models.list(lifecycle=['active'], limit=100)]
        except anthropic.APIError as exc:
            raise _anthropic_error(anthropic, exc)
        except UnicodeError:
            raise ProviderError('auth', INVALID_KEY_CHARS)
    body = _get_json(f'{base}/models', {'Authorization': f'Bearer {key}'})
    return [{'id': str(m.get('id') or ''), 'created': m.get('created') or 0} for m in body.get('data') or [] if m.get('id')]


def _version(text):
    return tuple(int(x) for x in re.findall(r'\d+', text or ''))


def _newest(ids_created, pattern=None):
    """符合 pattern 的模型中版本號最大 (同版本取較新建立的)"""
    rows = [(m['id'], m['created']) for m in ids_created if not pattern or re.fullmatch(pattern, m['id'])]
    rows.sort(key=lambda r: (_version(r[0]), r[1]), reverse=True)
    return rows[0][0] if rows else ''


def _newest_created(ids_created, prefix):
    rows = sorted((m for m in ids_created if m['id'].startswith(prefix)), key=lambda m: (m['created'], m['id']), reverse=True)
    return rows[0]['id'] if rows else ''


def pick_models(provider, models):
    """依各家規則挑 [主要, 備援]。規則寫「系列」不寫死版本"""
    ids = {m['id'] for m in models}
    chat = [m for m in models if not _NOT_CHAT.search(m['id']) and 'preview' not in m['id']]
    picks = []
    if provider == 'gemini':
        picks = [_newest(chat, r'gemini-\d+(?:\.\d+)*-flash') or ('gemini-flash-latest' if 'gemini-flash-latest' in ids else ''),
                 _newest(chat, r'gemini-\d+(?:\.\d+)*-flash-lite')
                 or ('gemini-flash-lite-latest' if 'gemini-flash-lite-latest' in ids else '')]
    elif provider == 'anthropic':
        # Claude 預設用最新的 Opus (最強)，備援最新的 Sonnet；要省費用可在設定指定 Sonnet / Haiku
        picks = [_newest_created(chat, 'claude-opus-'), _newest_created(chat, 'claude-sonnet-')]
    elif provider == 'openai':
        picks = [_newest(chat, r'gpt-\d+(?:\.\d+)*-mini'), _newest(chat, r'gpt-\d+(?:\.\d+)*')]
    elif provider == 'deepseek':
        picks = [m for m in ('deepseek-chat',) if m in ids]
    elif provider == 'openrouter':
        picks = [m for m in ('openrouter/auto',) if m in ids]
    elif provider == 'mistral':
        picks = [m for m in ('mistral-large-latest', 'mistral-small-latest') if m in ids]
    picks = [p for p in dict.fromkeys(picks) if p]
    if not picks and chat:   # 沒有對應規則：最新建立的對話模型
        picks = [max(chat, key=lambda m: (m['created'], _version(m['id'])))['id']]
    return picks


def pick_fast(provider, models, strong):
    """快速便宜的一檔 (客服、語音點餐、短文案)：Gemini 最新 Flash-Lite、Claude 最新 Haiku、OpenAI 最新 gpt-*-nano / mini；
    找不到時沿用主力的順序反過來 (備援通常是較小的模型)"""
    chat = [m for m in models if not _NOT_CHAT.search(m['id']) and 'preview' not in m['id']]
    picks = []
    if provider == 'gemini':
        picks = [_newest(chat, r'gemini-\d+(?:\.\d+)*-flash-lite'), _newest(chat, r'gemini-\d+(?:\.\d+)*-flash')]
    elif provider == 'anthropic':
        picks = [_newest_created(chat, 'claude-haiku-'), _newest_created(chat, 'claude-sonnet-')]
    elif provider == 'openai':
        picks = [_newest(chat, r'gpt-\d+(?:\.\d+)*-nano'), _newest(chat, r'gpt-\d+(?:\.\d+)*-mini')]
    elif provider == 'mistral':
        picks = [m for m in ('mistral-small-latest', 'mistral-large-latest') if m in {x['id'] for x in models}]
    picks = [p for p in dict.fromkeys(picks) if p]
    if not picks:
        picks = list(reversed(strong)) if provider in ('gemini', 'anthropic') else list(strong)
    return picks


def _to_shared(result):
    return asdict(result)


def _from_shared(data):
    try:
        return Resolved(**{k: data[k] for k in ('provider', 'models', 'error', 'at', 'auto', 'fast', 'ttl') if k in data})
    except (TypeError, KeyError):
        return None


def resolve(cred, force=False, cached_only=False):
    """辨識金鑰的廠商與要用的模型 (快取)；回 Resolved (error 不為空代表無法使用)。
    cached_only=True (系統監控頁 / 後台首頁)：需要查模型清單時不在請求裡等網路，回上次的結果或「尚未辨識」"""
    now = time.time()
    cid = cred.detect_id

    def fresh(r):
        return r is not None and now - r.at < (r.ttl or (DETECT_TTL if r.ok else ERROR_TTL))
    with _lock:
        hit = _cache.get(cid)
    if not force and fresh(hit):
        return hit
    other = None
    if not force:   # 其他行程已經辨識過 (Redis)
        other = shared_get(f'detect:{cid}')
        other = _from_shared(other) if isinstance(other, dict) else None
        if fresh(other):
            with _lock:
                _cache[cid] = other
            return other
    result, errors = Resolved(at=now), []
    names = (cred.provider,) if cred.provider in PROVIDERS else candidates(cred.key)
    if cred.provider in PROVIDERS and cred.model:   # 廠商與模型都已指定：不用查模型清單
        result.provider, result.auto = cred.provider, False
        result.models = [m.strip() for m in cred.model.split(',') if m.strip()]
        with _lock:
            _cache[cid] = result
        return result
    if cached_only:
        resolve_async(cred, _current_app())
        return hit or other or Resolved(error=NOT_DETECTED, at=now)
    transient = False
    for provider in names:
        try:
            models = list_models(provider, cred.key)
        except ProviderError as exc:
            errors.append(f'{PROVIDERS[provider][0]}：{exc.detail or exc.reason}')
            if exc.reason in ('overload', 'rate') and len(names) == 1:
                transient = True
                break   # 確定是這一家但暫時連不上：不要誤判成金鑰無效
            continue
        result.provider = provider
        result.models = [m.strip() for m in cred.model.split(",") if m.strip()] if cred.model else pick_models(provider, models)
        result.auto = not cred.model
        # 指定模型時兩檔都用指定的 (店家明確選了就照辦)
        result.fast = [] if cred.model else pick_fast(provider, models, result.models)
        if not result.models:
            result.error = '找不到可用的文字模型 (請在設定指定模型)'
        break
    if not result.provider:
        result.error = '；'.join(errors) or '無法辨識金鑰'
        if transient:
            # 暫時連不上：沿用上一次成功的辨識結果 (過期的也先用)，沒有的話只短暫快取，不標成無法使用 10 分鐘
            last_ok = next((r for r in (hit, other) if r is not None and r.ok), None)
            result = replace(last_ok, at=now) if last_ok is not None else result
            result.ttl = TRANSIENT_TTL
    with _lock:
        _cache[cid] = result
    shared_set(f'detect:{cid}', _to_shared(result), result.ttl or (DETECT_TTL if result.ok else ERROR_TTL))
    if result.ok:
        logger.info('AI 金鑰 %s (%s)：%s，模型 %s (快速 %s)', cred.masked, cred.source, result.label, ' → '.join(result.models),
                    ' → '.join(result.for_tier('fast')))
    else:
        logger.warning('AI 金鑰 %s (%s) 無法使用：%s', cred.masked, cred.source, result.error)
    return result


def forget(cred):
    with _lock:
        _cache.pop(cred.detect_id, None)
    shared_delete(f'detect:{cred.detect_id}')


_detecting: set = set()   # 背景辨識中的 detect_id


def _current_app():
    try:
        from flask import current_app
        return current_app._get_current_object()
    except RuntimeError:   # 沒有 app context
        return None


def resolve_async(cred, app=None):
    """在背景執行緒辨識 (網頁請求不必等各家模型清單)；同一組金鑰同時只跑一個"""
    cid = cred.detect_id
    with _lock:
        if cid in _detecting:
            return None
        _detecting.add(cid)

    def run():
        try:
            if app is not None:
                with app.app_context():
                    resolve(cred)
            else:
                resolve(cred)
        except Exception:  # noqa: BLE001
            logger.exception('AI 金鑰 %s 背景辨識失敗', cred.masked)
        finally:
            with _lock:
                _detecting.discard(cid)
    t = threading.Thread(target=run, name='ai-detect', daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# 呼叫 (Gemini 在 ai_service.gemini_generate；這裡是 Anthropic 與 OpenAI 相容格式)
# ---------------------------------------------------------------------------
def _anthropic():
    try:
        import anthropic
    except ImportError:
        raise ProviderError('other', '未安裝 anthropic 套件 (pip install -r requirements.txt)')
    return anthropic


def _anthropic_error(anthropic, exc):
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ProviderError('auth', str(exc))
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError('notfound', str(exc))
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError('rate', str(exc))
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return ProviderError('overload', f'連線異常 {exc.__class__.__name__}')
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:   # 含 529 overloaded_error
        return ProviderError('overload', str(exc))
    return ProviderError('other', str(exc))


# 支援伺服器端拒答備援 (fallbacks: "default") 的 Claude 模型
_CLAUDE_FALLBACK_MODELS = re.compile(r'claude-(opus-5|fable-5-1|sonnet-5-5)')
_CLAUDE_EFFORT_MODELS = re.compile(r'claude-(opus|sonnet|haiku|fable)-[5-9]')


def call_anthropic(key, model, prompt, max_tokens, interactive, prefix='', timeout=None):
    """prefix：每次都一樣的前段 (客服的店家資訊與菜單…)，放在 system 並標記提示快取，重複的部分只收約一成費用。
    timeout：這次最多等幾秒 (翻譯菜單的時間上限快到時縮短；省略 = 即時 12 秒、其他 120 秒)"""
    anthropic = _anthropic()
    limit = 12.0 if interactive else 120.0
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=min(limit, timeout) if timeout else limit)
    # 思考 (thinking) 的用量也算在 max_tokens 裡：太小會在思考完之前就被截斷而沒有回答
    kwargs = {'model': model, 'max_tokens': max(max_tokens, 4000 if interactive else 16000),
              'messages': [{'role': 'user', 'content': prompt}]}
    if prefix:
        kwargs['system'] = [{'type': 'text', 'text': prefix, 'cache_control': {'type': 'ephemeral'}}]
    if interactive and _CLAUDE_EFFORT_MODELS.search(model):
        kwargs['output_config'] = {'effort': 'low'}   # 即時對話：顧客在等，少想一點
    try:
        if _CLAUDE_FALLBACK_MODELS.search(model):
            # 模型拒答時由 Anthropic 在同一個請求內改用建議的備援模型
            resp = client.beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.APIError as exc:
        raise _anthropic_error(anthropic, exc)
    except UnicodeError:
        raise ProviderError('auth', INVALID_KEY_CHARS)
    if resp.stop_reason == 'refusal':
        raise ProviderError('refusal', getattr(resp.stop_details, 'category', '') or '')
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if not text:
        raise ProviderError('other', f'回應沒有內容 ({resp.stop_reason})')
    u = resp.usage
    return Reply.of(text, {'input': getattr(u, 'input_tokens', 0), 'output': getattr(u, 'output_tokens', 0),
                           'cache_read': getattr(u, 'cache_read_input_tokens', 0) or 0,
                           'cache_write': getattr(u, 'cache_creation_input_tokens', 0) or 0})


def call_openai_compatible(provider, key, model, prompt, max_tokens, temperature, interactive, prefix='', timeout=None):
    """prefix 放在 system 訊息 (固定的前段在最前面，OpenAI / DeepSeek 等會自動快取重複的開頭)。timeout 見 call_anthropic"""
    base = PROVIDERS[provider][2]
    body = {'model': model, 'messages': ([{'role': 'system', 'content': prefix}] if prefix else [])
            + [{'role': 'user', 'content': prompt}]}
    if provider == 'openai':
        # OpenAI 新模型改用 max_completion_tokens，推理模型不接受自訂 temperature
        body['max_completion_tokens'] = max(max_tokens, 4000)
    else:
        body.update(max_tokens=max_tokens, temperature=temperature)
    limit = (3, 12) if interactive else (5, 60)
    if timeout:
        limit = (min(limit[0], timeout), min(limit[1], timeout))
    try:
        resp = requests.post(f'{base}/chat/completions', json=body, timeout=limit,
                             headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    except requests.RequestException as exc:
        raise ProviderError('overload', f'連線異常 {exc.__class__.__name__}')
    except UnicodeError:   # 金鑰含零寬空白 / 全形字等，HTTP 標頭無法編碼
        raise ProviderError('auth', INVALID_KEY_CHARS)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        raise _http_error(resp.status_code, data)
    choices = data.get('choices') or [{}]
    text = ((choices[0].get('message') or {}).get('content') or '').strip()
    if not text:
        raise ProviderError('other', '回應沒有內容')
    u = data.get('usage') or {}
    cached = int((u.get('prompt_tokens_details') or {}).get('cached_tokens') or u.get('prompt_cache_hit_tokens') or 0)
    return Reply.of(text, {'input': max(0, int(u.get('prompt_tokens') or 0) - cached), 'output': u.get('completion_tokens'),
                           'cache_read': cached})


# ---------------------------------------------------------------------------
# 系統設定「AI 服務」的「測試連線」：每組金鑰重新辨識，並實際送一句很短的請求
# ---------------------------------------------------------------------------
def test_settings(cfg):
    from app.errors import AppError
    from app.services import ai_service
    lines, ok = [], 0
    for n in (1, 2, 3):
        key = ai_service._usable(cfg.get(f'key{n}'))
        if not key:
            continue
        cred = Credential(key=key, source=f'settings:{n}', model=str(cfg.get(f'model{n}') or '').strip())
        r = resolve(cred, force=True)
        if not r.ok:
            lines.append(f'金鑰 {n}：{r.error or "無法辨識"}')
            continue
        how = '自動挑選' if r.auto else '指定'
        models = f'模型 {" → ".join(r.models)}'
        if r.for_tier('fast') != r.models:
            models += f'，快速 {" → ".join(r.for_tier("fast"))}'
        # 店家剛修好金鑰 (儲值、開通 API) 就按測試：清掉冷卻，實際送出請求；失敗原因只看這次呼叫 (本店) 留下的錯誤
        ai_service.clear_cooldown(cred.id)
        ai_service.clear_ai_error()
        text, _source = ai_service.cloud_generate('只回覆兩個字：正常', max_tokens=50, patient=False, creds=[cred], tier='fast')
        if text:
            ok += 1
            lines.append(f'金鑰 {n}：{r.label}，{models} ({how})，回應正常')
        else:
            lines.append(f'金鑰 {n}：{r.label}，{models} ({how})，但呼叫失敗：{ai_service.last_ai_error() or "原因不明"}')
    if not lines:
        raise AppError('尚未填寫任何金鑰')
    if not ok:
        raise AppError('；'.join(lines))
    return '；'.join(lines)


def _register_tester():
    from app.services import settings_service
    settings_service.register_tester('ai')(test_settings)


_register_tester()
