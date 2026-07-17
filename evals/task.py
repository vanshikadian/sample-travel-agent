"""Run one case through the agent and record what happened.

Detectors need the reply *and* the tool results side by side, "the agent asserted
something its tools never returned" can't be checked from the reply alone.

Calls run_agent directly rather than via HTTP. The API path is covered by
scripts/generate_traffic.py; here a web server between us and the thing being
measured adds variance without signal. Same run_agent either way, both traced.
"""

import json
from typing import Any

from agent.loop import run_agent


def _extract_tool_calls(messages: list) -> list[dict]:
    """Pull tool calls and their results out of a finished message history.

    The call and its result live in different messages, joined by tool_use_id, so
    match them back up. Order is preserved, loop detection counts how many times a
    tool ran, not just which ones.
    """
    results_by_id: dict[str, Any] = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                raw = block.get("content")
                try:
                    results_by_id[block["tool_use_id"]] = json.loads(raw)
                except (TypeError, ValueError):
                    results_by_id[block["tool_use_id"]] = raw

    calls = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if getattr(block, "type", None) == "tool_use":
                calls.append(
                    {
                        "name": block.name,
                        "input": dict(block.input),
                        "result": results_by_id.get(block.id),
                    }
                )
    return calls


def run_case(example: dict) -> dict:
    """Send a case's messages in order and return one flat record of the turn.

    `example` is a Phoenix dataset example: input.messages is a list of user
    messages making up a single conversation.
    """
    user_messages = example["input"]["messages"]

    messages: list = []
    replies: list[str] = []
    for user_message in user_messages:
        messages.append({"role": "user", "content": user_message})
        reply, messages = run_agent(messages)
        replies.append(reply)

    return {
        "reply": replies[-1],
        "replies": replies,
        "user_messages": user_messages,
        "tool_calls": _extract_tool_calls(messages),
    }
