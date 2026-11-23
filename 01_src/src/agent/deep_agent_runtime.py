"""Pinned Deep Agents factory with host-owned execution and file boundaries.

The SDK owns the graph. HostTools owns every actual tool invocation, and the
existing working-context projection owns compression. No native filesystem,
subagent, synthetic tool repair or second summarizer is installed in the graph.
"""
from __future__ import annotations

from functools import cache

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile,
                        create_deep_agent, register_harness_profile)
from deepagents.backends.protocol import BackendProtocol
from langchain.agents.middleware import AgentMiddleware

SDK_VERSION = "0.7.38"
EXECUTOR_ID = "citrus-deepagents-0.7.38-host-v1"
MODEL_PROVIDER = "citrus_metered"


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


class HostFilesystemSlot(AgentMiddleware):
    """Replace the SDK filesystem slot with the registered host file tools.

    Same-name replacement is supported by the pinned SDK. Stock filesystem
    permission/offload hooks are intentionally absent; the host enforces access.
    """
    tools = ()

    @property
    def name(self):
        return "FilesystemMiddleware"

    def bind_backend(self, backend):
        self.backend = backend

    async def awrap_model_call(self, request, handler):
        # The factory authors an empty system message by default. Existing host
        # controls already live in history; do not add an extra empty control.
        if request.system_message is not None and not request.system_message.content:
            request = request.override(system_message=None)
        return await handler(request)


@cache
def configure_host_profile():
    import deepagents
    if deepagents.__version__ != SDK_VERSION:
        raise RuntimeError("unsupported_deepagents_version")
    # These private SDK hooks are deliberately version-pinned. Using the exact
    # assembled caching names also handles optional provider integrations without
    # unmatched profile exclusions or global patches to the SDK.
    from deepagents.middleware._parent_notifier import _ParentNotifierMiddleware
    from deepagents.middleware._prompt_caching import append_prompt_caching_middleware
    caching = []
    append_prompt_caching_middleware(caching)
    excluded = {"SummarizationMiddleware", "PatchToolCallsMiddleware",
                "UnsupportedContentMiddleware", "ModelRefusalMiddleware",
                _ParentNotifierMiddleware, *(item.name for item in caching)}
    register_harness_profile(MODEL_PROVIDER, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_middleware=frozenset(excluded)))


def create_host_agent(model, tools, policy, saver, state_schema):
    configure_host_profile()
    return create_deep_agent(model=model, tools=tools,
        middleware=[HostFilesystemSlot(), policy],
        subagents=[], skills=None, memory=None,
        backend=HostOnlyBackend(model.identity),
        checkpointer=saver, state_schema=state_schema,
        name="citrus_main_loop")


NATIVE_EXECUTOR_ID = "citrus-deepagents-0.7.38-native-v1"
NATIVE_MODEL_PROVIDER = "citrus_metered_native"
NATIVE_UNCOMPRESSED_PROVIDER = "citrus_metered_native_uncompressed"
NATIVE_READ_TOOLS = frozenset({"ls", "glob", "grep", "read_file"})


@cache
def configure_native_profile():
    import deepagents
    if deepagents.__version__ != SDK_VERSION:
        raise RuntimeError("unsupported_deepagents_version")
    for provider, summary_enabled in (
        (NATIVE_MODEL_PROVIDER, True), (NATIVE_UNCOMPRESSED_PROVIDER, False),
    ):
        exclusions = {"PatchToolCallsMiddleware"}
        if not summary_enabled:
            exclusions.add("SummarizationMiddleware")
        register_harness_profile(provider, HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=frozenset({"write_file", "edit_file", "delete", "execute"}),
            excluded_middleware=frozenset(exclusions)))


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
    def __init__(self, root, policy):
        from deepagents.backends import FilesystemBackend
        from pathlib import Path

        self.policy = policy
        self.routes = {}
        for entrypoint in sorted(Path(root).glob("*/SKILL.md")):
            directory = entrypoint.parent
            if not directory.is_symlink() and not entrypoint.is_symlink():
                self.routes[directory.name] = FilesystemBackend(root_dir=directory, virtual_mode=True)

    def _backend(self):
        from deepagents.backends import CompositeBackend
        return CompositeBackend(default=EmptySkillBackend(None), routes={
            f"/{name}/": backend for name, backend in self.routes.items()
            if self.policy.allows("skill:" + name)})

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


def create_native_agent(model, tools, policy, saver, state_schema, *,
                        skill_root, system_prompt, summary_model,
                        summary_trigger=48000, summary_keep=32000, capability_policy=None):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.middleware.filesystem import FilesystemPermission
    from deepagents.middleware.summarization import SummarizationMiddleware

    configure_native_profile()
    skill_backend = (ScopedSkillBackend(skill_root, capability_policy) if capability_policy is not None
                     else FilesystemBackend(root_dir=skill_root, virtual_mode=True))
    backend = CompositeBackend(default=StateBackend(), routes={"/skills/": skill_backend})
    permissions = [FilesystemPermission(
        operations=["write", "edit", "delete"], paths=["/**"], mode="deny")]
    middleware = []
    if summary_model is not None:
        middleware.append(SummarizationMiddleware(summary_model, backend=backend,
            trigger=("tokens", summary_trigger), keep=("tokens", summary_keep),
            trim_tokens_to_summarize=4000))
    from langchain.agents.middleware import ToolErrorMiddleware

    def read_error(error, request):
        if isinstance(error, PermissionError):
            return "当前路径无读取权限。请使用本轮获授权的文件或技能资源。"
        if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            return "路径与读取方式不匹配。请检查授权目录中实际存在的资源。"
        return None

    read_errors = ToolErrorMiddleware(on_error=read_error, tools=sorted(NATIVE_READ_TOOLS))
    return create_deep_agent(model=model, tools=tools,
        middleware=[*middleware, policy, read_errors], system_prompt=system_prompt,
        subagents=[], skills=["/skills/"], backend=backend, permissions=permissions,
        checkpointer=saver, state_schema=state_schema, name="citrus_native_loop")
