import copy
import json
import logging
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from armoriq_sdk import (
    ArmorIQSession,
    ConfigurationException,
    IntentMismatchException,
    InvalidTokenException,
    MCPInvocationException,
    PolicyBlockedException,
    PolicyHoldException,
)
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import armoriq_runtime
import main

TASK_A = "11111111-1111-4111-8111-111111111111"
TASK_B = "22222222-2222-4222-8222-222222222222"
SECRET_TITLE = "Confidential thesis draft"


class FakeTaskStore:
    """The data the protected MCP tools operate on."""

    def __init__(self):
        self.tasks = {
            TASK_A: {"id": TASK_A, "title": SECRET_TITLE, "status": "pending"},
            TASK_B: {"id": TASK_B, "title": "Buy groceries", "status": "pending"},
        }
        self.executed = []

    def snapshot(self):
        return copy.deepcopy(self.tasks)

    def run_tool(self, action, params):
        self.executed.append(action)
        if action == "list_tasks":
            result = list(self.tasks.values())
        elif action == "create_task":
            new_id = str(uuid.uuid4())
            result = self.tasks[new_id] = {"id": new_id, "status": "pending", **params}
        elif action == "complete_task":
            self.tasks[params["task_id"]]["status"] = "done"
            result = self.tasks[params["task_id"]]
        elif action == "delete_task":
            del self.tasks[params["task_id"]]
            result = {"deleted": True, "task_id": params["task_id"]}
        else:
            raise AssertionError(f"unexpected tool {action}")
        return {"content": [{"type": "text", "text": json.dumps({"result": result})}], "isError": False}

    def database(self):
        """A Supabase double for the backend's read-only task-title lookup."""
        database = Mock()
        query = database.table.return_value.select.return_value.order.return_value
        query.execute.side_effect = lambda: SimpleNamespace(data=list(self.snapshot().values()))
        return database


class FakeArmorIQClient:
    """Stands in for ArmorIQ's network services while real ArmorIQSession
    objects run the plan, trace, and flush lifecycle."""

    def __init__(self, store, *, denied=(), held=(), token_error=None, invoke_error=None, tool_error=False):
        self.store = store
        self.denied = set(denied)
        self.held = set(held)
        self.token_error = token_error
        self.invoke_error = invoke_error
        self.tool_error = tool_error
        self.api_key = "ak_test_unit"
        self.backend_endpoint = "https://api.armoriq.test"
        self.default_proxy_endpoint = "https://proxy.armoriq.test"
        self.user_id = "taskmate-service"
        self.agent_id = "taskmate-bot"
        self.user_email_override = None
        self.plans = []
        self.invocations = []
        self.sessions = []
        self.ingested = []
        self.http_client = Mock()
        self.http_client.post.side_effect = self._post

    def _post(self, url, json=None, **_kwargs):
        if url.endswith("/observability/spans"):
            self.ingested.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {"accepted": 1, "rejected": 0})

    def start_session(self, opts=None):
        session = ArmorIQSession(self, opts)
        self.sessions.append(session)
        return session

    def capture_plan(self, llm, prompt, plan, metadata=None):
        self.plans.append({"llm": llm, "prompt": prompt, "plan": plan})
        return SimpleNamespace(plan=plan, llm=llm, prompt=prompt)

    def get_intent_token(self, plan_capture, policy=None, validity_seconds=60):
        if self.token_error is not None:
            raise self.token_error
        return SimpleNamespace(plan=plan_capture.plan, validity_seconds=validity_seconds)

    def invoke(self, mcp, action, intent_token, params=None, **_kwargs):
        self.invocations.append({"mcp": mcp, "action": action, "params": params})
        if action not in {step["action"] for step in intent_token.plan["steps"]}:
            raise IntentMismatchException(f"Action '{action}' not found in the original plan.")
        # Mirrors the proxy: policy is evaluated before the MCP tool is reached.
        if action in self.denied:
            raise PolicyBlockedException("Enforcement: block", enforcement_action="block",
                                         reason="tool_not_in_allowlist")
        if action in self.held:
            raise PolicyHoldException("Enforcement: hold")
        if self.invoke_error is not None:
            raise self.invoke_error
        if self.tool_error:
            result = {"content": [{"type": "text", "text": json.dumps({"error": "boom"})}], "isError": True}
            return SimpleNamespace(status="error", verified=True, result=result)
        return SimpleNamespace(status="success", verified=True, result=self.store.run_tool(action, params))

    # Helpers for assertions -------------------------------------------------

    def shipped_traces(self):
        return [batch for payload in self.ingested for batch in payload["batches"]]

    def policy_spans(self):
        return [span for batch in self.shipped_traces() for span in batch["spans"] if span["kind"] == "policy_call"]


