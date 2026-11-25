from src.graph.request_plan import explicit_note_target
from src.retrieval.turn_dedup import RetrievalLedger


def test_note_save_requires_exact_current_target_and_command():
    assert explicit_note_target('请把结论保存到项目「季度统计」的记录') == '季度统计'
    for text in ('不要把结论保存到项目「季度统计」', '如何保存到项目「季度统计」？',
                 '举例：记录到课题「统计」', '“记录到课题「统计」”',
                 '正文：请把结论保存到项目「季度统计」', '保存到项目',
                 '保存到课题「甲」，记录到课题「乙」'):
        assert not explicit_note_target(text), text


def test_only_one_identical_transient_read_retry_and_no_uncertain_retry():
    ledger = RetrievalLedger()
    args = {'query': 'sample'}
    assert ledger.claim('deepseek_search', args, 'first') is None
    assert ledger.claim('deepseek_search', args, 'concurrent') == 'first'
    ledger.settle('deepseek_search', args, 'first', status='failed', reason='timeout')
    assert ledger.claim('deepseek_search', args, 'retry') is None
    ledger.settle('deepseek_search', args, 'retry', status='failed', reason='timeout')
    assert ledger.claim('deepseek_search', args, 'excess') == 'retry'
    other = {'query': 'other'}
    assert ledger.claim('deepseek_search', other, 'first-other') is None
    ledger.settle('deepseek_search', other, 'first-other', status='failed', reason='timeout', uncertain=True)
    assert ledger.claim('deepseek_search', other, 'blocked') == 'first-other'
