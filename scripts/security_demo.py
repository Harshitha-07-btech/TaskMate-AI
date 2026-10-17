"""Live ArmorIQ allow/deny check through the same traced path the chat uses.

Runs one allowed action (list_tasks, read-only) and one prohibited action
(delete_task on the all-zero UUID, which matches no real task). Each runs in
its own ArmorIQ session, so both appear in the ArmorIQ Trace Explorer.
Prints the request (session) ID and trace ID for each, and never prints
credentials or task contents.

    backend\\.venv\\Scripts\\python.exe scripts\\security_demo.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import armoriq_runtime  # noqa: E402
import main  # noqa: E402  (loads backend/.env and builds the ArmorIQ client)

SENTINEL_TASK_ID = "00000000-0000-0000-0000-000000000000"


def run(client, action, params):
    request_id = armoriq_runtime.new_request_id()
    try:
        result = armoriq_runtime.run_protected_action(
            client, request_id=request_id, model="security-demo", action=action, params=params
        )
    except armoriq_runtime.ProtectedActionError as exc:
        return exc.kind, request_id, exc.trace_id, exc.reason
    return ("allowed" if result.invocation.status == "success" else "mcp_tool_error"), request_id, result.trace_id, None


def main_demo():
    client = main.get_armoriq_client()
    outcomes = {
        "list_tasks": run(client, "list_tasks", {"status": "all"}),
        "delete_task": run(client, "delete_task", {"task_id": SENTINEL_TASK_ID}),
    }
    armoriq_runtime.wait_for_telemetry(30.0)

    for action, (outcome, request_id, trace_id, reason) in outcomes.items():
        print(f"{action:12} {outcome:15} session={request_id} trace={trace_id}" + (f" reason={reason}" if reason else ""))

    if outcomes["list_tasks"][0] != "allowed":
        raise SystemExit("FAIL: the allowed list_tasks action did not complete.")
    if outcomes["delete_task"][0] != "policy_denied":
        raise SystemExit("FAIL: ArmorIQ did not deny delete_task (no real task ID was used).")
    print("PASS: list_tasks allowed; delete_task denied by ArmorIQ policy.")


if __name__ == "__main__":
    main_demo()
