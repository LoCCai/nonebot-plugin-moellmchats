from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats import media_models
from nonebot_plugin_moellmchats import tokenhub_tools as tools
from nonebot_plugin_moellmchats.media_context import MediaAsset, MediaError, media_store
from nonebot_plugin_moellmchats.model_error_policy import is_quota_exhaustion_error_info
from nonebot_plugin_moellmchats.tool_contracts import ToolContext


def context(user='1', message=42):
    return ToolContext(bot=SimpleNamespace(self_id='10'), event=SimpleNamespace(
        group_id=20, message_id=message, get_user_id=lambda: user), request_id=message)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    selector = _initialized_selector(tmp_path, monkeypatch)
    for name, kind in [('hy-video-v1.5','video'), ('hy-image-v3','image'),
                       ('kinfra-text-embedding-0.6b','embedding'), ('kinfra-text-embedding-4b','embedding'),
                       ('hy-asr-3.0-preview','transcription'), ('wand-asr-v1','transcription')]:
        selector.models[f'{name} (tencent)'] = dict(model=name, provider='tencent', key=['unit-test-key'],
            model_type=kind, url='https://tokenhub.tencentmaas.com/v1/chat/completions')
    selector.model_config['embedding_model'] = 'kinfra-text-embedding-0.6b (tencent)'
    monkeypatch.setattr(tools, 'model_selector', selector)
    monkeypatch.setattr(tools, '_ledger_path', lambda: tmp_path/'jobs'/'tasks.json')
    monkeypatch.setattr(tools, 'can_generate_image', AsyncMock(return_value=True))
    return selector


@pytest.mark.parametrize('code', ['401007','401008','403004','CodeEndpointNoFreePackage','CodeEndpointFreeQuotaExhausted','CodeInsufficientBalance'])
def test_tencent_billing_codes_exhaust_pool(code):
    assert is_quota_exhaustion_error_info(402, {'error': {'code':code}})


@pytest.mark.parametrize('code', ['403001','403002','CodeModelAccessDenied','CodePermissionDenied'])
def test_tencent_permission_error_is_not_exhaustion(code):
    assert not is_quota_exhaustion_error_info(403, {'error': {'code':code}})


def test_nonchat_models_cannot_enter_pools_or_chat_roles(setup):
    name = 'hy-video-v1.5 (tencent)'
    with pytest.raises(ValueError):
        setup.validate_model_pools({'0':[name]}, setup.models)
    for method in [setup.set_chat_model,setup.set_category_model,setup.set_summary_model,setup.set_vision_model]:
        assert '不是' in method(name)
    assert '不是' in setup.set_moe_model(name,'0')
    setup.model_config['selected_model'] = name
    assert setup.get_model('selected_model') is None


def test_provider_key_reference_uses_driver_secret_without_persisting(setup, monkeypatch):
    import nonebot
    monkeypatch.setattr(nonebot.get_driver().config,'txhy_api_key','host-key',raising=False)
    assert setup._provider_key({'api_key_config':'txhy_api_key'}) == 'host-key'
    assert setup._provider_key({'api_key':['old']}) == ['old']
    with pytest.raises(ValueError):
        setup._provider_key({'api_key_config':'../secret'})


def test_disabled_media_role_cannot_be_bypassed_by_explicit_model(setup):
    setup.model_config['video_model']=''
    assert tools._models(tools.CATALOG['hy_video'],'hy-video-v1.5') == []


def test_disabled_model_not_resurrected_from_remote_cache(setup):
    setup.providers['good']['disabled_models'] = ['model-a']
    setup.cache_file.write_text(json.dumps({'good':['model-a','new']}))
    setup._load_all_models(strict=True)
    assert 'model-a (good)' not in setup.models
    assert 'new (good)' in setup.models


@pytest.mark.asyncio
async def test_generation_denied_before_network_or_asset_read(setup,monkeypatch):
    monkeypatch.setattr(tools,'can_generate_image',AsyncMock(return_value=False))
    request = AsyncMock()
    monkeypatch.setattr(tools,'request',request)
    result = await tools.call_model_capability(_tool_context=context(),operation='hy_video',parameters={'prompt':'a'})
    assert result.metadata['status'] == 'denied'
    request.assert_not_called()


