import asyncio

from langchain_core.tools import tool


@tool(response_format="content_and_artifact")
def _sync_demo(value: str) -> tuple[str, dict]:
    """Return content and artifact."""
    return f"ok:{value}", {"value": value}


@tool(response_format="content_and_artifact")
async def _async_demo(value: str) -> tuple[str, dict]:
    """Return content and artifact asynchronously."""
    return f"async:{value}", {"value": value}


def _call(name: str) -> dict:
    return {"type": "tool_call", "name": name, "id": "call_fixture",
            "args": {"value": "A"}}


def test_sync_tool_call_envelope_preserves_artifact():
    message = asyncio.run(_sync_demo.ainvoke(_call("_sync_demo")))
    assert message.tool_call_id == "call_fixture"
    assert message.content == "ok:A"
    assert message.artifact == {"value": "A"}


def test_async_tool_call_envelope_preserves_artifact():
    message = asyncio.run(_async_demo.ainvoke(_call("_async_demo")))
    assert message.tool_call_id == "call_fixture"
    assert message.content == "async:A"
    assert message.artifact == {"value": "A"}
