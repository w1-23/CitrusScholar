"""The send-view estimate includes tool schemas and paged readback text."""
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.request_context import estimate_request


class SchemaBudgetTests(unittest.TestCase):
    def setUp(self):
        self.schemas = [{"type": "function", "function": {
            "name": "tool_%d" % index, "description": "x" * 200,
            "parameters": {"type": "object", "properties": {}}}} for index in range(20)]
        self.messages = [HumanMessage(content="hello"), AIMessage(content="world")]

    def test_schema_is_part_of_the_send_view_count(self):
        plain = estimate_request(self.messages)["estimated_input_tokens"]
        counted = estimate_request(self.messages, tools=self.schemas)["estimated_input_tokens"]
        self.assertGreater(counted, plain)

    def test_paged_readback_is_counted_cumulatively(self):
        readback = ToolMessage(content="R" * 6000, tool_call_id="call-1")
        before = estimate_request(self.messages, tools=self.schemas)["estimated_input_tokens"]
        after = estimate_request(self.messages + [readback], tools=self.schemas)["estimated_input_tokens"]
        self.assertGreater(after - before, 1000)
