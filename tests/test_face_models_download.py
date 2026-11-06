"""人臉模型檔不進版控：缺檔時下載並比對 SHA-256 (校驗不符不保留)、系統健康頁的人臉狀態、flask models download"""
import hashlib
import os

import pytest

from app.services import cv_service


class _Resp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self._data), size):
            yield self._data[i:i + size]


@pytest.fixture
def fake_models(monkeypatch, tmp_path):
    good = b'yunet-bytes' * 1000
    paths = {str(tmp_path / 'yunet.onnx'): ('https://example.test/yunet.onnx', hashlib.sha256(good).hexdigest()),
             str(tmp_path / 'sface.onnx'): ('https://example.test/sface.onnx', 'deadbeef' * 8)}
    monkeypatch.setattr(cv_service, 'MODEL_FILES', paths)
    served = {'https://example.test/yunet.onnx': good, 'https://example.test/sface.onnx': b'tampered'}
    calls = []

    def fake_get(url, stream=True, timeout=None):
        calls.append(url)
        return _Resp(served[url])
    import requests
    monkeypatch.setattr(requests, 'get', fake_get)
    return tmp_path, calls


def test_download_verifies_checksum_and_keeps_only_valid_files(fake_models):
    tmp_path, calls = fake_models
    with pytest.raises(RuntimeError, match='校驗碼不符'):
        cv_service.download_models(log=lambda m: None)
    assert (tmp_path / 'yunet.onnx').exists(), '校驗正確的檔案保留'
    assert not (tmp_path / 'sface.onnx').exists() and not list(tmp_path.glob('*.part')), '校驗不符的檔案與暫存檔都不留'
    assert [os.path.basename(p) for p in cv_service.missing_models()] == ['sface.onnx']
    # 已存在的不重抓；force 才會
    calls.clear()
    with pytest.raises(RuntimeError):
        cv_service.download_models(log=lambda m: None)
    assert calls == ['https://example.test/sface.onnx']


def test_face_status_reports_missing_models(monkeypatch, tmp_path):
    monkeypatch.setattr(cv_service, 'MODEL_FILES', {str(tmp_path / 'x.onnx'): ('u', 'h')})
    if cv_service.FACE_AVAILABLE:
        ok, reason = cv_service.face_status()
        assert ok is False and 'x.onnx' in reason and 'models download' in reason
    else:
        assert cv_service.face_status()[0] is False
    assert cv_service.get_face_models() == (None, None) or cv_service.FACE_AVAILABLE
    assert cv_service.find_best_match_vectorized(None, []) == (None, 0.0)


def test_models_cli_and_health_tile(app, fake_models, admin_client):
    tmp_path, _calls = fake_models
    runner = app.test_cli_runner()
    result = runner.invoke(args=['models', 'download'])
    assert result.exit_code != 0 and '校驗碼不符' in result.output, result.output
    assert (tmp_path / 'yunet.onnx').exists()
    html = admin_client.get('/admin/system').get_data(as_text=True)
    assert 'id="faceTile"' in html and '人臉辨識登入' in html


def test_models_not_tracked_by_git():
    root = os.path.dirname(os.path.dirname(__file__))
    ignore = open(os.path.join(root, '.gitignore'), encoding='utf-8').read()
    assert 'models/*.onnx' in ignore and 'models/*.gguf' in ignore


def test_windows_https_uses_system_certificate_store(monkeypatch):
    """防毒 / 公司網路攔截 HTTPS 時，只信任 certifi 會 CERTIFICATE_VERIFY_FAILED：Windows 改用系統憑證庫 (truststore)"""
    import sys

    from app.services import http_client
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    with open(os.path.join(root, 'requirements.txt'), encoding='utf-8') as fh:
        lines = [ln.strip() for ln in fh if ln.startswith('truststore==')]
    assert lines and lines[0].endswith('; sys_platform == "win32"'), '只在 Windows 門市安裝 (線上端 Linux 容器用系統預設)'

    monkeypatch.setattr(http_client, '_system_certs_done', False)
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert http_client.use_system_certificates() is False   # 非 Windows 不動 ssl

    injected = []
    fake = type(sys)('truststore')
    fake.inject_into_ssl = lambda: injected.append(True)
    monkeypatch.setitem(sys.modules, 'truststore', fake)
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert http_client.use_system_certificates() is True and injected == [True]
    assert http_client.use_system_certificates() is True and injected == [True], '只注入一次'


def test_certificate_failure_warning_has_hint(monkeypatch, caplog):
    monkeypatch.setattr(cv_service, 'FACE_AVAILABLE', True)
    monkeypatch.setattr(cv_service, 'missing_models', lambda: ['x.onnx'])

    def boom(**kwargs):
        raise RuntimeError('SSLError(SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))')
    monkeypatch.setattr(cv_service, 'download_models', boom)
    with caplog.at_level('WARNING', logger='app.services.cv_service'):
        cv_service.ensure_models_async().join(5)
    assert 'REQUESTS_CA_BUNDLE' in caplog.text and 'flask --app run models download' in caplog.text
