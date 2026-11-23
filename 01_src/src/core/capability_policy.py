"""Server-owned admission policy for one task and its data access boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
NETWORK_CAPABILITIES = frozenset({"web_search", "web_fetch", "browser", "bibliographic_lookup"})
SWITCHES = NETWORK_CAPABILITIES | {"external_data_network", "local_rag", "summarization"}
TOOL_CAPABILITIES = {
    "deepseek_search": ("web_search",),
    "deepseek_fetch": ("web_fetch",),
    "deepseek_fetch_paper": ("web_fetch",),
    "browse_web": ("browser",),
    "search_literature": ("bibliographic_lookup",),
    "fetch_and_audit_open_supplements": ("web_fetch", "bibliographic_lookup"),
    "local_hybrid_search": ("local_rag",),
    "inspect_candidates": ("local_rag",),
    "read_candidate": ("local_rag",),
    "read_local_evidence": ("local_rag",),
    "search_candidates": ("local_rag",),
    "search_agent_corpus": ("local_rag",),
    "prepare_document": ("local_rag",),
    "commit_document_index": ("local_rag",),
    "propose_document_ingest": ("local_rag",),
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _revocations() -> frozenset[str]:
    from src.config import settings
    configured = settings.CAPABILITY_REVOCATIONS_FILE
    if not configured:
        return frozenset()
    try:
        with Path(configured).open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError("revocations_too_large")
        value = json.loads(raw)
        if (not isinstance(value, list) or any(not isinstance(name, str) or
                (name not in SWITCHES and not name.startswith("skill:")) for name in value)):
            raise ValueError("invalid_revocations")
        return frozenset(value)
    except (OSError, ValueError):
        logger.warning("Capability revocation file unavailable; access closed")
        return SWITCHES | {"skill:*"}


@dataclass(frozen=True)
class CapabilityPolicy:
    external_data_network: bool = True
    local_rag: bool = True
    web_search: bool = True
    web_fetch: bool = True
    browser: bool = True
    bibliographic_lookup: bool = True
    summarization: bool = True
    enabled_skills: tuple[str, ...] | None = None
    experiment_group: str = ""
    revoked: tuple[str, ...] = ()

    def snapshot(self) -> dict:
        result = asdict(self)
        for key in ("enabled_skills", "revoked"):
            if result[key] is not None:
                result[key] = list(result[key])
        return result

    @classmethod
    def restore(cls, snapshot: dict):
        if type(snapshot) is not dict or set(snapshot) != {field.name for field in fields(cls)}:
            raise ValueError("capability_snapshot_invalid")
        value = dict(snapshot)
        if any(type(value[name]) is not bool for name in SWITCHES) or type(value["experiment_group"]) is not str:
            raise ValueError("capability_snapshot_invalid")
        for key in ("enabled_skills", "revoked"):
            items = value[key]
            if items is None and key == "enabled_skills":
                continue
            if type(items) is not list or any(type(item) is not str for item in items):
                raise ValueError("capability_snapshot_invalid")
            value[key] = tuple(items)
        return cls(**value)

    @property
    def history_key(self) -> str:
        history = self.snapshot()
        if not self.experiment_group:
            history["summarization"] = True
        return "" if history == CapabilityPolicy().snapshot() else digest(history)

    def allows(self, name: str, *, live: bool = True) -> bool:
        revoked = frozenset(self.revoked) | (_revocations() if live else frozenset())
        if name.startswith("skill:"):
            return ("skill:*" not in revoked and name not in revoked and
                    (self.enabled_skills is None or name[6:] in self.enabled_skills))
        needed = (name, "external_data_network") if name in NETWORK_CAPABILITIES else (name,)
        allowed = all(getattr(self, key) and key not in revoked for key in needed)
        if not allowed and live and any(getattr(self, key) and key in revoked for key in needed):
            logger.info("Capability access revoked: %s", name)
        return allowed

    def has_skill_access(self) -> bool:
        revoked = frozenset(self.revoked) | _revocations()
        return "skill:*" not in revoked and (self.enabled_skills is None or
                any("skill:" + name not in revoked for name in self.enabled_skills))

    def tool_allowed(self, name: str) -> bool:
        return all(self.allows(capability) for capability in TOOL_CAPABILITIES.get(name, ()))


def new_policy(state: dict | None = None) -> CapabilityPolicy:
    from src.config import settings
    state = state or {}
    flags = {name: getattr(settings, name.upper()) for name in SWITCHES}
    flags["summarization"] &= bool(settings.CONTEXT_BUDGET_ENABLED and state.get("context_compression", True))
    if state:
        from src.graph.retrieval_router import retrieval_denied, source_scope
        query = str(state.get("query") or "")
        scope = source_scope(query, exclusive_only=True)
        blocked = retrieval_denied(query, scope)
        flags["external_data_network"] &= scope != "local" and not blocked
        flags["local_rag"] &= scope != "web" and not blocked
    if not flags["external_data_network"]:
        flags.update({name: False for name in NETWORK_CAPABILITIES})
    skills = settings.ENABLED_SKILLS
    return CapabilityPolicy(**flags, enabled_skills=None if skills is None else tuple(sorted(set(skills))),
                            experiment_group=settings.CAPABILITY_EXPERIMENT_GROUP,
                            revoked=tuple(sorted(_revocations())))


def policy_for_state(state: dict) -> CapabilityPolicy:
    saved = state.get("capability_snapshot")
    return CapabilityPolicy.restore(saved) if saved is not None else new_policy(state)


_current_policy: ContextVar[CapabilityPolicy | None] = ContextVar("citrus_capability_policy", default=None)


@contextmanager
def bind_policy(policy: CapabilityPolicy):
    token = _current_policy.set(policy)
    try:
        yield policy
    finally:
        _current_policy.reset(token)


def current_policy() -> CapabilityPolicy:
    return _current_policy.get() or new_policy()


def allows(name: str) -> bool:
    if name in NETWORK_CAPABILITIES:
        from src.core.tracing import web_search_enabled
        if not web_search_enabled():
            return False
    return current_policy().allows(name)


def not_run(name: str) -> dict:
    return {"status": "not_run", "reason_code": name + "_disabled", "artifacts": []}


def history_rows(messages, row_ids, policy):
    """Keep whole turns from one condition, with legacy rows admitted by default."""
    key = policy.history_key
    paired = list(zip(messages, row_ids, strict=True)) if len(messages) == len(row_ids) else []
    if not paired and messages and not key:
        return messages, row_ids
    matching = [(message, row_id) for message, row_id in paired
                if message.response_metadata.get("citrus_capability_history", "") == key]
    return [message for message, _ in matching], [row_id for _, row_id in matching]
