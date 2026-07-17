import json

import anthropic

from agent.config import MAX_TOKENS, MODEL
from agent.prompt import SYSTEM_PROMPT
from agent.tools import TOOLS, execute_tool
from agent.tracing import tracer

client = anthropic.Anthropic()


def run_agent(messages: list) -> tuple[str, list]:
    """Run one user turn through the tool-calling loop.

    `messages` must end with the latest user message. Returns the assistant's
    reply text and the updated message history.
    """
    with tracer.start_as_current_span("run_agent", openinference_span_kind="agent") as span:
        span.set_input(messages[-1]["content"] if messages else "")

        model_calls = 0
        tools_called = []

        while True:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            model_calls += 1
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tools_called.append(block.name)
                    result = execute_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        reply = "".join(block.text for block in response.content if block.type == "text")

        # Per-turn counters. The loop above has no iteration ceiling, so these are
        # what a loop-detection eval reads to spot a turn that never settles.
        span.set_attribute("agent.model_calls", model_calls)
        span.set_attribute("agent.tool_call_count", len(tools_called))
        span.set_attribute("agent.tools_called", json.dumps(tools_called))
        span.set_output(reply)

        return reply, messages
