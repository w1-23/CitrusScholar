# -*- coding: utf-8 -*-
"""Deterministic source-routing decision for the general service agent.

The decision is internal. It prevents source chips from becoming a model tool:
the UI derives chips from actual tool artifacts and selected history evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.tool_catalog import capability_groups

_GROUPS = capability_groups()
WEB_GROUP = _GROUPS["route_web"]
LOCAL_GROUP = _GROUPS["route_local"]

_REUSE_CUE = re.compile(
    r"(?:上述|前面|刚才|刚刚|这些|其中|前者|后者|该项|该结果|该文献|它|这个结果|"
    r"上一轮|进一步比较|哪个更重要|继续分析|基于以上|据此|从中|"
    r"the above|these results?|former|latter|it|compare (?:them|these)|which is more)",
    re.I,
)
_NEW_RETRIEVAL_CUE = re.compile(
    r"(?:最新|新进展|再查|重新(?:检索|搜索)|补充(?:检索|搜索)|另查|换一批|"
    r"最近|今年|截至|最新版|新发现|新物种|新基因|新地点|"
    r"https?://|\bdoi\b|10\.\d{4,9}/|"
    r"latest|recent|new\s+(?:paper|study|species|trial|data)|search again|"
    r"look\s+up\s+again)",
    re.I,
)
_PURE_TRANSFORM = re.compile(
    r"(?:请)?(?:改写|润色|翻译|概括|总结一下(?:上面|上述)|压缩|扩写|"
    r"把上面|基于上文|解释一下(?:上面|上述)|计算|换算|"
    r"rewrite|proofread|translate|summari[sz]e\s+the\s+above|reformat)",
    re.I,
)
_SOCIAL = re.compile(
    r"^\s*(?:你好|您好|hi|hello|嗨|在吗|谢谢|多谢|thanks?|thank\s+you|"
    r"你是谁|你叫什么|早上好|晚上好|早安|晚安|好的|收到|hello there|谢谢你的帮助)[\s!！。,.，?？~]*$",
    re.I,
)
_MEMORY_COMMAND = re.compile(
    r"^\s*(?:请|帮我|麻烦)?(?:记住|记一下|记下来|保存到(?:账号)?记忆)|"
    r"^\s*(?:从)?以后(?:都|请|默认).{0,120}(?:回答|使用|保留|记住)",
    re.I,
)
_LATEST = re.compile(
    r"最新|最近|今年|当前|实时|今天|latest|current|recent|today|news",
    re.I,
)
_REQUIRED = re.compile(
    r"请(?:你|帮我)?(?:联网|网上|网页)?(?:检索|搜索|搜一下|查找|查最新|查这篇|查阅)|"
    r"(?:帮我|麻烦)?(?:联网|网上|网页)(?:检索|搜索|搜|查)|"
    r"(?:^|[，,])\s*(?:检索|搜索|查找|搜一下)|search for|please search|find papers?|look up",
    re.I,
)
_DATA_AUDIT = re.compile(
    r"(?:检查|审查|核查|审计|检验).{0,20}"
    r"(?:上传|附件|表格|工作表|excel|xlsx|xls|csv|数据)|"
    r"(?:表格|工作表|excel|xlsx|xls|csv|上传数据).{0,20}"
    r"(?:缺失值|重复|异常|离群|标注|审查|检查)",
    re.I,
)
# Keep positive source requests distinct from permission limits. A ban on
# one source alone does not instruct the agent to search the other.
_LOCAL = r"(?:本地(?:资料库|语料库|资料|语料|库)?|知识库|库内|当前索引|\blocal(?:[ _-]hybrid[ _-]search|\s+(?:corpus|database|knowledge\s+base|sources?|search))?\b)"
_WEB = r"(?:公开(?:互联网|网络|网)|互联网|联网|上网|网络|網絡|网上|网页|\b(?:web|internet|online)(?:\s+(?:sources?|search))?\b)"
_SOURCE = rf"(?:{_LOCAL}|{_WEB})"
_ACTION = r"(?:检索|搜索|查询|查找|查阅|查看|查|搜|核对|\b(?:search(?:ing)?|look\s+up|check|find|brows(?:e|ing))\b)"
_CONNECT = r"(?:以及|或者|和|与|及|或|并|、|/|\band\b|\bor\b)"
_LOCATION = r"(?:使用|用|在|从|进行|做|去|\b(?:go|use|using|on|in|with)\b)"
_NEGATIVE = r"(?:不要|不许|不再|不需要|不用|无需|无须|不必|请勿|禁止|(?<![分区特类个])别|do\s+not|don['’]t|without|no\s+need\s+to)"
_EXCLUSIVE = r"(?:只(?:能|许|准)?|仅(?:限|可)?|\b(?:only|exclusively)\b)"
_SOURCE_CHAIN = rf"{_SOURCE}(?:\s*{_CONNECT}\s*{_SOURCE})?"
_SOURCE_BAN = re.compile(
    rf"{_NEGATIVE}\s*(?:(?:再|继续|直接|额外)\s*)?(?:{_LOCATION}\s*)?"
    rf"(?:{_ACTION}\s*(?:一下|the\s+)?\s*)?{_SOURCE_CHAIN}(?:\s*{_ACTION})?|"
    rf"(?:^|[，,\s])(?:先|暂)?不(?:\s*(?:{_ACTION}|{_LOCATION}))?\s*{_SOURCE_CHAIN}(?:\s*{_ACTION})?|"
    rf"\bno\s+{_SOURCE}(?=\s|$|[,.!?])", re.I)
_SEARCH_BAN = re.compile(
    rf"(?:{_NEGATIVE}|(?:暂|先)?不)\s*(?:(?:再|继续|直接|额外|进行|做|任何)\s*){{0,3}}{_ACTION}|"
    r"\bno\s+(?:search|retrieval)\b", re.I)
_ONLY_SOURCE = re.compile(
    rf"{_EXCLUSIVE}\s*(?:(?:{_LOCATION}|{_ACTION}|采用|依据|看|the\b|一下|一次)\s*){{0,4}}{_SOURCE_CHAIN}|"
    rf"(?:{_ACTION}\s+(?:(?:on|in|with|the)\s+)?)?{_SOURCE_CHAIN}\s+{_EXCLUSIVE}", re.I)
_SOURCE_ACTION = re.compile(
    rf"{_SOURCE}[^，。！？；,!?.;\n]{{0,14}}?{_ACTION}|"
    rf"{_ACTION}[^，。！？；,!?.;\n]{{0,18}}?{_SOURCE}", re.I)
_BOTH_ACTION = re.compile(r"分别|同时|都要|都用|结合|交叉核对|对照|\bboth\b", re.I)
_PAIR_JOIN = re.compile(rf"{_SOURCE}\s*(?:以及|和|与|及|并|、|\band\b)\s*{_SOURCE}", re.I)
_PAIR_ALTERNATIVE = re.compile(rf"{_SOURCE}\s*(?:或者|或|/|\bor\b)\s*{_SOURCE}", re.I)
_GENERIC_FUSION = re.compile(r"融合(?:搜索|检索|查询)", re.I)
_PRIVATE_EGRESS_DENIAL = re.compile(
    r"(?:不要|禁止|不得|请勿|别|不许)\s*(?:将.{0,24}?)?(?:外发|外传|上传到(?:公网|互联网|第三方))|"
    r"\b(?:do not|don['’]t|never)\s+(?:send|upload|share)\b.{0,60}\b(?:externally|outside|online|third parties)\b", re.I)
_SOURCE_EXPLANATION = re.compile(
    rf"(?:为什么|为何|怎么|\bwhy\b)[^。！？；!?;\n]{{0,30}}?"
    rf"(?:没(?:有)?|未|不曾|did(?:\s+not|n['’]t)|haven['’]t)[^。！？；!?;\n]{{0,18}}?"
    rf"(?:{_SOURCE}|{_ACTION})|(?:{_SOURCE_CHAIN}|融合搜索|融合检索)[^。！？；!?;\n]{{0,16}}"
    r"(?:是什么|什么意思|有什么区别|含义|如何理解)|"
    rf"(?:什么是|何谓|\bwhat\s+is\b)\s*(?:{_SOURCE}\s*{_ACTION}|融合搜索|融合检索)", re.I)
_SCOPE_VALUES = {"auto", "local", "web", "fusion"}


@dataclass(frozen=True)
class _SourceIntent:
    requested: str = "auto"
    exclusive: str = "auto"
    either: bool = False
    blocked: frozenset[str] = frozenset()
    explanation: bool = False
    action_text: str = ""


def _has_source(text: str, name: str) -> bool:
    return bool(re.search(_LOCAL if name == "local" else _WEB, text, re.I))


def _route_for(names: set[str]) -> str:
    return "fusion" if len(names) == 2 else next(iter(names), "auto")


def _source_intent(query: str) -> _SourceIntent:
    from src.graph.intent_text import intent_clauses

    clauses = intent_clauses(query)
    # An explanatory follow-up does not ask for fresh retrieval. Preserve a
    # separate actionable clause such as “为何没查？现在请联网检索”.
    explanation = any(_SOURCE_EXPLANATION.search(clause) for clause in clauses)
    action_text = "，".join(clause for clause in clauses if not _SOURCE_EXPLANATION.search(clause))
    blocked = {name for match in _SOURCE_BAN.finditer(action_text) for name in ("local", "web")
               if _has_source(match[0], name)}
    positive = _SOURCE_BAN.sub(" ", action_text)
    if _SEARCH_BAN.search(positive):
        blocked.update(("local", "web"))
    if _PRIVATE_EGRESS_DENIAL.search(positive):
        blocked.add("web")
    exclusive_clauses = [{name for name in ("local", "web") if _has_source(match[0], name)}
                         for match in _ONLY_SOURCE.finditer(positive)]
    for names in exclusive_clauses:
        blocked.update({"local", "web"} - names)
    exclusive_names = set().union(*exclusive_clauses)
    allowed = {"local", "web"} - blocked
    requested = set(exclusive_names)
    mentions_both = all(_has_source(positive, name) for name in ("local", "web"))
    either = bool(mentions_both and _PAIR_ALTERNATIVE.search(positive) and not _BOTH_ACTION.search(positive))
    if not either and (_GENERIC_FUSION.search(positive) or (mentions_both and (
            _BOTH_ACTION.search(positive) or (_PAIR_JOIN.search(positive) and _ACTION.search(positive))))):
        requested.update(("local", "web"))
    elif not either:
        for match in _SOURCE_ACTION.finditer(positive):
            requested.update(name for name in ("local", "web") if _has_source(match[0], name))
        # “请联网回答” requests a web operation even without a search verb.
        if re.search(r"(?:请|帮我|麻烦|继续|现在|重新)\s*(?:你|先|进行)?\s*联网|"
                     r"^\s*联网\s*(?:回答|给出|帮我|核实)", positive, re.I):
            requested.add("web")
    return _SourceIntent(_route_for(requested & allowed),
                         _route_for(allowed) if len(allowed) == 1 else "auto",
                         either, frozenset(blocked), explanation, positive)


def source_scope(query: str, *, exclusive_only: bool = False) -> str:
    """Share one source parser with capability admission and route planning."""
    intent = _source_intent(query)
    if intent.exclusive != "auto":
        return intent.exclusive
    if exclusive_only and intent.requested != "fusion":
        return "auto"
    return intent.requested


def retrieval_denied(query: str, scope: str = "auto") -> bool:
    """Report an explicit opt-out for both routes or for this exact source."""
    intent = _source_intent(str(query or ""))
    return len(intent.blocked) == 2 or scope in intent.blocked


def retrieval_suppressed(query: str) -> bool:
    """Suppress new retrieval without filtering authorized historical context."""
    q = str(query or "").strip()
    intent = _source_intent(q)
    return (bool(_SOCIAL.fullmatch(q) or _MEMORY_COMMAND.search(q))
            or (intent.explanation and intent.requested == "auto"
                and not _REQUIRED.search(intent.action_text)))


@dataclass(frozen=True)
class RequestConstraints:
    """Deterministic user constraints, separate from model tool suggestions."""

    source_policy: str
    requested_mode: str
    required_tool_groups: tuple[frozenset[str], ...]
    data_audit_requested: bool
    pure_transform: bool
    reason_code: str


def parse_request_constraints(
    query: str,
    scope: str = "auto",
    *,
    has_table_attachment: bool = False,
) -> RequestConstraints:
    """Parse only executable user constraints; never infer from model prose."""
    q = str(query or "").strip()
    selected_scope = str(scope or "auto").strip().casefold()
    if selected_scope not in {"auto", "local", "web", "fusion"}:
        selected_scope = "auto"
    intent = _source_intent(q)
    explicit = bool(_REQUIRED.search(intent.action_text))
    audit = bool(_DATA_AUDIT.search(q))
    pure = bool(_PURE_TRANSFORM.search(q))

    if intent.explanation and intent.requested == "auto" and not explicit:
        return RequestConstraints("deny", "none", (), audit, pure, "retrieval_explanation")
    if _SOCIAL.fullmatch(q) or _MEMORY_COMMAND.search(q):
        return RequestConstraints("deny", "none", (), audit, pure, "no_retrieval_or_social")
    if len(intent.blocked) == 2 or selected_scope in intent.blocked:
        return RequestConstraints("deny", "none", (), audit, pure, "user_denied_retrieval")
    mode = intent.requested
    if (mode == "auto" and intent.exclusive != "auto" and not explicit
            and selected_scope in {"auto", intent.exclusive}):
        other = next(iter(intent.blocked))
        return RequestConstraints("advisory", intent.exclusive, (), audit, pure,
                                  f"{other}_denied_{intent.exclusive}_optional")
    if mode == "auto" and selected_scope in {"local", "web", "fusion"}:
        chosen = {"local", "web"} if selected_scope == "fusion" else {selected_scope}
        mode = _route_for(chosen - intent.blocked)
    if mode == "auto" and explicit and not intent.either:
        mode = intent.exclusive if intent.exclusive != "auto" else "web"
    if mode != "auto":
        groups = {"local": (LOCAL_GROUP,), "web": (WEB_GROUP,), "fusion": (WEB_GROUP, LOCAL_GROUP)}
        return RequestConstraints("required", mode, groups[mode], audit, pure, f"explicit_{mode}")
    if audit and not explicit:
        return RequestConstraints("advisory", "none", (), True, pure,
                                  "data_audit_only")
    return RequestConstraints("advisory", "auto", (), audit,
                              pure, "automatic_advice")


_EXTERNAL_EVIDENCE = re.compile(
    r'(?:结合|参照|参考|根据|依据|综合|对照|引用|查找|寻找|对比).{0,14}(?:论文|文献|公开(?:资料|来源)|外部(?:资料|来源)|网页)'
    r'|\b(?:use|find|cite|based on|with)\b.{0,25}\b(?:papers?|studies|external sources|literature)\b', re.I)


def attachment_processing_only(query: str, *, has_source: bool, browse_requested: bool = False) -> bool:
    if not has_source or browse_requested:
        return False
    from src.graph.agent_operations import (_explicit_data_audit, _explicit_reference_check,
                                            _explicit_visualization_request)
    from src.graph.intent_text import intent_with_negation, mask_literal_payload

    query = mask_literal_payload(query)
    if not (_explicit_visualization_request(query) or _explicit_data_audit(query)):
        return False
    constraints = parse_request_constraints(query, source_scope(query))
    return (constraints.source_policy != "required" and not _explicit_reference_check(query)
            and not intent_with_negation(query, _EXTERNAL_EVIDENCE))


@dataclass(frozen=True)
class RetrievalDecision:
    mode: str
    required_tool_groups: tuple[frozenset[str], ...] = ()
    history_turn_seqs: tuple[int, ...] = ()
    reason_code: str = "default"
    policy: str = "advisory"

    def as_diagnostic(self) -> dict:
        return {
            "mode": self.mode,
            "required_tool_groups": [sorted(group) for group in self.required_tool_groups],
            "history_turn_seqs": list(self.history_turn_seqs),
            "reason_code": self.reason_code,
            "policy": self.policy,
        }


def required_route_groups(decision: RetrievalDecision) -> tuple[frozenset[str], ...]:
    """Only an explicit user source request can produce required route groups."""
    if decision.policy == "required":
        return tuple(decision.required_tool_groups)
    return ()


def retrieval_tools_allowed(
    constraints: RequestConstraints,
    decision: RetrievalDecision,
) -> bool:
    """Whether request-scoped retrieval tools may be advertised.

    A pure data-audit request has no retrieval dependency. Explicit source
    selection is represented by a different reason code, so it continues to
    expose only the tools allowed by that selection.
    """
    if decision.policy == "deny":
        return False
    if constraints.reason_code == "data_audit_only":
        return False
    return True


def decide_retrieval(
    query: str,
    scope: str = "auto",
    *,
    has_history_evidence: bool = False,
    recent_turn_seqs: list[int] | tuple[int, ...] = (),
) -> RetrievalDecision:
    """Describe user scope and reusable evidence without selecting tool calls."""
    q = str(query or "").strip()
    scope = str(scope or "auto").strip().casefold()
    if scope not in {"auto", "local", "web", "fusion"}:
        scope = "auto"
    recent = tuple(sorted({
        int(turn) for turn in (recent_turn_seqs or ()) if int(turn) > 0
    }))

    def decision(mode, groups=(), history=(), reason="default", policy="advisory"):
        return RetrievalDecision(mode, tuple(groups), history, reason, policy)

    constraints = parse_request_constraints(query, scope)
    if constraints.source_policy == "deny":
        return decision("none", (), (), constraints.reason_code, "deny")
    if constraints.source_policy == "required":
        return decision(
            constraints.requested_mode,
            constraints.required_tool_groups,
            (),
            constraints.reason_code,
            "required",
        )
    if constraints.reason_code.endswith("_optional"):
        return decision(constraints.requested_mode, reason=constraints.reason_code)
    policy = constraints.source_policy
    if not q or _SOCIAL.match(q):
        return decision("none", (), (), "no_retrieval_or_social", policy)
    if constraints.data_audit_requested and constraints.reason_code == "data_audit_only":
        return decision("none", (), (), "data_audit_only", policy)
    if _MEMORY_COMMAND.search(q):
        return decision("none", (), (), "memory_write_only", policy)

    new_evidence = bool(_NEW_RETRIEVAL_CUE.search(q))
    if has_history_evidence and recent and _REUSE_CUE.search(q) and not new_evidence:
        return decision("reuse", (), recent, "recent_evidence_reuse", policy)

    if _PURE_TRANSFORM.search(q):
        return decision("none", (), (), "transformation_from_context", policy)

    if _LATEST.search(q) or new_evidence:
        return decision("web", reason="time_sensitive_web", policy=policy)
    return decision("auto", reason="automatic_advice", policy=policy)
