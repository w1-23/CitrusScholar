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
NATIVE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete"})
NATIVE_FILE_TOOLS = NATIVE_READ_TOOLS | NATIVE_WRITE_TOOLS
# Native delegation: present only when an explicit, metered subagent is built.
NATIVE_TASK_TOOLS = frozenset({"task"})
# Native shell: present only through an isolated SandboxBackendProtocol backend.
NATIVE_EXECUTE_TOOLS = frozenset({"execute"})
# Registered here; `execute` runs only through the isolated Code Worker backend.
FILE_MIDDLEWARE_TOOLS = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute")


@cache
def configure_profile():
    import deepagents
    if deepagents.__version__ != SDK_VERSION:
        raise RuntimeError("unsupported_deepagents_version")
    register_harness_profile(GRAPH_PROFILE_KEY, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        # `execute` is omitted by the filesystem tool allowlist unless a sandbox
        # backend is supplied, and `task` only exists when this graph is given
        # an explicit, metered subagent. No capability is removed by name.
        excluded_tools=frozenset(),
        # A missing tool observation needs reconciliation, not an invented
        # error. Stock summaries are neither metered nor evidence authority.
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
                           skill_root=None, system_prompt=None, capability_policy=None, context_schema=None,
                           turn_skills=None, workspace_scope=None, identity=None,
                           task_subagent=True, model_budget=None, execute_ready=True, summary_enabled=True,
                           workspace_read_only=False):
    from deepagents.backends import CompositeBackend, StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
    from langchain.agents.middleware import TodoListMiddleware, ToolErrorMiddleware
    from langchain.agents.middleware.todo import WRITE_TODOS_TOOL_DESCRIPTION
    from src.core.public_progress import PUBLIC_TODO_INSTRUCTIONS

    configure_profile()
    if skill_root is None:
        skill_backend = EmptySkillBackend(None)
    else:
        skill_backend = ScopedSkillBackend(skill_root, capability_policy, turn_skills)
    if workspace_scope is None:
        # Direct callers without an authenticated run keep the previous
        # read-only state backend; only an admitted run gets a real workspace.
        default_backend = StateBackend()
        artifacts_root = "/"
        permissions = [FilesystemPermission(
            operations=["write", "edit", "delete"], paths=["/**"], mode="deny")]
    else:
        from .sandbox_backend import CitrusSandboxBackend

        # The sandbox adapter subclasses SandboxBackendProtocol, so the SDK
        # derives the native `execute` tool. It delegates file operations to the
        # same Workspace storage: one view, no second store.
        default_backend = CitrusSandboxBackend(*workspace_scope)
        # Offloaded tool results land in the run's writable staging area, not a
        # throwaway in-memory state backend.
        artifacts_root = "/workspace"
        # SDK 0.7.38 rejects tool-level path permissions on an execution-capable
        # backend unless every rule is scoped to a route. /input, /data, /skills
        # and /01_src are therefore refused by the backend itself (and /skills by
        # its read-only route); the sandbox is the real execution boundary.
        permissions = [FilesystemPermission(
            operations=["write", "edit", "delete"], paths=["/skills/**"], mode="deny")]
    backend = CompositeBackend(default=default_backend,
                               routes={"/skills/": skill_backend}, artifacts_root=artifacts_root)

    def read_error(error, request):
        if isinstance(error, PermissionError):
            return "当前路径无读取权限。请使用本轮获授权的文件或技能资源。"
        if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            return "路径与读取方式不匹配。请检查授权目录中实际存在的资源。"
        return None

    # Native tool-result offload is restored now that the backend persists:
    # oversized results are written under the run's writable artifacts root
    # (/workspace/large_tool_results) and the message keeps a readable ref.
    # Human-message eviction stays off: the original user instruction must never
    # be silently replaced, only summarized meaningfully.
    native_tools = tuple(name for name in FILE_MIDDLEWARE_TOOLS
                         if (execute_ready or name != "execute")
                         and (not workspace_read_only or name in NATIVE_READ_TOOLS))
    filesystem = FilesystemMiddleware(backend=backend, permissions=permissions,
        tools=list(native_tools),
        human_message_token_limit_before_evict=None, tool_token_limit_before_evict=20000)
    read_errors = ToolErrorMiddleware(on_error=read_error, tools=sorted(NATIVE_FILE_TOOLS))
    # SDK 0.7.38 selects a profile from the configured model identifier and
    # has no per-call profile argument. Scope this alias to graph construction
    # and restore the real provider model at the final model middleware hook.
    profiled_model = model.model_copy(update={"model_name": GRAPH_PROFILE_KEY})
    todos = TodoListMiddleware(tool_description=f"{WRITE_TODOS_TOOL_DESCRIPTION}\n\n{PUBLIC_TODO_INSTRUCTIONS}")
    subagents = []
    if workspace_scope is not None and identity is not None and task_subagent:
        # Explicit, restricted, metered: no implicit general-purpose profile.
        from .subagent_runtime import general_purpose_subagent

        subagents = [general_purpose_subagent(model=model, identity=identity, backend=backend,
                                              permissions=permissions,
                                              file_tools=FILE_MIDDLEWARE_TOOLS, budget=model_budget)]
    context_middleware = []
    if summary_enabled and workspace_scope is not None and identity is not None and model_budget is not None:
        from .context_compaction import MeteredContextCompaction

        context_middleware.append(MeteredContextCompaction(
            model, identity=identity, budget=model_budget, backend=backend,
            allowed=capability_policy.allows if capability_policy is not None else None))
    return create_deep_agent(model=profiled_model, tools=tools,
        middleware=[filesystem, todos, *context_middleware, *middleware, read_errors], system_prompt=system_prompt,
        subagents=subagents, skills=["/skills/"] if skill_root is not None else [], backend=backend,
        permissions=permissions, checkpointer=saver, state_schema=state_schema,
         context_schema=context_schema, name="citrus_framework_loop")


