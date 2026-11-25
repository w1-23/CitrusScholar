"""Offline source-routing replays. No model, live provider, or real corpus is used."""
from unittest import IsolatedAsyncioTestCase, TestCase, SkipTest

from src.graph.retrieval_router import (
    decide_retrieval, parse_request_constraints, required_route_groups, retrieval_denied,
    retrieval_suppressed, source_scope,
)
from src.graph.tool_availability import Env, retrieval_tool_names, runtime_env, unavailable_capability
from src.retrieval.turn_dedup import RetrievalLedger, retrieval_key


_LOCAL = "local_hybrid_search"
_WEB = "deepseek_search"
_BOTH = {_LOCAL, _WEB}


class RetrievalRoutingTest(TestCase):
    def test_explicit_both_and_optional_alternative_are_distinct(self):
        for query in ("请分别查看本地资料和公开网络", "Search both local corpus and web for calibration"):
            with self.subTest(query=query):
                self.assertEqual(source_scope(query, exclusive_only=True), "fusion")
                self.assertEqual(len(required_route_groups(decide_retrieval(query))), 2)
        for query in ("请在本地或公开网络搜索质谱资料", "Search local corpus or web for calibration"):
            with self.subTest(query=query):
                self.assertEqual(source_scope(query, exclusive_only=True), "auto")
                self.assertEqual(required_route_groups(decide_retrieval(query)), ())

    def test_strict_scope_removes_opposite_capabilities(self):
        for query, scope in (
                ("只查本地库中的质谱文献，不用联网", "local"),
                ("只在网上查质谱文献，不要用本地库", "web"),
                ("只搜索网络，别查知识库。质谱质量控制", "web"),
                ("Search only the local corpus for calibration", "local"),
                ("Search only the web for calibration", "web")):
            with self.subTest(query=query):
                self.assertEqual(source_scope(query, exclusive_only=True), scope)
                self.assertEqual(len(required_route_groups(decide_retrieval(query))), 1)
                self.assertFalse(retrieval_denied(query, scope))
                self.assertTrue(retrieval_denied(query, "web" if scope == "local" else "local"))
        env = runtime_env(source_scope="web", shared_corpus=False, script_sandbox=False)
        self.assertIn("read_candidate", env.denied)
        self.assertTrue(unavailable_capability("search_agent_corpus", Env(source_scope="web")))
        self.assertTrue(unavailable_capability("browse_web", Env(source_scope="local")))
        self.assertFalse(unavailable_capability("draw_svg", Env(retrieval=False)))
        self.assertTrue({_LOCAL, _WEB, "browse_web", "search_literature", "read_candidate", "search_agent_corpus"}
                        <= retrieval_tool_names())
        self.assertNotIn("read_context_evidence", retrieval_tool_names())

    def test_negations_never_authorize_another_source(self):
        for query in ("不要进行联网搜索", "不要使用网络搜索", "不用去网上搜", "不联网", "不要用本地库",
                      "请勿将上传附件外发", "Do not send any private data to third parties"):
            with self.subTest(query=query):
                self.assertEqual(parse_request_constraints(query).source_policy, "advisory")
                self.assertEqual(required_route_groups(decide_retrieval(query)), ())
                self.assertTrue(retrieval_denied(query, "local" if "本地" in query else "web"))
        for query in ("这轮别查本地或网络", "只查本地，只用网上", "请勿搜索。解释质量控制",
                      "本轮不做任何检索，解释质量控制"):
            with self.subTest(query=query):
                self.assertTrue(retrieval_denied(query))
        self.assertEqual(decide_retrieval("不要查本地，搜索质量控制资料").mode, "web")
        self.assertEqual(decide_retrieval("不用联网，查阅本地质量控制资料").mode, "local")
        self.assertEqual(required_route_groups(decide_retrieval("将材料翻译为英文。正文：请联网搜索")), ())

    def test_topic_terms_and_freshness_do_not_create_tool_obligations(self):
        for query in ("你能不能告诉我怎么进行lc-ms", "rwp基因如何调控胚性", "柑橘代谢机制的最新文献",
                      "What is the latest energy report today?"):
            self.assertEqual(required_route_groups(decide_retrieval(query)), ())
        for follow in ("你为什么没有进行联网搜索？", "为什么没联网", "Why didn't you search the web?"):
            self.assertEqual(decide_retrieval(follow).reason_code, "retrieval_explanation")
            self.assertTrue(retrieval_suppressed(follow))
            self.assertFalse(retrieval_denied(follow))
        self.assertTrue(retrieval_suppressed("你好！"))
        self.assertTrue(retrieval_suppressed("什么是本地检索？"))
        reused = decide_retrieval("基于以上继续分析", has_history_evidence=True, recent_turn_seqs=[2])
        self.assertEqual((reused.mode, reused.history_turn_seqs), ("reuse", (2,)))