@pytest.mark.asyncio
async def test_async_submission_deduplicated_persisted_and_scope_checked(setup,monkeypatch):
    request = AsyncMock(side_effect=[(200,{'id':'job/1','status':'queued'}),
                                    (200,{'status':'completed','data':{'url':'https://example.com/movie.mp4'}})])
    monkeypatch.setattr(tools,'request',request)
    first = await tools.call_model_capability(_tool_context=context(),operation='hy_video',parameters={'prompt':'a'})
    ref = first.metadata['task_ref']
    assert first.metadata['state'] == 'submitted'
    duplicate = await tools.call_model_capability(_tool_context=context(),operation='hy_video',parameters={'prompt':'a'})
    assert duplicate.metadata['status'] == 'already_attempted'
    assert request.await_count == 1
    other = await tools.get_model_task(_tool_context=context(user='2'),task_ref=ref)
    assert other.metadata['status'] == 'failed'
    # Clearing all transient caches leaves the persistent task queryable.
    media_store.entries.clear()
    result = await tools.get_model_task(_tool_context=context(),task_ref=ref)
    assert result.metadata['state'] == 'completed'
    assert request.call_args.args[1].endswith('/job%2F1')
    assert 'movie.mp4' in result.text


@pytest.mark.asyncio
async def test_unknown_submission_never_auto_replayed(setup,monkeypatch):
    request = AsyncMock(side_effect=MediaError('timeout'))
    monkeypatch.setattr(tools,'request',request)
    first = await tools.call_model_capability(_tool_context=context(),operation='hy_video',parameters={'prompt':'a'})
    assert first.metadata['status'] == 'failed'
    duplicate = await tools.call_model_capability(_tool_context=context(),operation='hy_video',parameters={'prompt':'a'})
    assert duplicate.metadata['status'] == 'already_attempted'
    queried = await tools.get_model_task(_tool_context=context(),task_ref=first.metadata['task_ref'])
    assert queried.metadata['status'] == 'unknown'
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_explicit_quota_error_tries_next_compatible_model_and_persists(setup,monkeypatch):
    request = AsyncMock(side_effect=[(402,{'error':{'code':'401008'}}), (200,{'text':'转写完成'})])
    monkeypatch.setattr(tools,'request',request)
    result = await tools.call_model_capability(_tool_context=context(),operation='transcribe',parameters={'data':'base64'})
    assert result.metadata['status'] == 'succeeded'
    assert setup.is_model_exhausted('hy-asr-3.0-preview (tencent)')
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_permission_error_preserved_without_quota_or_blind_retry(setup,monkeypatch):
    request = AsyncMock(return_value=(403,{'error':{'code':'403002','message':'secret-url'}}))
    monkeypatch.setattr(tools,'request',request)
    result = await tools.call_model_capability(_tool_context=context(),operation='transcribe',parameters={'data':'base64'})
    assert '403002' in result.text and 'secret-url' not in result.text
    assert not setup.is_model_exhausted('hy-asr-3.0-preview (tencent)')
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_semantic_retrieval_ranks_text_and_omits_raw_vectors(setup,monkeypatch):
    request = AsyncMock(return_value=(200,{'data':[{'index':2,'embedding':[0.0,1.0]},
        {'index':0,'embedding':[1.0,0.0]},{'index':1,'embedding':[1.0,0.0]}]}))
    monkeypatch.setattr(tools,'request',request)
    result = await tools.semantic_search(_tool_context=context(),query='技能',candidates=['技能说明','其他'],top_k=1)
    assert json.loads(result.text)['matches'][0]['text'] == '技能说明'
    assert 'embedding' not in json.loads(result.text)
    assert request.call_args.args[2]['input'] == ['技能','技能说明','其他']


@pytest.mark.asyncio
async def test_incomplete_or_nonfinite_vectors_rejected(setup,monkeypatch):
    monkeypatch.setattr(tools,'request',AsyncMock(return_value=(200,{'data':[{'embedding':[float('nan')]}]})))
    with pytest.raises(MediaError):
        await tools.embeddings('embed_text',{'input':['a','b']})


