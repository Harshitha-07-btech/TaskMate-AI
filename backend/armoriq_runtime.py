"""Per-request ArmorIQ sessions for protected TaskMate MCP actions.

SDK 0.6.10 only emits observability telemetry from ``ArmorIQSession``;
``ArmorIQClient.invoke`` on its own records nothing, which is why earlier
builds showed no traces. Each protected action therefore runs inside its own
proxy-mode session:

    start_plan()  -> token issuance (policy may deny here)
    client.invoke -> proxy enforcement + MCP call (policy may deny here)
    close()       -> ends the plan trace and flushes it to ArmorIQ

The invoke step is wrapped in our own container span instead of
``session.dispatch``/``session.check`` because those copy raw tool arguments
(task titles and descriptions) into span attributes. Spans recorded here hold
only the MCP id, action, argument names, and the decision ArmorIQ returned.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

import httpx
from armoriq_sdk import (
    ArmorIQClient,
    ArmorIQException,
    ArmorIQSession,
    IntentMismatchException,
    InvalidTokenException,
    MCPInvocationException,
    PolicyBlockedException,
    PolicyHoldException,
    SessionOptions,
    ToolCall,
)
from armoriq_sdk.observability import close_span, open_span, record_policy_call

logger = logging.getLogger(__name__)

MCP_SERVER_ID = "taskmate-mcp"
INTENT_VALIDITY_SECONDS = 300

_telemetry_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="armoriq-telemetry")
_pending_closes: set[Future] = set()
_pending_lock = threading.Lock()


class ProtectedActionError(Exception):
    """A protected action that did not complete, classified for the caller.

    ``kind`` is one of: policy_denied, policy_hold, token_issuance,
    auth_failed, intent_mismatch, timeout, mcp_error, armoriq_error, unexpected.
    """

    def __init__(self, kind: str, stage: str, cause: BaseException, trace_id: str | None):
        super().__init__(f"{kind} during {stage}: {type(cause).__name__}")
        self.kind = kind
        self.stage = stage
        self.cause = cause
        self.trace_id = trace_id
        self.reason = getattr(cause, "reason", None)


@dataclass
class ProtectedActionResult:
    invocation: object
    trace_id: str | None


def new_request_id() -> str:
    return str(uuid.uuid4())


def _is_timeout(exc: BaseException) -> bool:
    # client.invoke wraps transport errors in MCPInvocationException without
    # `from`, so the original httpx exception is only on __context__.
    seen = exc
    while seen is not None:
        if isinstance(seen, httpx.TimeoutException):
            return True
        seen = seen.__context__
    return "timed out" in str(exc).lower()


def classify(exc: BaseException, stage: str) -> str:
    if isinstance(exc, PolicyBlockedException):
        return "policy_denied"
    if isinstance(exc, PolicyHoldException):
        return "policy_hold"
    if isinstance(exc, IntentMismatchException):
        return "intent_mismatch"
    if isinstance(exc, InvalidTokenException):
        # Token issuance happens in start_plan; a 401/403 from the proxy during
        # invoke means the intent token or the MCP bearer credential was refused.
        return "token_issuance" if stage == "plan" else "auth_failed"
    if isinstance(exc, MCPInvocationException):
        return "timeout" if _is_timeout(exc) else "mcp_error"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "armoriq_error" if isinstance(exc, ArmorIQException) else "unexpected"


def _bounded(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _record_decision(session: ArmorIQSession, decision: str, *, source: str, reason: str | None,
                     enforcement_action: str, action: str, parent_span_id: str | None) -> None:
    """Record a decision ArmorIQ actually returned. Never raises."""
    recorder = session.observability_recorder
    ctx = session.active_plan_trace_context
    if recorder is None or ctx is None:
        return
    try:
        record_policy_call(
            recorder,
            ctx,
            policy_id=None,
            policy_name=None,
            decision=decision,
            reason=_bounded(reason),
            source=source,
            input={"mcp": MCP_SERVER_ID, "toolName": action},
            output={"enforcementAction": enforcement_action},
            enforcement_action=enforcement_action,
            parent_span_id=parent_span_id,
        )
    except Exception:  # noqa: BLE001 - telemetry must never change the outcome
        logger.warning("Could not record ArmorIQ policy decision span", exc_info=True)


def _finish_session(session: ArmorIQSession, status: str) -> None:
    # SDK 0.6.10 has no public way to end a plan trace with a non-"ok"
    # status, and close() always ends it as "ok". _end_plan_trace is the
    # session's own internal hook for this; close() then stops the shipper and
    # performs the final flush.
    end_plan_trace = getattr(session, "_end_plan_trace", None)
    if callable(end_plan_trace):
        try:
            end_plan_trace(status)
        except Exception:  # noqa: BLE001
            logger.warning("Could not end ArmorIQ plan trace", exc_info=True)
    session.close()


def schedule_session_close(session: ArmorIQSession, status: str) -> None:
    """Close the session off the request path; the final flush can retry for
    several seconds when ArmorIQ's ingest endpoint is slow."""
    future = _telemetry_executor.submit(_finish_session, session, status)
    with _pending_lock:
        _pending_closes.add(future)
    future.add_done_callback(_discard_future)


