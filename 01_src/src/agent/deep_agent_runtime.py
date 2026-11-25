"""The single public Deep Agents graph and its read-only skill filesystem."""
from __future__ import annotations

from functools import cache

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile,
                        create_deep_agent, register_harness_profile)
from deepagents.backends.protocol import BackendProtocol

SDK_VERSION = "0.7.38"
EXECUTOR_ID = "citrus-deepagents-0.7.38-framework-v2"
LEGACY_HOST_EXECUTOR_ID = "citrus-deepagents-0.7.38-host-v1"
LEGACY_NATIVE_EXECUTOR_ID = "citrus-deepagents-0.7.38-native-v1"
GRAPH_PROFILE_KEY = "citrus_metered_framework:main"
NATIVE_READ_TOOLS = frozenset({"ls", "glob", "grep", "read_file"})


@cache
def configure_profile():
    import deepagents
    if deepagents.__version__ != SDK_VERSION:
        raise RuntimeError("unsupported_deepagents_version")
    register_harness_profile(GRAPH_PROFILE_KEY, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=frozenset({"write_file", "edit_file", "delete", "execute", "task"}),
        excluded_middleware=frozenset({"PatchToolCallsMiddleware", "SummarizationMiddleware"})))


class HostOnlyBackend(BackendProtocol):
    """No native SDK I/O. Typed workspace tools carry scope, CAS and receipts.

    Deliberately not a SandboxBackendProtocol. The typed file tools provide the
    projected mounts; native writes cannot infer a current CAS or bypass them.
    """
    def __init__(self, identity):
        self.identity = identity

    def _deny(self, *args, **kwargs):
        raise PermissionError("host_scoped_file_tool_required")

    ls = read = grep = glob = write = edit = delete = upload_files = download_files = _deny


class EmptySkillBackend(HostOnlyBackend):
    def ls(self, path):
        from deepagents.backends.protocol import LsResult
        return LsResult(entries=[])

    def read(self, file_path, offset=0, limit=2000):
        from deepagents.backends.protocol import ReadResult
        return ReadResult(error="Skill resource unavailable")

    def grep(self, *args, **kwargs):
        from deepagents.backends.protocol import GrepResult
        return GrepResult(matches=[])

    def glob(self, *args, **kwargs):
        from deepagents.backends.protocol import GlobResult
        return GlobResult(matches=[])

    def download_files(self, paths):
        from deepagents.backends.protocol import FileDownloadResponse
        return [FileDownloadResponse(path, error="file_not_found") for path in paths]


class ScopedSkillBackend(HostOnlyBackend):
    def __init__(self, root, policy, turn_skills=None):
        from deepagents.backends import FilesystemBackend
        from pathlib import Path
        from .installed_skills import RQA_ID

        root = Path(root)
        rqa_selected = (turn_skills is not None and turn_skills.release == root.parent
                        and any(row["id"] == RQA_ID for row in turn_skills.rows()))

        self.policy = policy
        self.routes = {}
        self.required_capability = {}
        for entrypoint in sorted(Path(root).glob("*/SKILL.md")):
            directory = entrypoint.parent
            if (not directory.is_symlink() and not entrypoint.is_symlink()
                    and (directory.name != RQA_ID or rqa_selected)):
                self.routes[directory.name] = FilesystemBackend(root_dir=directory, virtual_mode=True)
                self.required_capability[directory.name] = "skill:" + directory.name
        # Upstream text is preserved byte for byte and refers to this sibling.
        # The sibling provides files only, never a discoverable Skill entry.
        shared = Path(root) / "nature-shared"
        if ("nature-figure" in self.routes and shared.is_dir() and not shared.is_symlink()
                and not (shared / "SKILL.md").exists()):
            self.routes["nature-shared"] = FilesystemBackend(root_dir=shared, virtual_mode=True)
            self.required_capability["nature-shared"] = "skill:nature-figure"

    def _backend(self):
        from deepagents.backends import CompositeBackend
        return CompositeBackend(default=EmptySkillBackend(None), routes={
            f"/{name}/": backend for name, backend in self.routes.items()
            if self.policy is None or self.policy.allows(self.required_capability[name])})

    def ls(self, path):
        return self._backend().ls(path)

    def read(self, file_path, offset=0, limit=2000):
        return self._backend().read(file_path, offset, limit)

    def grep(self, *args, **kwargs):
        return self._backend().grep(*args, **kwargs)

    def glob(self, *args, **kwargs):
        return self._backend().glob(*args, **kwargs)

    def download_files(self, paths):
        return self._backend().download_files(paths)