def function_call(name, arguments):
    return SimpleNamespace(type="function_call", id=f"call-{name}", name=name, arguments=arguments)


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)
        self.store = FakeTaskStore()

    def tearDown(self):
        self.assertTrue(armoriq_runtime.wait_for_telemetry(10.0), "ArmorIQ sessions were not closed")

    def build_gemini_client(self, reply="A real text response.", steps=None, usage=None):
        interactions = SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(output_text=reply, steps=steps or [], usage=usage))
        )
        return SimpleNamespace(interactions=interactions)

    def chat(self, armoriq_client, steps, message="Do the task action", timezone="Asia/Kolkata", usage=None):
        gemini_client = self.build_gemini_client(steps=steps, usage=usage)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key", "GEMINI_MODEL": "gemini-3.8-flash"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client", return_value=armoriq_client),
            patch.object(main, "db", self.store.database()),
            patch.object(main, "refresh_reminders_quietly"),
        ):
            response = self.http.post("/agent/chat", json={"message": message, "timezone": timezone})
        armoriq_runtime.wait_for_telemetry(10.0)
        return response


class AllowedActionTests(AgentTestCase):
    def test_chat_returns_text_without_invoking_armor_iq(self):
        gemini_client = self.build_gemini_client("Hello from Gemini.")

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key", "GEMINI_MODEL": "gemini-3.8-flash"}),
            patch.object(main.genai, "Client", return_value=gemini_client) as client_factory,
            patch.object(main, "get_armoriq_client") as armoriq_factory,
        ):
            response = self.http.post("/agent/chat", json={"message": "Say hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "Hello from Gemini."})
        client_factory.assert_called_once_with(
            api_key="unit-test-key", http_options=main.GEMINI_HTTP_OPTIONS
        )
        request = gemini_client.interactions.create.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.8-flash")
        self.assertEqual(request["input"], "Say hello")
        self.assertEqual(
            {tool["name"] for tool in request["tools"]},
            {"create_task", "list_tasks", "complete_task", "delete_task"},
        )
        armoriq_factory.assert_not_called()

    def test_create_task_runs_through_authorized_path_and_ships_redacted_trace(self):
        armoriq = FakeArmorIQClient(self.store)
        arguments = {"title": SECRET_TITLE + " v2", "description": "private notes", "due_at": "2026-10-17"}
        usage = SimpleNamespace(total_input_tokens=120, total_output_tokens=30)

        response = self.chat(armoriq, [function_call("create_task", arguments)], usage=usage)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["action"], "create_task")
        self.assertEqual(body["decision"], "allow")
        self.assertIn(SECRET_TITLE + " v2", body["reply"])
        self.assertIn(body["task"]["id"], self.store.tasks)

        params = main.normalize_task_arguments(arguments, main.resolve_timezone("Asia/Kolkata"))
        self.assertEqual(params["due_at"], "2026-10-17T23:59:00+05:30")
        self.assertEqual(armoriq.invocations, [{"mcp": "taskmate-mcp", "action": "create_task", "params": params}])
        step = armoriq.plans[0]["plan"]["steps"][0]
        self.assertEqual((step["mcp"], step["action"], step["params"]), ("taskmate-mcp", "create_task", params))
        self.assertEqual(armoriq.plans[0]["llm"], "gemini-3.8-flash")

        traces = armoriq.shipped_traces()
        self.assertEqual(len(traces), 1)
        trace = traces[0]["trace"]
        self.assertEqual(trace["id"], body["trace_id"])
        self.assertEqual(trace["sessionId"], body["request_id"])
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["agentId"], "taskmate-bot")
        kinds = {span["kind"] for span in traces[0]["spans"]}
        self.assertTrue({"policy_call", "generation", "span"} <= kinds)
        [decision] = armoriq.policy_spans()
        self.assertEqual(decision["attributes"]["decision"], "allow")
        self.assertEqual(decision["attributes"]["source"], "proxy")
        invoke_span = next(s for s in traces[0]["spans"] if s["name"] == "taskmate.mcp.invoke")
        self.assertEqual(invoke_span["status"], "ok")
        self.assertIsInstance(invoke_span["durationMs"], int)
        generation = next(s for s in traces[0]["spans"] if s["kind"] == "generation")
        self.assertIsNone(generation["attributes"]["prompt"])

        shipped = json.dumps(armoriq.ingested)
        self.assertNotIn(SECRET_TITLE, shipped)
        self.assertNotIn("private notes", shipped)
        self.assertNotIn("ak_test_unit", shipped)

    def test_list_tasks_uses_authorized_list_action(self):
        armoriq = FakeArmorIQClient(self.store)

        response = self.chat(armoriq, [function_call("list_tasks", {"status": "pending"})], "Show my open tasks")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "list_tasks")
        self.assertIn("Buy groceries (open)", response.json()["reply"])
        self.assertEqual(armoriq.invocations[0]["params"], {"status": "pending"})
        self.assertEqual(self.store.executed, ["list_tasks"])

    def test_complete_task_resolves_title_and_updates_only_that_task(self):
        armoriq = FakeArmorIQClient(self.store)

        response = self.chat(armoriq, [function_call("complete_task", {"task_title": "buy groceries"})])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"]["status"], "done")
        self.assertEqual(armoriq.invocations[0]["params"], {"task_id": TASK_B})
        self.assertEqual(self.store.tasks[TASK_A]["status"], "pending")

    def test_ambiguous_or_unknown_task_titles_ask_instead_of_invoking(self):
        self.store.tasks[str(uuid.uuid4())] = {"id": "x", "title": "Buy groceries", "status": "pending"}
        for title in ("buy groceries", "No such task"):
            with self.subTest(title=title):
                armoriq = FakeArmorIQClient(self.store)
                response = self.chat(armoriq, [function_call("delete_task", {"task_title": title})])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(armoriq.plans, [])
                self.assertEqual(armoriq.invocations, [])


