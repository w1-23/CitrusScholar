"""Synthetic, offline tests for the administrator activation boundaries."""
import asyncio
import json
from threading import Event, Thread
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from src import config
from src.api import main as api
from src.engine import hardware
from src.engine.embedder import Embedder
from src.engine.reranker import Reranker


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    for name in ('CITRUS_PROVIDER_API_KEY', 'CITRUS_PROVIDER_ID', 'CITRUS_PROVIDER_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    app_settings = config.Settings(_env_file=None, MAIN_API_KEY='synthetic-deployment-key',
                                   DEEPSEEK_API_KEY='', FAST_API_KEY='', FAST_BASE_URL='')
    monkeypatch.setattr(api, 'settings', app_settings)
    return app_settings


def test_atomic_config_origin_rotation_and_failed_replace(isolated_settings, monkeypatch, tmp_path):
    from src.config import model_origin

    settings = isolated_settings
    origin_a = 'https://model-a.example/v1'
    origin_b = 'https://model-b.example/v1'
    assert settings.model_transport().api_key == 'synthetic-deployment-key'
    assert model_origin('https://MODEL-A.example:443/v2') == model_origin(origin_a)
    for bad in ('https://model-a.example\\@model-b.example/v1',
                'https://model-a.example:invalid/v1',
                'https://user@model-a.example', 'https://model-a.example/v1?target=any'):
        assert not model_origin(bad)
    assert not settings.model_credential_for('custom_openai', origin_a)
    assert not settings.model_credential_for('deepseek', 'https://api.deepseek.com.example/v1')
    settings._runtime_provider_api_keys = {'custom_openai': 'synthetic-unbound-legacy'}
    assert not settings.model_credential_for('custom_openai', origin_a)
    assert settings.activate_model(revision='', provider_id='custom_openai', main='a',
                                   main_base_url=origin_a, new_key='synthetic-key-a')
    path = tmp_path / 'state' / 'model_activation.json'
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b'synthetic-deployment-key' not in saved
    assert settings.model_credential_for('custom_openai', 'https://MODEL-A.example:443/v2') == 'synthetic-key-a'
    assert not settings.model_credential_for('custom_openai', 'https://model-a.example:444/v1')
    assert not settings.model_credential_for('custom_openai', origin_b)
    assert not settings.fast_credential_for(origin_b, origin_a, 'synthetic-key-a')

    def fail_replace(*_args):
        raise OSError('synthetic storage fault')

    monkeypatch.setattr(config.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        settings.activate_model(revision=settings.model_activation_revision,
                                provider_id='custom_openai', main='b', main_base_url=origin_b,
                                new_key='synthetic-key-b')
    assert path.read_bytes() == saved
    assert settings.model_transport().base_url == origin_a
    assert settings.model_transport().api_key == 'synthetic-key-a'
    restarted = config.Settings(_env_file=None, MAIN_API_KEY='', DEEPSEEK_API_KEY='')
    restarted._load_runtime_model_activation()
    assert restarted.model_transport().api_key == 'synthetic-key-a'
    assert set(item.name for item in path.parent.iterdir()) == {'model_activation.lock', path.name}


def test_authorized_readonly_source_is_bound_and_restart_prefers_writable_activation(isolated_settings, tmp_path):
    settings = isolated_settings
    source = tmp_path / 'authorized-credential-source'
    state = source / 'state'
    state.mkdir(parents=True)
    source_cfg = state / 'model_config.json'
    source_keys = state / 'provider_api_keys.json'
    endpoint = 'https://opencode.ai/inference/go/openai/v1'
    source_cfg.write_text(json.dumps({'provider_id': 'opencode_go', 'main_base_url': 'https://elsewhere.example',
                                      'main_model': 'source-model'}))
    source_keys.write_text(json.dumps({'opencode_go': 'synthetic-source-key'}))
    settings._runtime_model_cfg = {'provider_id': 'opencode_go', 'main_model': 'source-model'}
    settings.bind_readonly_model_source(source)
    assert not settings.model_transport(include_headers=False).api_key
    source_cfg.write_text(json.dumps({'provider_id': 'opencode_go', 'main_base_url': endpoint,
                                      'main_model': 'source-model'}))
    snapshot = {file: file.read_bytes() for file in state.iterdir()}
    settings.bind_readonly_model_source(source)
    assert settings.model_transport(include_headers=False).api_key == 'synthetic-source-key'
    assert not settings.model_credential_for('custom_openai', endpoint)
    assert not settings.model_credential_for('opencode_go', endpoint.replace('.ai', '.ai.example'))
    assert settings.activate_model(revision='', provider_id='opencode_go', main='other-model', main_base_url=endpoint)
    writable = tmp_path / 'state' / 'model_activation.json'
    assert b'synthetic-source-key' not in writable.read_bytes()
    assert all(file.read_bytes() == previous for file, previous in snapshot.items())

    restarted = config.Settings(_env_file=None, MAIN_API_KEY='', DEEPSEEK_API_KEY='')
    restarted._load_runtime_model_activation()
    restarted.bind_readonly_model_source(source)
    assert restarted.model_transport(include_headers=False).api_key == 'synthetic-source-key'
    assert restarted.model_transport(include_headers=False).model == 'other-model'
    assert restarted.activate_model(revision=restarted.model_activation_revision, provider_id='opencode_go',
                                    main='other-model', main_base_url=endpoint, new_key='synthetic-writable-key')
    restarted._readonly_model_keys = {}
    restarted._load_runtime_model_activation()
    restarted.bind_readonly_model_source(source)
    assert restarted.model_transport(include_headers=False).api_key == 'synthetic-writable-key'
    assert not restarted._readonly_model_keys
    assert all(file.read_bytes() == previous for file, previous in snapshot.items())


def test_routes_do_not_probe_other_origin_with_reused_key_and_reject_stale_commit(isolated_settings, monkeypatch):
    async def scenario():
        origin_a = 'https://model-a.example/v1'
        origin_b = 'https://model-b.example/v1'
        entered, release = asyncio.Event(), asyncio.Event()
        probes = []

        async def catalog(provider, key, base, **_kwargs):
            probes.append((provider, key, base))
            if key == 'synthetic-slow-a':
                entered.set()
                await release.wait()
            return ['model-a', 'model-b']

        monkeypatch.setattr(api, '_fetch_provider_models', catalog)
        slow = asyncio.create_task(api.save_provider_config(api.ProviderConfigRequest(
            provider_id='custom_openai', api_key='synthetic-slow-a', main_model='model-a', api_base_url=origin_a)))
        await entered.wait()
        try:
            result = await api.save_provider_config(api.ProviderConfigRequest(
                provider_id='custom_openai', api_key='synthetic-key-b', main_model='model-b', api_base_url=origin_b))
            assert result['model']['main'] == 'model-b'
        finally:
            release.set()
        with pytest.raises(HTTPException) as stale:
            await slow
        assert stale.value.status_code == 409
        prior = list(probes)
        for call in (
            api.provider_models(api.ProviderModelsRequest(provider_id='custom_openai', api_key='', api_base_url=origin_a)),
            api.set_model_config(api.ModelConfigRequest(main_base_url=origin_a)),
        ):
            with pytest.raises(HTTPException) as rejected:
                await call
            assert rejected.value.status_code == 400
        assert probes == prior
        await api.set_api_key(api.ApiKeyRequest(api_key='synthetic-rotation-b'))
        await api.set_model_config(api.ModelConfigRequest(main_model='model-a'))
        result = isolated_settings.model_transport()
        assert (result.provider_id, result.model, result.base_url, result.api_key) == (
            'custom_openai', 'model-a', origin_b, 'synthetic-rotation-b')

    asyncio.run(scenario())


def test_available_cuda_probe_failure_and_concurrent_switch_preserve_old_runtime(monkeypatch, tmp_path):
    path = tmp_path / 'state' / 'onnx_device.json'
    monkeypatch.setattr(hardware, '_DEVICE_PATH', path)
    monkeypatch.setattr(hardware, '_runtime_mode', 'cpu')
    previous = SimpleNamespace(_shared_model=None)
    generation = ('cpu', previous, previous, 7)
    monkeypatch.setattr(hardware, '_ACTIVE_MODELS', generation)
    monkeypatch.setattr(hardware, '_probe_cache', {})
    monkeypatch.setattr(hardware, '_switch_work', None)
    monkeypatch.delenv('CITRUS_ONNX_DEVICE', raising=False)
    monkeypatch.delenv('CITRUS_REQUIRE_GPU', raising=False)
    monkeypatch.setattr(hardware, '_ort', lambda: SimpleNamespace(get_available_providers=lambda: [
        'CUDAExecutionProvider', 'CPUExecutionProvider']))
    monkeypatch.setattr(hardware, 'probe_provider', lambda _provider, _cache=None: (False, 'synthetic failure'))
    hardware._write_mode('cpu')
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match='执行探针'):
        hardware.set_device_mode('gpu')
    assert hardware._ACTIVE_MODELS is generation
    assert hardware._probe_cache == {}
    assert path.read_bytes() == original

    staged, release = Event(), Event()
    failures = []
    monkeypatch.setattr(hardware, 'get_ort_providers', lambda **kwargs: ['CUDAExecutionProvider'])
    monkeypatch.setattr(Embedder, '_new_runtime', lambda **kwargs: SimpleNamespace(embed_query=lambda _: [1.0]))

    def failed_reranker(**kwargs):
        staged.set()
        assert release.wait(5)
        raise RuntimeError('synthetic reranker failure')

    monkeypatch.setattr(Reranker, '_new_runtime', failed_reranker)

    def run_switch():
        try:
            hardware.set_device_mode('gpu')
        except RuntimeError as exc:
            failures.append(str(exc))

    change = Thread(target=run_switch)
    change.start()
    try:
        assert staged.wait(3)
        assert hardware.runtime_readiness()['mode'] == 'cpu'
        assert hardware._ACTIVE_MODELS is generation
        with pytest.raises(RuntimeError, match='正在验证另一项'):
            hardware.set_device_mode('cpu')
    finally:
        release.set()
        change.join(5)
    assert failures and '重排序模型' in failures[0]
    assert hardware._ACTIVE_MODELS is generation
    assert path.read_bytes() == original
