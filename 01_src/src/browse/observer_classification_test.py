"""Browse policy and transport classifications with a synthetic renderer."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.browse.relay import BrowseError, browse_reason_class
from src.browse.service import Renderer
from src.core.progress_bus import public_progress_payload, public_tool_outcome
from src.tools.browse_web import _output


@pytest.mark.parametrize(('reason', 'expected'), [
    ('browse_host_not_allowed', 'scope_limited'), ('browse_url_denied', 'scope_limited'),
    ('browse_network_error', 'source_unavailable'), ('browse_network_timeout', 'transient'),
])
def test_known_browse_reasons_preserve_scope_or_network(reason, expected):
    _, output = _output({'error': reason}, error=reason)
    assert browse_reason_class(reason) == expected
    assert public_tool_outcome(output['_tool_outcome'])['reason_class'] == expected
    assert ('source_guidance' in output) is (expected == 'scope_limited')


@pytest.mark.parametrize('top_level', [True, False])
def test_trusted_relay_reason_classifies_only_failed_top_level_navigation(top_level):
    tab = 't-aaaaaaaaaaaa'
    renderer = SimpleNamespace(lock=asyncio.Lock(), closing=False, actions=0,
        send=AsyncMock(), receive=AsyncMock(side_effect=[
            {'kind': 'request', 'url': 'https://example.org/', 'method': 'GET', 'navigation_tab': tab if top_level else ''},
            {'kind': 'result', 'error': 'browse_page_action_failed', 'tab_id': tab},
        ]), relay=SimpleNamespace(fetch=AsyncMock(side_effect=BrowseError('browse_host_not_allowed'))))
    if top_level:
        with pytest.raises(BrowseError, match='^browse_host_not_allowed$'):
            asyncio.run(Renderer.exchange(renderer, {'action': 'open'}))
    else:
        result = asyncio.run(Renderer.exchange(renderer, {'action': 'open'}))
        assert result['error'] == 'browse_page_action_failed'
    assert renderer.send.await_args_list[-1].args == ({'ok': False},)


def test_resume_preparation_has_a_distinct_fixed_label_under_replay():
    data = {'category': 'agent_prepare', 'origin': 'host', 'note_id': 'job:prepare'}
    new = public_progress_payload('assistant_progress', data)
    restore = public_progress_payload('assistant_progress', {**data, 'note_id': 'run:resume-prepare'})
    assert new['category'] == 'agent_prepare'
    assert restore['category'] == 'agent_resume_prepare'
    assert restore['text'] != new['text']
    assert public_progress_payload('assistant_progress', restore) == restore