class RetrievalDedupTest(TestCase):
    def test_9b0ad19da7fd_32_proposals_need_only_distinct_fixture_calls(self):
        ledger = RetrievalLedger()
        proposals = [(_WEB, {"query": " Fixture  ARTICLE "}),
                     ("deepseek_fetch", {"url": "https://Example.org:443/P#first"}),
                     ("read_tool_result", {"tool_call_id": "fetch-result", "fields": ["/text", "/title"]}),
                     ("read_tool_result", {"tool_call_id": "fetch-result", "cursor": "next"})] * 8
        unique = []
        for i, (name, args) in enumerate(proposals):
            prior = ledger.claim(name, args, f"call-{i}")
            if prior:
                self.assertEqual(prior, f"call-{i % 4}")
            else:
                unique.append(i)
        self.assertEqual(unique, [0, 1, 2, 3])
        self.assertEqual(ledger.claim(_WEB, {"query": "fixture article", "count": 5}, "normalized"), "call-0")
        self.assertEqual(ledger.claim("deepseek_fetch", {"url": "https://example.org/P#other"}, "fragment"), "call-1")
        self.assertEqual(ledger.claim("read_tool_result", {"tool_call_id": "fetch-result",
            "fields": ["/title", "/text"]}, "fields"), "call-2")
        self.assertIsNone(RetrievalLedger().claim(_WEB, proposals[0][1], "another-turn"))
        self.assertIsNone(ledger.claim("deepseek_fetch", {"url": "https://example.org/p"}, "case-sensitive-path"))
        self.assertIsNone(ledger.claim("deepseek_fetch", {"url": "https://example.org/P?v=2"}, "new-query-part"))

    def test_distinct_queries_read_pages_and_tools_are_not_collapsed(self):
        layer = RetrievalLedger()
        self.assertIsNone(layer.claim(_WEB, {"query": "fixture article"}, "original"))
        self.assertIsNone(layer.claim(_LOCAL, {"query": "fixture article", "corpus_types": ["fixture"]}, "local"))
        self.assertIsNone(layer.claim(_WEB, {"query": "fixture other article"}, "new"))
        self.assertIsNone(layer.claim(_WEB, {"query": "fixture article", "need_content": True}, "enriched"))
        scope = {"turn_id": "turn", "generation": "corpus-version"}
        layer.restore("read_local_evidence", scope | {"ref": "test-src"}, "from-checkpoint")
        self.assertEqual(layer.claim("read_candidate", scope | {"source_ref": "test-src"}, "alias"), "from-checkpoint")
        self.assertIsNone(layer.claim("read_candidate", scope | {"source_ref": "test-src", "offset": 4000}, "next-page"))
        self.assertIsNone(layer.claim("read_candidate", scope | {"source_ref": "test-src", "generation": "new-version"}, "new-generation"))
        self.assertIsNone(retrieval_key("draw_svg", {"svg": "fixture"}))


class FrameworkToolBatchTest(IsolatedAsyncioTestCase):
    async def test_dual_fixture_dispatches_concurrently_via_sdk_tool_node(self):
        import asyncio
        try:
            from langchain_core.messages import AIMessage
            from langchain_core.tools import tool
            from langgraph.prebuilt import ToolNode
        except ImportError as exc:
            raise SkipTest("pinned SDK needs the isolated application image") from exc

        started = set()
        both_started = asyncio.Event()

        async def observe(name, query):
            started.add(name)
            if started == _BOTH:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=3)
            return f"offline fixture for {query}"

        @tool
        async def local_hybrid_search(query: str, corpus_types: list[str]) -> str:
            """Return an inert local fixture result."""
            self.assertEqual(corpus_types, ["fixture_public_corpus"])
            return await observe(_LOCAL, query)

        @tool
        async def deepseek_search(query: str) -> str:
            """Return an inert public web fixture result."""
            return await observe(_WEB, query)

        # The model, not a planner, proposed both named calls for this explicit request.
        message = AIMessage(content="", tool_calls=[
            {"name": _LOCAL, "id": "route-local", "args": {"query": "质量控制实践", "corpus_types": ["fixture_public_corpus"]}},
            {"name": _WEB, "id": "route-web", "args": {"query": "质量控制实践"}},
        ])
        node = ToolNode([local_hybrid_search, deepseek_search], handle_tool_errors=False)
        from langgraph.graph import END, START, StateGraph, MessagesState
        graph = StateGraph(MessagesState)
        graph.add_node("tools", node)
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        result = await graph.compile().ainvoke({"messages": [message]})
        receipts = result["messages"][1:]
        self.assertEqual({item.name for item in receipts}, _BOTH)
        self.assertEqual({item.content for item in receipts}, {"offline fixture for 质量控制实践"})


class RetrievalHistoryBoundaryTest(TestCase):
    def test_explanation_keeps_authorized_previous_rows_without_new_search(self):
        from unittest.mock import patch
        try:
            from langchain_core.messages import HumanMessage, ToolMessage
            import pydantic_settings  # noqa: F401
        except ImportError as exc:
            raise SkipTest("pinned SDK and settings need the isolated application image") from exc
        from src.core.capability_policy import history_rows, new_policy

        earlier = "你能不能告诉我怎么进行lc-ms"
        explanation = "你为什么没有进行联网搜索？"
        with patch("src.core.capability_policy._revocations", return_value=frozenset()):
            before = new_policy({"query": earlier})
            after = new_policy({"query": explanation})
        messages = [HumanMessage(content=earlier), ToolMessage(content="inert local observation", tool_call_id="past-local")]
        for message in messages:
            message.response_metadata["citrus_capability_history"] = before.history_key
        self.assertEqual(history_rows(messages, [1, 2], after), (messages, [1, 2]))
        self.assertTrue(retrieval_suppressed(explanation))
        self.assertEqual(required_route_groups(decide_retrieval(explanation)), ())
