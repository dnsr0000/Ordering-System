"""
多家 AI 服務：金鑰自動辨識廠商、自動挑模型、依序備援 (呼叫流程在 app/services/ai_service.cloud_generate)。

金鑰來源 (依序，重複的略過)：系統設定「AI 服務」的金鑰 1~3 (每店各自設定) → .env 的 AI_API_KEYS (逗號分隔) → GEMINI_API_KEY。
辨識廠商：先看金鑰開頭 (sk-ant- = Anthropic、gsk_ = Groq…)；開頭不唯一 (例如 sk- 可能是 OpenAI 或 DeepSeek) 時，
依序呼叫各家的「模型清單」API，接受這組金鑰的就是。
挑模型：各家的模型清單 + 挑選規則 (寫「系列」不寫死版本，新版推出自動換)；設定裡有指定模型時以指定為準。
結果快取 24 小時 (失敗快取 10 分鐘)；呼叫回「模型不存在」時重新查一次。
Anthropic (Claude) 走官方 anthropic SDK；其餘多為 OpenAI 相容格式 (/chat/completions)，以 requests 呼叫。
"""
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field

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
LIST_TIMEOUT = 10


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
    def masked(self):
        return f'{self.key[:6]}…{self.key[-4:]}' if len(self.key) > 12 else '***'


@dataclass
class Resolved:
    provider: str = ''
    models: list = field(default_factory=list)   # [主要, 備援...]
    error: str = ''
    at: float = 0.0
    auto: bool = True

    @property
    def label(self):
        return PROVIDERS[self.provider][0] if self.provider in PROVIDERS else '無法辨識'

    @property
    def ok(self):
        return bool(self.provider and self.models)


_cache: dict[str, Resolved] = {}
_lock = threading.Lock()


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


def resolve(cred, force=False):
    """辨識金鑰的廠商與要用的模型 (快取)；回 Resolved (error 不為空代表無法使用)"""
    now = time.time()
    with _lock:
        hit = _cache.get(cred.id)
    if hit is not None and not force and now - hit.at < (DETECT_TTL if hit.ok else ERROR_TTL):
        return hit
    result, errors = Resolved(at=now), []
    names = (cred.provider,) if cred.provider in PROVIDERS else candidates(cred.key)
    if cred.provider in PROVIDERS and cred.model:   # 廠商與模型都已指定：不用查模型清單
        result.provider, result.auto = cred.provider, False
        result.models = [m.strip() for m in cred.model.split(',') if m.strip()]
        with _lock:
            _cache[cred.id] = result
        return result
    for provider in names:
        try:
            models = list_models(provider, cred.key)
        except ProviderError as exc:
            errors.append(f'{PROVIDERS[provider][0]}：{exc.detail or exc.reason}')
            if exc.reason in ('overload', 'rate') and len(names) == 1:
                break   # 確定是這一家但暫時連不上：不要誤判成金鑰無效
            continue
        result.provider = provider
        result.models = [m.strip() for m in cred.model.split(",") if m.strip()] if cred.model else pick_models(provider, models)
        result.auto = not cred.model
        if not result.models:
            result.error = '找不到可用的文字模型 (請在設定指定模型)'
        break
    if not result.provider:
        result.error = '；'.join(errors) or '無法辨識金鑰'
    with _lock:
        _cache[cred.id] = result
    if result.ok:
        logger.info('AI 金鑰 %s (%s)：%s，模型 %s', cred.masked, cred.source, result.label, ' → '.join(result.models))
    else:
        logger.warning('AI 金鑰 %s (%s) 無法使用：%s', cred.masked, cred.source, result.error)
    return result


def forget(cred):
    with _lock:
        _cache.pop(cred.id, None)


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


def call_anthropic(key, model, prompt, max_tokens, interactive):
    anthropic = _anthropic()
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=12.0 if interactive else 120.0)
    # 思考 (thinking) 的用量也算在 max_tokens 裡：太小會在思考完之前就被截斷而沒有回答
    kwargs = {'model': model, 'max_tokens': max(max_tokens, 4000 if interactive else 16000),
              'messages': [{'role': 'user', 'content': prompt}]}
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
    if resp.stop_reason == 'refusal':
        raise ProviderError('refusal', getattr(resp.stop_details, 'category', '') or '')
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if not text:
        raise ProviderError('other', f'回應沒有內容 ({resp.stop_reason})')
    return text


def call_openai_compatible(provider, key, model, prompt, max_tokens, temperature, interactive):
    base = PROVIDERS[provider][2]
    body = {'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
    if provider == 'openai':
        # OpenAI 新模型改用 max_completion_tokens，推理模型不接受自訂 temperature
        body['max_completion_tokens'] = max(max_tokens, 4000)
    else:
        body.update(max_tokens=max_tokens, temperature=temperature)
    try:
        resp = requests.post(f'{base}/chat/completions', json=body, timeout=(3, 12) if interactive else (5, 60),
                             headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    except requests.RequestException as exc:
        raise ProviderError('overload', f'連線異常 {exc.__class__.__name__}')
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
    return text


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
        text, _source = ai_service.cloud_generate('只回覆兩個字：正常', max_tokens=50, patient=False, creds=[cred])
        if text:
            ok += 1
            lines.append(f'金鑰 {n}：{r.label}，模型 {" → ".join(r.models)} ({how})，回應正常')
        else:
            lines.append(f'金鑰 {n}：{r.label}，模型 {" → ".join(r.models)} ({how})，但呼叫失敗：{ai_service.last_ai_error()}')
    if not lines:
        raise AppError('尚未填寫任何金鑰')
    if not ok:
        raise AppError('；'.join(lines))
    return '；'.join(lines)


def _register_tester():
    from app.services import settings_service
    settings_service.register_tester('ai')(test_settings)


_register_tester()
