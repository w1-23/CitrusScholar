"""The single public Deep Agents graph and its read-only skill filesystem."""
from __future__ import annotations

from functools import cache

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile,
                        create_deep_agent, register_harness_profile)
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

SDK_VERSION = "0.7.38"
EXECUTOR_ID = "citrus-deepagents-0.7.38-framework-v2"
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


class InventorySkillBackend(FilesystemBackend):
    """Expose the SDK filesystem operations over an installed release manifest."""

    def __init__(self, root, entries):
        super().__init__(root_dir=root, virtual_mode=True)
        self.skill_root = root
        self.entries = entries
        self.directories = {prefix for name in entries for prefix in self._parents(name)}

    @staticmethod
    def _parents(name):
        from pathlib import PurePosixPath
        return (str(part) for part in PurePosixPath(name).parents if str(part) != ".")

    def _verified(self, path):
        import hashlib
        from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES
        name = path.removeprefix("/")
        expected = self.entries.get(name)
        if expected is None:
            raise PermissionError("skill_resource_unavailable")
        try:
            raw = _bytes(_path(self.skill_root, name), _MAX_RESOURCE_BYTES)
        except (OSError, ValueError) as exc:
            raise PermissionError("skill_resource_unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            raise PermissionError("skill_changed_during_read")
        return raw

    def download_files(self, paths):
        from deepagents.backends.protocol import FileDownloadResponse
        results = []
        for path in paths:
            try:
                results.append(FileDownloadResponse(path, content=self._verified(path)))
            except PermissionError:
                results.append(FileDownloadResponse(path, error="permission_denied"))
        return results

    def read(self, file_path, offset=0, limit=2000):
        from deepagents.backends.protocol import ReadResult
        try:
            self._verified(file_path)
        except PermissionError:
            return ReadResult(error="skill_resource_unavailable")
        return super().read(file_path, offset, limit)

    def _listed(self, item):
        name = item["path"].strip("/")
        return name in (self.directories if item.get("is_dir") else self.entries)

    def ls(self, path):
        from dataclasses import replace
        result = super().ls(path)
        return replace(result, entries=[item for item in result.entries or () if self._listed(item)])

    def glob(self, pattern, path=None):
        from dataclasses import replace
        result = super().glob(pattern, path)
        return replace(result, matches=[item for item in result.matches or () if self._listed(item)])

    def grep(self, pattern, path=None, glob=None, *, max_count=None, context_lines=0):
        from deepagents.backends.protocol import GrepResult
        base = (path or "/").strip("/")
        matches = []
        for name in self.entries:
            if base and name != base and not name.startswith(base + "/"):
                continue
            try:
                self._verified(name)
            except PermissionError:
                continue
            limit = None if max_count is None else max_count - len(matches)
            if limit == 0:
                return GrepResult(matches=matches, truncated=True)
            result = super().grep(pattern, "/" + name, glob, max_count=limit, context_lines=context_lines)
            matches.extend(result.matches or ())
            if result.truncated or result.error:
                return GrepResult(error=result.error, matches=matches, truncated=result.truncated)
        return GrepResult(matches=matches)


class ScopedSkillBackend(HostOnlyBackend):
    def __init__(self, root, policy, turn_skills=None):
        import json
        from pathlib import Path
        from .research_skill_catalog import _bytes, _path, _MAX_RESOURCE_BYTES

        root = Path(root)
        inventory = json.loads(_bytes(_path(root.parent, "inventory.json"), _MAX_RESOURCE_BYTES))
        if not isinstance(inventory, dict):
            raise ValueError("invalid_skill_inventory")

        self.policy = policy
        self.routes = {}
        self.required_capability = {}

        def mount(directory, capability):
            prefix = "skills/" + directory.name + "/"
            entries = {key.removeprefix(prefix): digest for key, digest in inventory.items()
                       if key.startswith(prefix)}
            if entries:
                self.routes[directory.name] = InventorySkillBackend(directory, entries)
                self.required_capability[directory.name] = capability

        for entrypoint in sorted(root.glob("*/SKILL.md")):
            directory = entrypoint.parent
            if (not directory.is_symlink() and not entrypoint.is_symlink()
                    and f"skills/{directory.name}/SKILL.md" in inventory):
                mount(directory, "skill:" + directory.name)
        # Upstream text refers to this sibling, which has no discoverable entry.
        shared = root / "nature-shared"
        if ("nature-figure" in self.routes and shared.is_dir() and not shared.is_symlink()
                and not (shared / "SKILL.md").exists()):
            mount(shared, "skill:nature-figure")

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