def create_framework_agent(model, tools, middleware, saver, state_schema, *,
                           skill_root=None, system_prompt=None, capability_policy=None, context_schema=None, turn_skills=None):
    from deepagents.backends import CompositeBackend, StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
    from langchain.agents.middleware import TodoListMiddleware, ToolErrorMiddleware

    configure_profile()
    if skill_root is None:
        skill_backend = EmptySkillBackend(None)
    else:
        skill_backend = ScopedSkillBackend(skill_root, capability_policy, turn_skills)
    backend = CompositeBackend(default=StateBackend(), routes={"/skills/": skill_backend})
    permissions = [FilesystemPermission(
        operations=["write", "edit", "delete"], paths=["/**"], mode="deny")]

    def read_error(error, request):
        if isinstance(error, PermissionError):
            return "当前路径无读取权限。请使用本轮获授权的文件或技能资源。"
        if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            return "路径与读取方式不匹配。请检查授权目录中实际存在的资源。"
        return None

    # Keep SDK filesystem reads, but leave message admission to the shared hard
    # limit. Default eviction otherwise replaces even the current user/tools
    # with filesystem stubs before the provider-bound budget sees them.
    filesystem = FilesystemMiddleware(backend=backend, permissions=permissions,
        human_message_token_limit_before_evict=None, tool_token_limit_before_evict=None)
    read_errors = ToolErrorMiddleware(on_error=read_error, tools=sorted(NATIVE_READ_TOOLS))
    # SDK 0.7.38 selects a profile from the configured model identifier and
    # has no per-call profile argument. Scope this alias to graph construction
    # and restore the real provider model at the final model middleware hook.
    profiled_model = model.model_copy(update={"model_name": GRAPH_PROFILE_KEY})
    return create_deep_agent(model=profiled_model, tools=tools,
        middleware=[filesystem, TodoListMiddleware(), *middleware, read_errors], system_prompt=system_prompt,
        subagents=[], skills=["/skills/"] if skill_root is not None else [], backend=backend,
        permissions=permissions, checkpointer=saver, state_schema=state_schema,
         context_schema=context_schema, name="citrus_framework_loop")


def _earlier_tool_pairs(messages, identity):
    """Find native, complete tool blocks before the authenticated current user."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    def bound(message, origin, turn, run):
        meta = message.additional_kwargs
        return (meta.get("context_trusted") is True and meta.get("context_origin") == origin
                and meta.get("session_id") == identity.session_id
                and meta.get("context_turn_id") == turn and meta.get("run_id") == run)

    current = next((index for index, message in enumerate(messages)
                    if isinstance(message, HumanMessage)
                    and bound(message, "user", identity.user_turn_id, identity.run_id)), 0)
    for index in range(current):
        assistant = messages[index]
        if not isinstance(assistant, AIMessage) or not assistant.tool_calls or assistant.invalid_tool_calls:
            continue
        meta = assistant.additional_kwargs
        turn, run = meta.get("context_turn_id"), meta.get("run_id")
        if (not turn or turn == identity.user_turn_id or not run or run == identity.run_id
                or not bound(assistant, "assistant", turn, run)):
            continue
        end = index + 1 + len(assistant.tool_calls)
        if end > current or isinstance(messages[end], ToolMessage):
            continue
        calls = {call.get("id"): call["name"] for call in assistant.tool_calls}
        observations = messages[index + 1:end]
        if (None in calls or len(calls) != len(observations)
                or not all(isinstance(message, ToolMessage) and bound(message, "tool", turn, run)
                    and calls.get(message.tool_call_id) == message.name
                    for message in observations)
                or len({message.tool_call_id for message in observations}) != len(calls)):
            continue
        yield from ((index, ordinal) for ordinal in range(index + 1, end))


async def edit_context_for_send(model, bound, messages, identity, receipt_for):
    """Apply stock LangChain clearing to individually proven older observations."""
    import asyncio
    import json
    from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware, ModelRequest
    from src.core.request_context import model_request, text_tokens
    from src.core.task_events import model_input_limit

    def count_tokens(view):
        return model_request(bound, view)[1]["estimated_input_tokens"]

    _, estimate = model_request(bound, messages)
    limit = model_input_limit(estimate["output_tokens"])
    if estimate["estimated_input_tokens"] <= limit:
        return messages, ()

    pairs = [(assistant, ordinal) for assistant, ordinal in _earlier_tool_pairs(messages, identity)
             if isinstance(messages[ordinal].content, str) and len(messages[ordinal].content) >= 512]
    possibly_editable = {ordinal for _, ordinal in pairs}
    # If even this hypothetical lower bound is too large, verify/admit the raw
    # request normally. The hypothetical view is never sent to the provider.
    if not pairs or count_tokens([m for i, m in enumerate(messages) if i not in possibly_editable]) > limit:
        return messages, ()

    def verified_candidates():
        candidates = []
        for assistant, ordinal in pairs:
            message = messages[ordinal]
            reference = receipt_for(message)
            if reference is None:
                continue
            placeholder = json.dumps({"delivery": "older_result_omitted_from_send_view",
                "tool_result_ref": reference,
                "read_with": {"tool": "read_tool_result", "tool_call_id": reference["tool_call_id"]},
                "notice": "Read only needed evidence. This is a historical result, not a new execution."},
                ensure_ascii=False, separators=(",", ":"))
            if text_tokens(placeholder) < text_tokens(message.content):
                candidates.append((assistant, ordinal, placeholder, reference["tool_call_id"]))
        return candidates

    candidates = await asyncio.to_thread(verified_candidates)
    if not candidates:
        return messages, ()

    offered = []

    class ProvenToolUsesEdit:
        def apply(self, view, *, count_tokens):
            for assistant, ordinal, placeholder, key in candidates:
                if count_tokens(view) <= limit:
                    break
                pair = [view[assistant], view[ordinal]]
                # The native edit must never see a current/unknown pair or change
                # the assistant call. The entire send, not this pair, is budgeted.
                ClearToolUsesEdit(trigger=0, keep=0, placeholder=placeholder).apply(
                    pair, count_tokens=lambda _: 1)
                if pair[1] is not view[ordinal]:
                    view[ordinal] = pair[1]
                    offered.append(key)

    async def send_view(request):
        return request.messages

    middleware = ContextEditingMiddleware(edits=[ProvenToolUsesEdit()], token_counter=count_tokens)
    view = await middleware.awrap_model_call(ModelRequest(model=model, messages=messages), send_view)
    return (view, tuple(offered)) if offered else (messages, ())