def _discard_future(future: Future) -> None:
    with _pending_lock:
        _pending_closes.discard(future)
    if future.exception() is not None:
        logger.warning("ArmorIQ session close failed: %s", type(future.exception()).__name__)


def wait_for_telemetry(timeout: float = 30.0) -> bool:
    """Block until scheduled session closes finish. Returns True when drained."""
    with _pending_lock:
        pending = set(_pending_closes)
    if not pending:
        return True
    _done, not_done = wait(pending, timeout=timeout)
    return not not_done


def run_protected_action(
    client: ArmorIQClient,
    *,
    request_id: str,
    model: str,
    action: str,
    params: dict,
    usage: tuple[int, int] | None = None,
) -> ProtectedActionResult:
    """Run one MCP action under ArmorIQ authorization, fully traced.

    Raises ProtectedActionError for every outcome that is not a completed,
    authorized invocation. The protected MCP tool is only reached through
    ``client.invoke``, and only after an intent token was issued for exactly
    this action.
    """
    session = client.start_session(
        SessionOptions(
            mode="proxy",
            default_mcp_name=MCP_SERVER_ID,
            llm=model,
            validity_seconds=INTENT_VALIDITY_SECONDS,
            session_id=request_id,
        )
    )
    trace_status = "error"
    stage = "plan"
    trace_id: str | None = None
    try:
        token = session.start_plan(
            [ToolCall(name=action, args=params)],
            goal=f"TaskMate chat request: {action}",
        )
        ctx = session.active_plan_trace_context
        trace_id = ctx.trace_id if ctx else None
        recorder = session.observability_recorder
        if usage is not None:
            # Token counts only: prompts and completions are not recorded.
            session.record_generation(model=model, input_tokens=usage[0], output_tokens=usage[1])

        stage = "invoke"
        span_id = None
        if recorder is not None and ctx is not None:
            span_id = open_span(
                recorder,
                ctx,
                name="taskmate.mcp.invoke",
                attributes={
                    "requestId": request_id,
                    "mcp": MCP_SERVER_ID,
                    "toolName": action,
                    "argumentNames": sorted(params),
                },
            )
        started = time.perf_counter()
        span_status, span_error = "error", None
        try:
            invocation = client.invoke(
                mcp=MCP_SERVER_ID,
                action=action,
                intent_token=token,
                params=params,
            )
        except PolicyBlockedException as exc:
            span_status, span_error = "denied", "PolicyBlockedException"
            _record_decision(session, "deny", source="proxy", reason=exc.reason or str(exc),
                             enforcement_action="block", action=action, parent_span_id=span_id)
            raise
        except PolicyHoldException as exc:
            span_status, span_error = "denied", "PolicyHoldException"
            _record_decision(session, "hold", source="proxy", reason=str(exc),
                             enforcement_action="hold", action=action, parent_span_id=span_id)
            raise
        except BaseException as exc:
            span_error = type(exc).__name__
            raise
        else:
            # The proxy evaluates policy before forwarding to the MCP server;
            # a non-enforcement response means that evaluation allowed it.
            _record_decision(session, "allow", source="proxy",
                             reason="Proxy enforcement passed; the MCP invocation completed.",
                             enforcement_action="allow", action=action, parent_span_id=span_id)
            span_status = "ok" if invocation.status == "success" else "error"
            if span_status == "error":
                span_error = "MCP tool returned an error result"
        finally:
            if recorder is not None and ctx is not None and span_id:
                try:
                    close_span(recorder, ctx, span_id, status=span_status,
                               duration_ms=(time.perf_counter() - started) * 1000,
                               error_message=span_error)
                except Exception:  # noqa: BLE001
                    logger.warning("Could not close MCP invoke span", exc_info=True)

        trace_status = "ok" if invocation.status == "success" else "error"
        return ProtectedActionResult(invocation=invocation, trace_id=trace_id)
    except (PolicyBlockedException, PolicyHoldException) as exc:
        trace_status = "denied"
        if stage == "plan":
            # Denied at token issuance, before any proxy call.
            ctx = session.active_plan_trace_context
            trace_id = trace_id or (ctx.trace_id if ctx else None)
            decision = "hold" if isinstance(exc, PolicyHoldException) else "deny"
            _record_decision(session, decision, source="sdk",
                             reason=getattr(exc, "reason", None) or str(exc),
                             enforcement_action="hold" if decision == "hold" else "block",
                             action=action, parent_span_id=None)
        raise ProtectedActionError(classify(exc, stage), stage, exc, trace_id) from exc
    except Exception as exc:
        if trace_id is None:
            ctx = session.active_plan_trace_context
            trace_id = ctx.trace_id if ctx else None
        raise ProtectedActionError(classify(exc, stage), stage, exc, trace_id) from exc
    finally:
        schedule_session_close(session, trace_status)
