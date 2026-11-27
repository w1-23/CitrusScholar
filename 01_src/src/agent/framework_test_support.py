"""Test-only provider fixtures that run through native Agent model nodes."""
import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.errors import NodeCancelledError
from langgraph.runtime import get_runtime
from pydantic import Field

from .deep_agent_runtime import NATIVE_EXECUTE_TOOLS, NATIVE_FILE_TOOLS, NATIVE_TASK_TOOLS
from .model import ModelDriver
from .plan_tools import NATIVE_PLAN_TOOL


class ScriptedModel(BaseChatModel):
    script: Any = Field(exclude=True)

    @property
    def _llm_type(self):
        return "scripted-fixture"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, *args, **kwargs):
        raise AssertionError("asynchronous model fixture required")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        identity = get_runtime().context.identity
        native = tuple(tool for tool in kwargs.get("tools", ())
                       if tool["function"]["name"] in NATIVE_FILE_TOOLS | NATIVE_TASK_TOOLS
                       | NATIVE_EXECUTE_TOOLS | {NATIVE_PLAN_TOOL})
        try:
            result = await self.script(tuple(messages), identity, native_schemas=native)
        except asyncio.CancelledError as error:
            if asyncio.current_task().cancelling():
                raise
            raise NodeCancelledError("model") from error
        return ChatResult(generations=[ChatGeneration(message=result)])


async def invoke_model_once(model, catalog, names, messages, identity, **options):
    """Exercise the stock model node without any executable test tools."""
    driver = ModelDriver(catalog, **options)

    class Observe(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            return await driver(request, handler, identity, allowed=names, host_tools=names)

    graph = create_agent(model=model, tools=catalog.schemas(names), middleware=[Observe()])
    result = await graph.ainvoke({"messages": list(messages)})
    return result["messages"][-1]
