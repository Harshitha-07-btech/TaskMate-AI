import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


def parse_sse(response):
    data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


class FakeQuery:
    def __init__(self):
        self.status_filter = None
        self.inserted_values = None

    def select(self, _columns):
        return self

    def order(self, _column, desc=False):
        return self

    def eq(self, _column, value):
        self.status_filter = value
        return self

    def insert(self, values):
        self.inserted_values = values
        return self

    def execute(self):
        if self.inserted_values is not None:
            return SimpleNamespace(
                data=[
                    {
                        "id": "created-fixture-id",
                        **self.inserted_values,
                        "status": "pending",
                    }
                ]
            )
        return SimpleNamespace(data=[{"id": "fixture-id", "title": "Fixture task"}])


class FakeDatabase:
    def __init__(self):
        self.query = FakeQuery()

    def table(self, _name):
        return self.query


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(server.app)
        self.headers = {"Authorization": "Bearer unit-test-token"}

    def post_rpc(self, payload):
        with patch.dict(os.environ, {"TASKMATE_MCP_BEARER_TOKEN": "unit-test-token"}):
            return self.http.post("/mcp", json=payload, headers=self.headers)

    def test_unauthenticated_tool_call_is_rejected_before_database_access(self):
        database = unittest.mock.Mock()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {"title": "Should not be created"},
            },
        }
        with patch.dict(os.environ, {"TASKMATE_MCP_BEARER_TOKEN": "unit-test-token"}):
            with patch.object(server, "db", database):
                response = self.http.post("/mcp", json=payload)

        self.assertEqual(response.status_code, 401)
        database.table.assert_not_called()

    def test_initialize_and_tools_list_use_sse_and_register_both_actions(self):
        initialized = self.post_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        listed = self.post_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        self.assertTrue(initialized.headers["content-type"].startswith("text/event-stream"))
        self.assertTrue(listed.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(parse_sse(initialized)["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(
            {tool["name"] for tool in parse_sse(listed)["result"]["tools"]},
            {"list_tasks", "create_task", "delete_task"},
        )

    def test_list_tasks_queries_supabase_and_returns_json_text(self):
        database = FakeDatabase()
        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_tasks", "arguments": {"status": "pending"}},
        }

        with patch.object(server, "db", database):
            response = self.post_rpc(payload)

        tool_result = parse_sse(response)["result"]
        self.assertEqual(database.query.status_filter, "pending")
        self.assertEqual(tool_result["content"][0]["type"], "text")
        self.assertEqual(
            json.loads(tool_result["content"][0]["text"]),
            {"result": [{"id": "fixture-id", "title": "Fixture task"}]},
        )

    def test_create_task_inserts_through_supabase_and_returns_saved_row(self):
        database = FakeDatabase()
        payload = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "title": "Finish hackathon project",
                    "description": "Wrap up the demo.",
                    "priority": "high",
                    "due_at": "2026-10-16T23:59:00+05:30",
                },
            },
        }

        with patch.object(server, "db", database):
            response = self.post_rpc(payload)

        tool_result = parse_sse(response)["result"]
        saved_row = json.loads(tool_result["content"][0]["text"])["result"]
        self.assertEqual(database.query.inserted_values["title"], "Finish hackathon project")
        self.assertEqual(database.query.inserted_values["priority"], "high")
        self.assertEqual(saved_row["id"], "created-fixture-id")
        self.assertEqual(saved_row["status"], "pending")

    def test_create_task_allows_title_and_deadline_without_inventing_other_fields(self):
        database = FakeDatabase()
        payload = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "title": "Finish hackathon project",
                    "due_at": "2026-10-16T23:59:00+05:30",
                },
            },
        }

        with patch.object(server, "db", database):
            response = self.post_rpc(payload)

        saved_row = json.loads(parse_sse(response)["result"]["content"][0]["text"])["result"]
        self.assertEqual(set(database.query.inserted_values), {"title", "due_at"})
        self.assertEqual(saved_row["id"], "created-fixture-id")

    def test_create_task_rejects_end_before_planned_start(self):
        database = FakeDatabase()
        payload = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "title": "Invalid period",
                    "scheduled_start": "2026-10-17T00:00:00+05:30",
                    "scheduled_end": "2026-10-16T23:59:00+05:30",
                },
            },
        }

        with patch.object(server, "db", database):
            response = self.post_rpc(payload)

        result = parse_sse(response)["result"]
        self.assertTrue(result["isError"])
        self.assertIsNone(database.query.inserted_values)


if __name__ == "__main__":
    unittest.main()