def _tool_pairs(messages, identity, *, active=False):
    """Find complete native batches on one side of the authenticated user."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    def bound(message, origin, turn, run):
        meta = message.additional_kwargs
        return (meta.get("context_trusted") is True and meta.get("context_origin") == origin
                and meta.get("session_id") == identity.session_id
                and meta.get("context_turn_id") == turn and meta.get("run_id") == run)

    current = next((index for index, message in enumerate(messages)
                    if isinstance(message, HumanMessage)
                    and bound(message, "user", identity.user_turn_id, identity.run_id)), None)
    if current is None:
        return
    start, stop = (current + 1, len(messages)) if active else (0, current)
    for index in range(start, stop):
        assistant = messages[index]
        if not isinstance(assistant, AIMessage) or not assistant.tool_calls or assistant.invalid_tool_calls:
            continue
        meta = assistant.additional_kwargs
        turn, run = meta.get("context_turn_id"), meta.get("run_id")
        if not turn or not run or not bound(assistant, "assistant", turn, run):
            continue
        if active:
            if turn != identity.user_turn_id or run != identity.run_id:
                continue
        elif turn == identity.user_turn_id or run == identity.run_id:
            continue
        end = index + 1 + len(assistant.tool_calls)
        if end > stop or (end < len(messages) and isinstance(messages[end], ToolMessage)):
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


async def _clear_proven_tool_pair(model, assistant, observation, placeholder):
    """Use stock LangChain to replace just the result content on a send copy."""
    from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware, ModelRequest

    async def edited_messages(request):
        return request.messages

    middleware = ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=0, keep=0, placeholder=placeholder)])
    changed = await middleware.awrap_model_call(ModelRequest(model=model,
        messages=[assistant, observation]), edited_messages)
    return changed[1] if changed[1].content == placeholder else observation


async def _omit_repeated_readback_pages(model, view, identity, pairs, receipt_for, offered, count_tokens, tokens):
    """Remove an older raw page only when this send carries an exact current copy."""
    import asyncio
    import json
    from src.core.model_context import exact_readback_page

    # Comparing the entire wire body also compares ordered selectors, source
    # digest, cursor, outcome and all exact returned bytes. Do not compare IDs,
    # lengths, or selected text alone. The source must already be re-offered.
    current = {}
    for _, ordinal in _tool_pairs(view, identity, active=True):
        message = view[ordinal]
        if message.name == "read_tool_result" and message.status == "success" and isinstance(message.content, str):
            current.setdefault(message.content, []).append(message)
    if not current:
        return view
    candidates = [(assistant, ordinal) for assistant, ordinal in pairs
                  if view[ordinal].name == "read_tool_result" and isinstance(view[ordinal].content, str)
                  and view[ordinal].content in current]
    if not candidates:
        return view

    def verify():
        verified = {}
        edits = []
        for assistant, ordinal in candidates:
            old = view[ordinal]
            if (page := exact_readback_page(old, offered)) is None:
                continue
            if old.content not in verified:
                verified[old.content] = next((message for message in current[old.content]
                    if receipt_for(message, current_readback=True) is not None), None)
            newer = verified[old.content]
            if newer is None or receipt_for(old) is None:
                continue
            source, bounds, fields = page
            placeholder = json.dumps({"delivery": "older_verified_readback_page_omitted",
                "duplicate_of_tool_call_id": newer.tool_call_id, "tool_result_ref": source,
                "page": bounds, "selected_fields": fields,
                "notice": "The identical current readback is in this request. The older page is retained."},
                ensure_ascii=False, separators=(",", ":"))
            if len(placeholder.encode("utf-8")) <= 1024:
                edits.append((assistant, ordinal, placeholder))
        return edits

    edits = await asyncio.to_thread(verify)
    if not edits:
        return view
    projected = list(view)
    for assistant, ordinal, placeholder in edits:
        projected[ordinal] = await _clear_proven_tool_pair(model, view[assistant], view[ordinal], placeholder)
    return projected if count_tokens(projected) < tokens else view


async def prepare_context_for_send(model, bound, messages, identity, receipt_for, *, edit):
    """Use native send-only edits and re-offer visible, authenticated receipts.

    ``bound`` includes the reader schema. A view that doesn't fit with that
    schema is not adopted. The graph and durable result rows remain untouched.
    """
    import asyncio
    import json
    from src.core.model_context import stored_history_offer
    from src.core.request_context import model_request, text_tokens
    from src.core.task_events import model_input_limit

    def count_tokens(view):
        return model_request(bound, view)[1]["estimated_input_tokens"]

    _, estimate = model_request(bound, messages)
    limit = model_input_limit(estimate["output_tokens"])
    tokens = estimate["estimated_input_tokens"]
    pairs = list(_tool_pairs(messages, identity))
    if not pairs:
        return messages, ()
    inline = []
    visible = []
    for assistant, ordinal in pairs:
        message = messages[ordinal]
        if stored_history_offer(message) is not None:
            visible.append(ordinal)
        elif edit and tokens > limit and isinstance(message.content, str) and len(message.content) >= 512:
            inline.append((assistant, ordinal))

    if tokens > limit:
        editable = {ordinal for _, ordinal in inline}
        if not editable or count_tokens([m for i, m in enumerate(messages) if i not in editable]) > limit:
            return messages, ()
    if not inline and not visible:
        return messages, ()

    def verify():
        offered = {ref["tool_call_id"]: ref["full_sha256"] for ordinal in visible
                   if (ref := receipt_for(messages[ordinal], existing_delivery=True)) is not None}
        candidates = []
        for assistant, ordinal in inline:
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
                candidates.append((assistant, ordinal, placeholder, reference))
        return offered, candidates

    offered, candidates = await asyncio.to_thread(verify)
    view = list(messages)

    for assistant, ordinal, placeholder, reference in candidates:
        if tokens <= limit:
            break
        original = view[ordinal]
        view[ordinal] = await _clear_proven_tool_pair(model, view[assistant], original, placeholder)
        if view[ordinal] is original:
            continue
        edited_tokens = count_tokens(view)
        if edited_tokens >= tokens:
            view[ordinal] = original
            continue
        tokens = edited_tokens
        offered[reference["tool_call_id"]] = reference["full_sha256"]
    if not offered or tokens > limit:
        return messages, ()
    if edit:
        view = await _omit_repeated_readback_pages(model, view, identity, pairs, receipt_for, offered,
                                                  count_tokens, tokens)
    return view, tuple(offered)
