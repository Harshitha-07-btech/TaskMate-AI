import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call as mock_call, patch

import httpx
from armoriq_sdk import (
    ConfigurationException,
    IntentMismatchException,
    InvalidTokenException,
    MCPInvocationException,
    PolicyBlockedException,
)
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import main


class AgentSecurityTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)

    def build_gemini_client(self, reply="A real text response.", steps=None):
        interactions = SimpleNamespace(
            create=Mock(
                return_value=SimpleNamespace(output_text=reply, steps=steps or [])
            )
        )
        return SimpleNamespace(interactions=interactions)

    @staticmethod
    def build_armor_client(tool_result):
        client = Mock()
        client.capture_plan.return_value = "captured-plan"
        client.get_intent_token.return_value = "intent-token"
        client.invoke.return_value = SimpleNamespace(
            status="success",
            verified=True,
            result={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"result": tool_result}),
                    }
                ],
                "isError": False,
            },
        )
        return client

    def test_chat_returns_text_without_invoking_armor_iq(self):
        gemini_client = self.build_gemini_client("Hello from Gemini.")

        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "unit-test-key", "GEMINI_MODEL": "gemini-3.8-flash"},
            ),
            patch.object(main.genai, "Client", return_value=gemini_client) as client_factory,
            patch.object(main, "get_armoriq_client") as armoriq_factory,
        ):
            response = self.http.post(
                "/agent/chat", json={"message": "Say hello"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "Hello from Gemini."})
        client_factory.assert_called_once_with(api_key="unit-test-key")
        request = gemini_client.interactions.create.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.8-flash")
        self.assertEqual(request["input"], "Say hello")
        self.assertIn("system_instruction", request)
        self.assertEqual(
            {tool["name"] for tool in request["tools"]},
            {"create_task", "list_tasks"},
        )
        armoriq_factory.assert_not_called()

    def test_create_task_uses_armor_iq_and_returns_confirmed_mcp_row(self):
        saved_task = {
            "id": "saved-task-id",
            "title": "Finish hackathon project",
            "due_at": "2026-10-17T23:59:00+00:00",
            "status": "pending",
        }
        call = SimpleNamespace(
            type="function_call",
            id="function-call-1",
            name="create_task",
            arguments={
                "title": "Finish hackathon project",
                "due_at": "2026-10-17",
            },
        )
        gemini_client = self.build_gemini_client(steps=[call])
        armoriq_client = self.build_armor_client(saved_task)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client", return_value=armoriq_client),
            patch.object(main, "db") as database,
        ):
            response = self.http.post(
                "/agent/chat",
                json={"message": "Add a task to finish my hackathon project today."},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"], saved_task)
        self.assertEqual(response.json()["action"], "create_task")
        self.assertIn("Finish hackathon project", response.json()["reply"])
        parameters = main.normalize_task_arguments(call.arguments)
        armoriq_client.capture_plan.assert_called_once()
        captured_plan = armoriq_client.capture_plan.call_args.kwargs
        self.assertEqual(captured_plan["llm"], "gemini-3.8-flash")
        self.assertEqual(
            captured_plan["plan"]["steps"],
            [{"action": "create_task", "mcp": "taskmate-mcp", "params": parameters}],
        )
        armoriq_client.get_intent_token.assert_called_once_with(
            "captured-plan", validity_seconds=300
        )
        armoriq_client.invoke.assert_called_once_with(
            mcp="taskmate-mcp",
            action="create_task",
            intent_token="intent-token",
            params=parameters,
        )
        database.table.assert_not_called()

    def test_show_tasks_uses_authorized_list_action(self):
        saved_tasks = [
            {"id": "task-1", "title": "Fixture task", "status": "pending"}
        ]
        call = SimpleNamespace(
            type="function_call",
            id="function-call-1",
            name="list_tasks",
            arguments={"status": "pending"},
        )
        gemini_client = self.build_gemini_client(steps=[call])
        armoriq_client = self.build_armor_client(saved_tasks)

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client", return_value=armoriq_client),
        ):
            response = self.http.post("/agent/chat", json={"message": "Show my open tasks"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "list_tasks")
        self.assertIn("Fixture task (open)", response.json()["reply"])
        plan = {
            "goal": "Complete the user's TaskMate request using list_tasks.",
            "steps": [
                {
                    "action": "list_tasks",
                    "mcp": "taskmate-mcp",
                    "params": {"status": "pending"},
                }
            ],
        }
        self.assertEqual(
            armoriq_client.mock_calls,
            [
                mock_call.capture_plan(
                    llm="gemini-3.8-flash",
                    prompt="Show my open tasks",
                    plan=plan,
                ),
                mock_call.get_intent_token("captured-plan", validity_seconds=300),
                mock_call.invoke(
                    mcp="taskmate-mcp",
                    action="list_tasks",
                    intent_token="intent-token",
                    params={"status": "pending"},
                ),
            ],
        )
        armoriq_client.invoke.assert_called_once_with(
            mcp="taskmate-mcp",
            action="list_tasks",
            intent_token="intent-token",
            params={"status": "pending"},
        )

    def test_armor_iq_policy_block_never_confirms_task_creation(self):
        call = SimpleNamespace(
            type="function_call",
            id="function-call-blocked",
            name="create_task",
            arguments={"title": "Blocked fixture task"},
        )
        gemini_client = self.build_gemini_client(steps=[call])
        armoriq_client = self.build_armor_client(None)
        armoriq_client.invoke.side_effect = PolicyBlockedException(
            "Denied by policy.", reason="tool denied"
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client", return_value=armoriq_client),
        ):
            response = self.http.post(
                "/agent/chat", json={"message": "Create a blocked fixture task"}
            )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("task", response.json())
        self.assertNotIn("Added", response.json()["detail"])

    def test_intent_and_mcp_failures_have_distinct_responses(self):
        failures = [
            (
                InvalidTokenException("synthetic invalid token"),
                502,
                "fresh intent token",
            ),
            (
                IntentMismatchException("synthetic plan mismatch"),
                409,
                "captured intent plan",
            ),
            (
                MCPInvocationException("synthetic MCP failure"),
                502,
                "could not complete the MCP invocation",
            ),
        ]
        for failure, expected_status, expected_detail in failures:
            with self.subTest(failure=type(failure).__name__):
                call_step = SimpleNamespace(
                    type="function_call",
                    id="function-call-list",
                    name="list_tasks",
                    arguments={},
                )
                gemini_client = self.build_gemini_client(steps=[call_step])
                armoriq_client = self.build_armor_client(None)
                armoriq_client.invoke.side_effect = failure

                with (
                    patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
                    patch.object(main.genai, "Client", return_value=gemini_client),
                    patch.object(main, "get_armoriq_client", return_value=armoriq_client),
                ):
                    response = self.http.post(
                        "/agent/chat", json={"message": "Show my tasks"}
                    )

                self.assertEqual(response.status_code, expected_status)
                self.assertIn(expected_detail, response.json()["detail"])
                self.assertNotIn("reachable", response.json()["detail"])

    def test_missing_armor_iq_configuration_fails_closed_without_success(self):
        call = SimpleNamespace(
            type="function_call",
            id="function-call-config",
            name="create_task",
            arguments={"title": "Unconfigured fixture task"},
        )
        gemini_client = self.build_gemini_client(steps=[call])

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(
                main,
                "get_armoriq_client",
                side_effect=ConfigurationException("synthetic missing ArmorIQ key"),
            ),
        ):
            response = self.http.post(
                "/agent/chat", json={"message": "Create an unconfigured fixture task"}
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Protected task actions aren't configured", response.json()["detail"])
        self.assertNotIn("task", response.json())

    def test_invalid_task_parameters_and_multiple_calls_never_invoke_armor_iq(self):
        invalid_call = SimpleNamespace(
            type="function_call",
            id="function-call-invalid",
            name="create_task",
            arguments={"title": "  ", "document_url": "https://example.invalid"},
        )
        second_call = SimpleNamespace(
            type="function_call",
            id="function-call-second",
            name="list_tasks",
            arguments={},
        )
        for steps in ([invalid_call], [invalid_call, second_call]):
            with self.subTest(call_count=len(steps)):
                gemini_client = self.build_gemini_client(steps=steps)
                with (
                    patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
                    patch.object(main.genai, "Client", return_value=gemini_client),
                    patch.object(main, "get_armoriq_client") as armoriq_factory,
                ):
                    response = self.http.post(
                        "/agent/chat", json={"message": "Create a task"}
                    )

                self.assertEqual(response.status_code, 200)
                armoriq_factory.assert_not_called()

    def test_date_only_task_deadline_uses_local_end_of_day(self):
        parameters = main.normalize_task_arguments(
            {"title": "Finish hackathon project", "due_at": "2026-10-17"}
        )

        self.assertTrue(parameters["due_at"].startswith("2026-10-17T23:59:00"))

    def test_chat_maps_invalid_key_to_friendly_error(self):
        gemini_client = self.build_gemini_client()
        gemini_client.interactions.create.side_effect = genai_errors.ClientError(
            401,
            {"error": {"code": "authentication", "status": "UNAUTHENTICATED", "message": "invalid key"}},
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
        ):
            response = self.http.post("/agent/chat", json={"message": "Hello"})

        self.assertEqual(response.status_code, 502)
        self.assertIn("rejected the configured API key", response.json()["detail"])

    def test_chat_maps_quota_exhaustion_to_retryable_error(self):
        gemini_client = self.build_gemini_client()
        gemini_client.interactions.create.side_effect = genai_errors.ClientError(
            429,
            {"error": {"code": "quota_exceeded", "status": "RESOURCE_EXHAUSTED", "message": "limit reached"}},
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
        ):
            response = self.http.post("/agent/chat", json={"message": "Hello"})

        self.assertEqual(response.status_code, 429)
        self.assertIn("quota is exhausted", response.json()["detail"])

    def test_chat_maps_missing_model_and_network_errors(self):
        cases = [
            (
                genai_errors.ClientError(
                    404,
                    {"error": {"code": "model_not_found", "status": "NOT_FOUND", "message": "missing"}},
                ),
                "configured Gemini model wasn't found",
            ),
            (httpx.ConnectError("connection refused"), "couldn't reach Gemini"),
        ]
        for error, expected_detail in cases:
            with self.subTest(error=type(error).__name__):
                gemini_client = self.build_gemini_client()
                gemini_client.interactions.create.side_effect = error
                with (
                    patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
                    patch.object(main.genai, "Client", return_value=gemini_client),
                ):
                    response = self.http.post(
                        "/agent/chat", json={"message": "Hello"}
                    )

                self.assertIn(expected_detail, response.json()["detail"])

    def test_manual_task_create_and_completion_routes_remain_available(self):
        created_task = {
            "id": "manual-fixture-id",
            "title": "Manual fixture task",
            "description": "",
            "priority": "normal",
            "status": "pending",
        }
        completed_task = {**created_task, "status": "done"}
        database = Mock()
        database.table.return_value.insert.return_value.execute.return_value = (
            SimpleNamespace(data=[created_task])
        )
        database.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[completed_task])
        )
        database.table.return_value.select.return_value.order.return_value.execute.return_value = (
            SimpleNamespace(data=[completed_task])
        )

        with patch.object(main, "db", database):
            created = self.http.post(
                "/tasks", json={"title": "Manual fixture task"}
            )
            completed = self.http.patch(
                "/tasks/manual-fixture-id", json={"status": "done"}
            )
            reloaded = self.http.get("/tasks?status=all")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["id"], "manual-fixture-id")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "done")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["tasks"][0]["status"], "done")

    def test_delete_task_route_confirms_database_deletion(self):
        deleted_task = {"id": "manual-fixture-id", "title": "Manual fixture task"}
        database = Mock()
        database.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[deleted_task])
        )

        with patch.object(main, "db", database):
            response = self.http.delete("/tasks/manual-fixture-id")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"deleted": True, "task_id": "manual-fixture-id"},
        )
        database.table.return_value.delete.return_value.eq.assert_called_once_with(
            "id", "manual-fixture-id"
        )

    def test_delete_task_route_returns_not_found_when_no_row_was_deleted(self):
        database = Mock()
        database.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[])
        )

        with patch.object(main, "db", database):
            response = self.http.delete("/tasks/missing-task")

        self.assertEqual(response.status_code, 404)

    def test_missing_gemini_credential_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.http.post("/agent/chat", json={"message": "Show tasks"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Gemini is not configured.")


class ArmorIQClientConfigTests(unittest.TestCase):
    """Regression: the proxy must receive the resolved MCP bearer token.

    from_config() dropped mcp_servers[].auth, so ArmorIQ called the MCP server
    with the wrong bearer and its 401 surfaced as InvalidTokenException
    ("Token verification failed: {'detail': 'Unauthorized.'}").
    """

    def test_client_sends_resolved_mcp_bearer_from_project_config(self):
        env = {"ARMORIQ_API_KEY": "ak_live_unit", "TASKMATE_MCP_BEARER_TOKEN": "unit-bearer"}
        with (
            patch.dict(os.environ, env),
            patch.object(main, "ArmorIQClient") as client_class,
        ):
            main.build_armoriq_client(main.ARMORIQ_CONFIG)

        kwargs = client_class.call_args.kwargs
        self.assertEqual(
            kwargs["mcp_credentials"],
            {"taskmate-mcp": {"authType": "bearer", "token": "unit-bearer"}},
        )
        self.assertEqual(kwargs["api_key"], "ak_live_unit")
        self.assertTrue(kwargs["use_production"])

    def test_missing_mcp_bearer_fails_closed(self):
        env = {"ARMORIQ_API_KEY": "ak_live_unit", "TASKMATE_MCP_BEARER_TOKEN": ""}
        with (
            patch.dict(os.environ, env),
            patch.object(main, "ArmorIQClient") as client_class,
        ):
            with self.assertRaises(ConfigurationException):
                main.build_armoriq_client(main.ARMORIQ_CONFIG)
        client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()