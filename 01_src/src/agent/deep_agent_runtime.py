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
MODEL_PROVIDER = "citrus_metered_framework"
NATIVE_READ_TOOLS = frozenset({"ls", "glob", "grep", "read_file"})


@cache
def configure_profile():
    import deepagents
    if deepagents.__version__ != SDK_VERSION:
        raise RuntimeError("unsupported_deepagents_version")
    register_harness_profile(MODEL_PROVIDER, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=frozenset({"write_file", "edit_file", "delete", "execute", "task", "write_todos"}),
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
    def __init__(self, root, policy):
        from deepagents.backends import FilesystemBackend
        from pathlib import Path

        self.policy = policy
        self.routes = {}
        self.required_capability = {}
        for entrypoint in sorted(Path(root).glob("*/SKILL.md")):
            directory = entrypoint.parent
            if not directory.is_symlink() and not entrypoint.is_symlink():
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
            if self.policy.allows(self.required_capability[name])})

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
                           skill_root=None, system_prompt=None, capability_policy=None):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.middleware.filesystem import FilesystemPermission
    from langchain.agents.middleware import ToolErrorMiddleware

    configure_profile()
    if skill_root is None:
        skill_backend = EmptySkillBackend(None)
    elif capability_policy is not None:
        skill_backend = ScopedSkillBackend(skill_root, capability_policy)
    else:
        skill_backend = FilesystemBackend(root_dir=skill_root, virtual_mode=True)
    backend = CompositeBackend(default=StateBackend(), routes={"/skills/": skill_backend})
    permissions = [FilesystemPermission(
        operations=["write", "edit", "delete"], paths=["/**"], mode="deny")]

    def read_error(error, request):
        if isinstance(error, PermissionError):
            return "当前路径无读取权限。请使用本轮获授权的文件或技能资源。"
        if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            return "路径与读取方式不匹配。请检查授权目录中实际存在的资源。"
        return None

    read_errors = ToolErrorMiddleware(on_error=read_error, tools=sorted(NATIVE_READ_TOOLS))
    return create_deep_agent(model=model, tools=tools,
        middleware=[*middleware, read_errors], system_prompt=system_prompt,
        subagents=[], skills=["/skills/"] if skill_root is not None else [], backend=backend,
        permissions=permissions, checkpointer=saver, state_schema=state_schema, name="citrus_framework_loop")


def metered_maintenance_model(transport, identity):
    """Use the existing checkpoint-only accountant, never the foreground budget."""
    from typing import Any
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field
    from src.core import task_events, tracing

    class MaintenanceModel(BaseChatModel):
        underlying: Any = Field(exclude=True, repr=False)

        @property
        def _llm_type(self):
            return "citrus-metered-checkpoint"

        def _generate(self, *args, **kwargs):
            raise RuntimeError("async_maintenance_required")

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            original = tracing.get_job_id()
            tracing.set_job_id(identity.run_id)
            try:
                reply = await task_events.counted_ainvoke(self.underlying, messages,
                    stop=stop, purpose="turn_checkpoint", usage_session_id=identity.session_id)
            finally:
                tracing.set_job_id(original)
            return ChatResult(generations=[ChatGeneration(message=reply)])

    return MaintenanceModel(underlying=transport, profile=transport.profile)
