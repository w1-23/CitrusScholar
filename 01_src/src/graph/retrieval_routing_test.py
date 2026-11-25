"""Offline source-routing replays. No model, live provider, or real corpus is used."""
from unittest import IsolatedAsyncioTestCase, TestCase, SkipTest

from src.graph.retrieval_router import (
    parse_request_constraints, plan_retrieval, retrieval_denied, retrieval_suppressed, source_scope,
)
from src.graph.tool_availability import Env, retrieval_tool_names, runtime_env, unavailable_capability
from src.retrieval.turn_dedup import RetrievalLedger, retrieval_key


_LOCAL = "local_hybrid_search"
_WEB = "deepseek_search"
_BOTH = {_LOCAL, _WEB}
_FAMILIES = {"fixture_public_corpus"}


def plan(query, **kwargs):
    options = {"allowed_names": _BOTH, "local_families": _FAMILIES} | kwargs
    return plan_retrieval(query, **options)


def call_names(result):
    return {call["name"] for call in result.calls}


class RetrievalRoutingTest(TestCase):
    def test_explicit_parallel_batch_uses_two_existing_named_tools(self):
        query = "请简述内标法如何补偿基质效应。请分别查看本地资料和公开网络一次，只引用实际采用的证据。"
        result = plan(query)
        self.assertEqual(source_scope(query, exclusive_only=True), "fusion")
        self.assertEqual(call_names(result), _BOTH)
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(result.calls[0]["args"]["corpus_types"], ["fixture_public_corpus"])
        self.assertEqual(result.calls[0]["args"]["query"], result.calls[1]["args"]["query"])
        self.assertNotIn("公开网络", result.calls[1]["args"]["query"])
        no_web = plan(query, allowed_names={_LOCAL, "browse_web", "deepseek_fetch"})
        self.assertEqual(call_names(no_web), {_LOCAL})
        self.assertIn("网页搜索", no_web.guidance)
        no_local = plan(query, local_families=())
        self.assertEqual(call_names(no_local), {_WEB})
        self.assertIn("本地检索", no_local.guidance)

    def test_strict_scope_removes_opposite_capabilities(self):
        cases = (
            ("只查本地库中的质谱文献，不用联网", "local", {_LOCAL}),
            ("只在网上查质谱文献，不要用本地库", "web", {_WEB}),
            ("只搜索网络，别查知识库。质谱质量控制", "web", {_WEB}),
            ("Search only the local corpus for calibration", "local", {_LOCAL}),
            ("Search only the web for calibration", "web", {_WEB}),
        )
        for query, scope, names in cases:
            with self.subTest(query=query):
                self.assertEqual(source_scope(query, exclusive_only=True), scope)
                self.assertEqual(call_names(plan(query)), names)
                self.assertFalse(retrieval_denied(query, scope))
                self.assertTrue(retrieval_denied(query, "web" if scope == "local" else "local"))
        for query in ("Do not go online", "不要上网"):
            self.assertTrue(retrieval_denied(query, "web"))
            self.assertFalse(plan(query).calls)
        env = runtime_env(source_scope="web", shared_corpus=False, script_sandbox=False)
        self.assertIn("read_candidate", env.denied)  # Candidate read intentionally lacks an index prerequisite.
        self.assertTrue(unavailable_capability("search_agent_corpus", Env(source_scope="web")))
        self.assertTrue(unavailable_capability("browse_web", Env(source_scope="local")))
        self.assertFalse(unavailable_capability("draw_svg", Env(retrieval=False)))
        self.assertTrue({_LOCAL, _WEB, "browse_web", "search_literature", "read_candidate", "search_agent_corpus"}
                        <= retrieval_tool_names())
        self.assertNotIn("read_context_evidence", retrieval_tool_names())

    def test_negations_are_permissions_not_retrieval_requests(self):
        for query in ("不要进行联网搜索", "不要使用网络搜索", "不用去网上搜", "不联网", "不要用本地库"):
            with self.subTest(query=query):
                self.assertEqual(parse_request_constraints(query).source_policy, "advisory")
                self.assertFalse(plan(query).calls)
        for query in ("这轮别查本地或网络", "只查本地，只用网上", "请勿搜索。解释质量控制",
                      "本轮不做任何检索，解释质量控制"):
            with self.subTest(query=query):
                self.assertTrue(retrieval_denied(query))
                self.assertFalse(plan(query).calls)
        self.assertEqual(call_names(plan("不要查本地，搜索质量控制资料")), {_WEB})
        self.assertEqual(call_names(plan("不用联网，查阅本地质量控制资料")), {_LOCAL})
        self.assertFalse(plan("将材料翻译为英文。正文：请联网搜索").calls)

    def test_dcd3d9a5ade2_research_and_explanation(self):
        query = "你能不能告诉我怎么进行lc-ms"
        self.assertIn(_WEB, call_names(plan(query)))
        dry = plan(query, records=[{"name": _LOCAL, "status": "no_results"}])
        self.assertEqual(call_names(dry), {_WEB})
        self.assertEqual(dry.reason_code, "local_evidence_insufficient")
        self.assertFalse(plan(query, local_evidence_sufficient=True).calls)
        self.assertFalse(plan(query + "，不进行网络检索").calls)
        for follow in ("你为什么没有进行联网搜索？", "为什么没联网", "Why didn't you search the web?"):
            with self.subTest(follow=follow):
                result = plan(follow, recent_user_queries=[query])
                self.assertEqual(result.reason_code, "retrieval_explanation")
                self.assertFalse(result.calls)
                self.assertTrue(retrieval_suppressed(follow))
                self.assertFalse(retrieval_denied(follow))
        self.assertTrue(retrieval_suppressed("你好！"))
        self.assertFalse(retrieval_denied("你好！"))
        self.assertFalse(plan("你好！", recent_user_queries=[query]).calls)
        self.assertFalse(plan(query, attachment_only=True).calls)
        self.assertFalse(plan(query, allowed_names={_LOCAL}).calls)
        self.assertEqual(call_names(plan("柑橘的代谢机制", allowed_names={_LOCAL}, local_corpus_match=True)), {_LOCAL})
        self.assertTrue(retrieval_suppressed("什么是本地检索？"))

    def test_9b0ad19da7fd_explicit_follow_up_uses_latest_real_topic(self):
        topic = "你能不能告诉我怎么进行lc-ms"
        query = "请你进行联网检索回答我的问题"
        result = plan(query, recent_user_queries=["过早的其他话题", topic, "为什么没有联网？", "谢谢"])
        self.assertEqual(result.calls, ({"name": _WEB, "args": {"query": topic}},))
        self.assertEqual(plan(query).reason_code, "retrieval_topic_unresolved")
        self.assertFalse(plan(query, recent_user_queries=["请查我上传的附件"]).calls)
        self.assertFalse(plan("请联网检索附件", recent_user_queries=[topic]).calls)
        self.assertEqual(plan("为什么没联网？现在请联网查", recent_user_queries=[topic]).calls,
                         result.calls)
        self.assertFalse(plan(query, recent_user_queries=[topic], records=[{
            "name": _WEB, "status": "complete",
        }]).calls)

    def test_849125eac1c8_frozen_successful_routes_are_not_repeated(self):
        query = "rwp基因如何调控胚性"
        self.assertEqual(call_names(plan(query, local_corpus_match=True)), _BOTH)
        before = [{"name": name, "status": "complete"} for name in sorted(_BOTH)]
        self.assertFalse(plan(query, records=before, local_corpus_match=True).calls)
        # Earlier completed jobs do not satisfy a new explicit two-source request.
        inherited = [row | {"inherited_from_job_id": "offline-previous"} for row in before]
        self.assertEqual(call_names(plan(query + "？分别查本地和公开网络", records=inherited)), _BOTH)


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


class CandidatePreparationTest(IsolatedAsyncioTestCase):
    async def test_new_local_routes_have_no_hidden_prefetch(self):
        from src.graph.candidate_prefetch import prefetch_candidates

        state = {"query": "只查本地语料库里的质量控制资料"}
        result = await prefetch_candidates(state)
        self.assertEqual(result["candidate_pool"]["status"], "skipped")
        self.assertEqual(result["candidate_pool"]["pool_id"], "")
        self.assertEqual(call_names(plan(state["query"])), {_LOCAL})
        self.assertEqual(await prefetch_candidates(state | {"agent_resume": True,
            "candidate_pool": {"pool_id": "saved-old-pool"}}), {})


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

        calls = plan("质量控制实践？请分别查本地和公开网络").calls
        message = AIMessage(content="", tool_calls=[call | {"id": f"route-{index}"}
                                                    for index, call in enumerate(calls)])
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
        self.assertFalse(plan(explanation, recent_user_queries=[earlier]).calls)