@pytest.mark.asyncio
async def test_asset_ref_encoding_and_private_ref_isolation(setup):
    ref = media_store.put(context(),MediaAsset('audio','a.wav',payload=b'audio',mime='audio/wav'))
    assert await tools._parameters(context(),{'asset_ref':ref,'encoding':'base64'}) == 'YXVkaW8='
    with pytest.raises(MediaError):
        await tools._parameters(context(user='2'),{'asset_ref':ref})
    with pytest.raises(MediaError):
        await tools._parameters(context(),{'callback_url':'https://example.com'})


@pytest.mark.asyncio
async def test_vector_references_compare_without_network_and_enforce_model_and_user(setup):
    query=media_store.put(context(),{'vectors':[[1,0]],'model':'same'})
    candidate=media_store.put(context(),{'vectors':[[0,1],[1,0]],'model':'same'})
    result=await tools.compare_embeddings(_tool_context=context(),query_ref=query,candidate_refs=[candidate])
    assert json.loads(result.text)['matches'][0]['index']==2
    result=await tools.compare_embeddings(_tool_context=context(user='2'),query_ref=query,candidate_refs=[candidate])
    assert result.metadata['status']=='failed'
    different=media_store.put(context(),{'vectors':[[1,0]],'model':'different'})
    result=await tools.compare_embeddings(_tool_context=context(),query_ref=query,candidate_refs=[different])
    assert result.metadata['status']=='failed'


@pytest.mark.asyncio
async def test_audio_analysis_transcribes_before_vision_and_keeps_text(setup,monkeypatch):
    from nonebot_plugin_moellmchats import media_tools
    from nonebot_plugin_moellmchats.media_context import MessageDocument
    monkeypatch.setattr(media_tools,'media_models_transcription_enabled',lambda:True)
    transcribe=AsyncMock(return_value='明天八点集合')
    monkeypatch.setattr(tools,'transcribe_asset',transcribe)
    ref=media_store.put(context(),MediaAsset('audio','a.wav',payload=b'audio',mime='audio/wav'))
    text,parts,warnings=await media_tools._prepared_content(context(),MessageDocument(),[ref])
    assert '明天八点集合' in text
    assert not parts and not warnings


def test_api_catalog_paths_and_poll_contracts_are_bounded():
    assert len(tools.CATALOG)>=50
    for op in tools.CATALOG.values():
        assert op['models']
        assert op['path'].startswith('/') and not op['path'].startswith('//')
        assert '..' not in op['path']
        if op['poll']:
            assert '{id}' in op['poll'] or op['poll_method']=='POST'
        assert op['doc'] in tools.GUIDES


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['hy_image','hy_image35','seedream_image'])
async def test_drawing_uses_documented_tencent_payload_and_deliverable_image(setup,monkeypatch,operation):
    import base64
    from io import BytesIO
    from PIL import Image
    stream=BytesIO()
    Image.new('RGB',(24,24),'blue').save(stream,format='PNG')
    model = setup.models['hy-image-v3 (tencent)']
    model.update(image_api_format='tokenhub', image_operation=operation)
    setup.model_config['image_model']='hy-image-v3 (tencent)'
    monkeypatch.setattr(media_models,'model_selector',setup)
    response={'choices':[{'delta':{'image':{'url':'https://example.com/image.png'}}}]} if operation=='hy_image35' else {
        'data':[{'b64_json':base64.b64encode(stream.getvalue()).decode()}]}
    post=AsyncMock(return_value=(200,response))
    monkeypatch.setattr(media_models,'_post',post)
    monkeypatch.setattr(media_models,'download_media',AsyncMock(return_value=(stream.getvalue(),'image/png')))
    result, _ = await media_models.generate_picture('蓝色方形',[],size='1024x1536')
    assert result == stream.getvalue()
    payload=post.call_args.kwargs['payload']
    assert payload['size'] == ('768x1152' if operation=='hy_image' else '1024x1536')
    assert ('messages' in payload) == (operation=='hy_image35')
