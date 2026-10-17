"""Run live ArmorIQ allow/deny checks; delete uses only an all-zero test UUID."""

import os
from pathlib import Path

from armoriq_sdk import ArmorIQClient, PolicyBlockedException
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")


def invoke(client: ArmorIQClient, action: str, params: dict):
    prompt = f"Security demo: invoke {action} with the supplied test parameters."
    plan = client.capture_plan(
        llm="manual-security-demo",
        prompt=prompt,
        plan={
            "goal": "Verify TaskMate MCP policy enforcement.",
            "steps": [{"action": action, "mcp": "taskmate-mcp", "params": params}],
        },
    )
    token = client.get_intent_token(plan, validity_seconds=300)
    return client.invoke(
        mcp="taskmate-mcp",
        action=action,
        intent_token=token,
        params=params,
    )


def main():
    if not os.getenv("GEMINI_MODEL"):
        os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"
    client = ArmorIQClient.from_config(str(ROOT / "armoriq.yaml"))

    allowed = invoke(client, "list_tasks", {"status": "all"})
    if allowed.status != "success":
        raise SystemExit("Allowed list_tasks action did not succeed.")
    print("ALLOWED: taskmate-mcp.list_tasks returned successfully.")

    sentinel_id = "00000000-0000-0000-0000-000000000000"
    try:
        invoke(client, "delete_task", {"task_id": sentinel_id})
    except PolicyBlockedException:
        print("BLOCKED: ArmorIQ denied taskmate-mcp.delete_task.")
        return
    raise SystemExit("FAIL: ArmorIQ did not block delete_task; no real task ID was used.")


if __name__ == "__main__":
    main()