class DeniedActionTests(AgentTestCase):
    def test_proxy_denial_never_reaches_the_tool_and_leaves_data_unchanged(self):
        armoriq = FakeArmorIQClient(self.store, denied={"delete_task"})
        before = self.store.snapshot()

        with self.assertLogs("taskmate", level="INFO") as logs:
            response = self.chat(armoriq, [function_call("delete_task", {"task_title": SECRET_TITLE})])

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["error_type"], "policy_denied")
        self.assertEqual(body["reason"], "tool_not_in_allowlist")
        self.assertIn("Nothing was changed", body["detail"])
        self.assertNotIn("task", body)
        self.assertEqual(self.store.executed, [])
        self.assertEqual(self.store.snapshot(), before)

        [trace] = armoriq.shipped_traces()
        self.assertEqual(trace["trace"]["id"], body["trace_id"])
        self.assertEqual(trace["trace"]["status"], "denied")
        [decision] = armoriq.policy_spans()
        self.assertEqual(decision["attributes"]["decision"], "deny")
        self.assertEqual(decision["attributes"]["source"], "proxy")
        self.assertEqual(decision["attributes"]["reason"], "tool_not_in_allowlist")
        self.assertEqual(decision["status"], "denied")
        self.assertNotIn(SECRET_TITLE, json.dumps(armoriq.ingested))
        self.assertNotIn(SECRET_TITLE, "\n".join(logs.output))

    def test_denial_at_token_issuance_never_invokes_the_proxy(self):
        armoriq = FakeArmorIQClient(
            self.store,
            token_error=PolicyBlockedException("Policy blocked intent token issuance", reason="delete_task: denied"),
        )
        before = self.store.snapshot()

        response = self.chat(armoriq, [function_call("delete_task", {"task_title": "Buy groceries"})])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_type"], "policy_denied")
        self.assertEqual(armoriq.invocations, [])
        self.assertEqual(self.store.snapshot(), before)
        [decision] = armoriq.policy_spans()
        self.assertEqual(decision["attributes"]["source"], "sdk")
        self.assertEqual(armoriq.shipped_traces()[0]["trace"]["status"], "denied")

    def test_hold_is_reported_as_approval_required_not_success(self):
        armoriq = FakeArmorIQClient(self.store, held={"create_task"})

        response = self.chat(armoriq, [function_call("create_task", {"title": "Needs approval"})])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_type"], "policy_hold")
        self.assertEqual(self.store.executed, [])
        self.assertEqual(armoriq.policy_spans()[0]["attributes"]["decision"], "hold")

    def test_allowed_actions_still_work_after_a_denial(self):
        armoriq = FakeArmorIQClient(self.store, denied={"delete_task"})
        denied = self.chat(armoriq, [function_call("delete_task", {"task_title": "Buy groceries"})])
        allowed = self.chat(armoriq, [function_call("list_tasks", {})])

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.store.executed, ["list_tasks"])


class FailureClassificationTests(AgentTestCase):
    def test_failures_have_distinct_types_and_never_look_like_policy_denials(self):
        timeout = MCPInvocationException("MCP invocation failed: timed out")
        timeout.__context__ = httpx.ReadTimeout("timed out")
        cases = [
            ({"token_error": InvalidTokenException("Token issuance failed: bad key")}, 502, "token_issuance"),
            ({"invoke_error": InvalidTokenException("Token verification failed: {'detail': 'Unauthorized.'}")}, 502, "auth_failed"),
            ({"invoke_error": IntentMismatchException("synthetic plan mismatch")}, 409, "intent_mismatch"),
            ({"invoke_error": MCPInvocationException("synthetic MCP failure", status_code=502)}, 502, "mcp_error"),
            ({"invoke_error": timeout}, 504, "timeout"),
            ({"invoke_error": RuntimeError("bug")}, 500, "unexpected"),
            ({"tool_error": True}, 502, "mcp_tool_error"),
        ]
        for kwargs, status_code, error_type in cases:
            with self.subTest(error_type=error_type):
                armoriq = FakeArmorIQClient(self.store, **kwargs)
                before = self.store.snapshot()

                response = self.chat(armoriq, [function_call("list_tasks", {})])

                self.assertEqual(response.status_code, status_code)
                body = response.json()
                self.assertEqual(body["error_type"], error_type)
                self.assertNotIn("reply", body)
                self.assertNotIn("reason", body)
                self.assertNotIn("policy", body["detail"].lower())
                self.assertEqual(self.store.snapshot(), before)
                # Only a completed proxy call is recorded as a policy decision
                # (an MCP tool error is still an allowed call); infrastructure
                # failures must not appear as deny decisions.
                decisions = [span["attributes"]["decision"] for span in armoriq.policy_spans()]
                self.assertEqual(decisions, ["allow"] if error_type == "mcp_tool_error" else [])
                traces = armoriq.shipped_traces()
                self.assertEqual(len(traces), 1, "the failed action's trace must still be shipped")
                self.assertEqual(traces[0]["trace"]["status"], "error")

    def test_every_session_is_closed_and_its_shipper_stopped(self):
        armoriq = FakeArmorIQClient(self.store, invoke_error=MCPInvocationException("synthetic"))
        self.chat(armoriq, [function_call("list_tasks", {})])
        armoriq.invoke_error = None
        self.chat(armoriq, [function_call("list_tasks", {})])

        self.assertEqual(len(armoriq.sessions), 2)
        for session in armoriq.sessions:
            self.assertTrue(session.observability_recorder.shipper._stopped)
        self.assertEqual(len({s.session_id for s in armoriq.sessions}), 2)

    def test_missing_armor_iq_configuration_fails_closed_without_success(self):
        gemini_client = self.build_gemini_client(steps=[function_call("create_task", {"title": "Unconfigured"})])
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client",
                         side_effect=ConfigurationException("synthetic missing ArmorIQ key")),
        ):
            response = self.http.post("/agent/chat", json={"message": "Create an unconfigured task"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_type"], "configuration")
        self.assertIn("Protected task actions aren't configured", response.json()["detail"])
        self.assertNotIn("task", response.json())

    def test_invalid_task_parameters_and_multiple_calls_never_invoke_armor_iq(self):
        invalid_call = function_call("create_task", {"title": "  ", "document_url": "https://example.invalid"})
        second_call = function_call("list_tasks", {})
        for steps in ([invalid_call], [invalid_call, second_call]):
            with self.subTest(call_count=len(steps)):
                gemini_client = self.build_gemini_client(steps=steps)
                with (
                    patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
                    patch.object(main.genai, "Client", return_value=gemini_client),
                    patch.object(main, "get_armoriq_client") as armoriq_factory,
                ):
                    response = self.http.post("/agent/chat", json={"message": "Create a task"})

                self.assertEqual(response.status_code, 200)
                armoriq_factory.assert_not_called()

    def test_unknown_timezone_is_rejected(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}):
            response = self.http.post("/agent/chat", json={"message": "Hi", "timezone": "Mars/Base"})
        self.assertEqual(response.status_code, 422)


class DateHandlingTests(unittest.TestCase):
    def test_date_only_task_deadline_uses_local_end_of_day(self):
        parameters = main.normalize_task_arguments(
            {"title": "Finish hackathon project", "due_at": "2026-10-17"}
        )
        self.assertTrue(parameters["due_at"].startswith("2026-10-17T23:59:00"))

    def test_dates_use_the_users_time_zone_not_the_servers(self):
        zone = main.resolve_timezone("America/New_York")
        parameters = main.normalize_task_arguments(
            {"title": "Call", "due_at": "2026-10-17", "scheduled_start": "2026-10-16T09:30:00"}, zone
        )
        self.assertEqual(parameters["due_at"], "2026-10-17T23:59:00-04:00")
        self.assertEqual(parameters["scheduled_start"], "2026-10-16T09:30:00-04:00")


class GeminiErrorTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)

    def build_gemini_client(self):
        return SimpleNamespace(interactions=SimpleNamespace(create=Mock()))

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

    def test_gemini_client_never_retries_quota_errors_and_is_time_bounded(self):
        # Regression: the SDK slept for a ~34,000 s Retry-After on a quota 429,
        # leaving the chat request (and the UI) pending for hours.
        with patch.object(main.genai, "Client", return_value=Mock()) as client_class:
            main.call_gemini("unit-test-key", model="m", input="Hello")

        options = client_class.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, int(main.GEMINI_TIMEOUT_SECONDS * 1000))
        self.assertNotIn(429, options.retry_options.http_status_codes)
        self.assertLessEqual(options.retry_options.attempts, 1)

    def test_long_retry_after_quota_error_returns_promptly_with_reset_time(self):
        gemini_client = self.build_gemini_client()
        armoriq_client = Mock()
        gemini_client.interactions.create.side_effect = genai_errors.ClientError(
            429,
            {"error": {"code": "quota_exceeded", "status": "RESOURCE_EXHAUSTED", "message": "limit reached"}},
            httpx.Response(429, headers={"retry-after": "34331"}),
        )
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
            patch.object(main, "get_armoriq_client", return_value=armoriq_client),
        ):
            response = self.http.post("/agent/chat", json={"message": "Add a task to review my project tomorrow"})

        self.assertEqual(response.status_code, 429)
        self.assertIn("quota for this API key is used up", response.json()["detail"])
        self.assertIn("about 10 hours", response.json()["detail"])
        armoriq_client.invoke.assert_not_called()

    def test_gemini_call_past_its_deadline_returns_504_without_task_action(self):
        import threading

        release = threading.Event()
        gemini_client = self.build_gemini_client()
        gemini_client.interactions.create.side_effect = lambda **_: release.wait(5)
        armoriq_client = Mock()
        try:
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
                patch.object(main, "GEMINI_DEADLINE_SECONDS", 0.2),
                patch.object(main.genai, "Client", return_value=gemini_client),
                patch.object(main, "get_armoriq_client", return_value=armoriq_client),
            ):
                response = self.http.post("/agent/chat", json={"message": "Add a task to review my project tomorrow"})
        finally:
            release.set()

        self.assertEqual(response.status_code, 504)
        self.assertIn("No task action was taken", response.json()["detail"])
        armoriq_client.capture_plan.assert_not_called()
        armoriq_client.invoke.assert_not_called()

    def test_interactions_api_rate_limit_error_maps_to_quota_message(self):
        # The Interactions API raises compat errors, not genai_errors.APIError;
        # they used to fall through to a generic 502.
        from google.genai._gaos.lib import compat_errors

        quota = compat_errors.RateLimitError(
            "quota",
            response=httpx.Response(
                429, headers={"retry-after": "34331"},
                request=httpx.Request("POST", "https://example.invalid"),
            ),
            body={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}},
        )
        gemini_client = self.build_gemini_client()
        gemini_client.interactions.create.side_effect = quota
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}),
            patch.object(main.genai, "Client", return_value=gemini_client),
        ):
            response = self.http.post("/agent/chat", json={"message": "Show me my saved tasks"})

        self.assertEqual(response.status_code, 429)
        self.assertIn("quota for this API key is used up", response.json()["detail"])

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
                    response = self.http.post("/agent/chat", json={"message": "Hello"})

                self.assertIn(expected_detail, response.json()["detail"])

    def test_missing_gemini_credential_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.http.post("/agent/chat", json={"message": "Show tasks"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Gemini is not configured.")


class ManualTaskRouteTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)

    def test_manual_task_create_and_completion_routes_remain_available(self):
        created_task = {
            "id": TASK_A,
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

        with patch.object(main, "db", database), patch.object(main, "refresh_reminders_quietly") as refresh:
            created = self.http.post("/tasks", json={"title": "Manual fixture task"})
            completed = self.http.patch(f"/tasks/{TASK_A}", json={"status": "done"})
            reloaded = self.http.get("/tasks?status=all")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["id"], TASK_A)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "done")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["tasks"][0]["status"], "done")
        self.assertEqual(refresh.call_count, 2)

    def test_delete_task_route_confirms_database_deletion(self):
        database = Mock()
        database.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[{"id": TASK_A, "title": "Manual fixture task"}])
        )

        with patch.object(main, "db", database):
            response = self.http.delete(f"/tasks/{TASK_A}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "task_id": TASK_A})
        database.table.return_value.delete.return_value.eq.assert_called_once_with("id", TASK_A)

    def test_delete_task_route_returns_not_found_when_no_row_was_deleted(self):
        database = Mock()
        database.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[])
        )

        with patch.object(main, "db", database):
            response = self.http.delete(f"/tasks/{TASK_B}")

        self.assertEqual(response.status_code, 404)

    def test_non_uuid_task_ids_are_rejected_before_database_access(self):
        database = Mock()
        with patch.object(main, "db", database):
            deleted = self.http.delete("/tasks/not-a-uuid")
            updated = self.http.patch("/tasks/not-a-uuid", json={"status": "done"})

        self.assertEqual((deleted.status_code, updated.status_code), (404, 404))
        database.table.assert_not_called()


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

    def test_policy_denies_delete_and_allows_the_chat_actions(self):
        from armoriq_sdk.config import load_armoriq_config

        with patch.dict(os.environ, {"ARMORIQ_API_KEY": "ak_live_unit", "TASKMATE_MCP_BEARER_TOKEN": "x"}):
            policy = load_armoriq_config(main.ARMORIQ_CONFIG).policy
        self.assertIn("taskmate-mcp.delete_task", policy.deny)
        self.assertNotIn("taskmate-mcp.delete_task", policy.allow)
        self.assertTrue({"taskmate-mcp.list_tasks", "taskmate-mcp.create_task"} <= set(policy.allow))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
