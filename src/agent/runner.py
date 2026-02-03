from __future__ import annotations

import json
from typing import Any, Dict, List, Protocol

from .types import AgentResponse, AgentTrace, ToolCall, ToolResult, ToolSpec


class LLMClient(Protocol):
    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        ...


def build_tool_payloads(tool_specs: List[ToolSpec]) -> List[Dict[str, Any]]:
    payloads = []
    for tool in tool_specs:
        payloads.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        )
    return payloads


def run_agent_loop(
    llm_client: LLMClient,
    system_prompt: str,
    user_message: str,
    tool_specs: List[ToolSpec],
    max_steps: int = 20,
    max_tool_calls: int = 20,
    debug: bool = False,
) -> AgentResponse:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tool_payloads = build_tool_payloads(tool_specs)
    tools_by_name = {tool.name: tool for tool in tool_specs}
    trace = AgentTrace()
    tool_calls_used = 0

    for _ in range(max_steps):
        response = llm_client.generate(messages=messages, tools=tool_payloads)
        response_type = response.get("type")

        if response_type == "final":
            return AgentResponse(final=response.get("content"), trace=trace)

        if response_type != "tool_call":
            raise ValueError(f"Unsupported response type: {response_type}")

        if tool_calls_used >= max_tool_calls:
            return AgentResponse(
                final={
                    "status": "tool_budget_exceeded",
                    "tool_calls_used": tool_calls_used,
                },
                trace=trace,
            )

        tool_name = response.get("name")
        if tool_name not in tools_by_name:
            raise ValueError(f"Unknown tool requested: {tool_name}")

        args = response.get("arguments", {}) or {}
        call_id = response.get("id")
        trace.tool_calls.append(ToolCall(name=tool_name, arguments=args, call_id=call_id))
        tool_calls_used += 1

        tool = tools_by_name[tool_name]
        print(f"[tool {tool_calls_used}] {tool_name}")
        if debug:
            print(json.dumps({"event": "tool_call", "name": tool_name, "arguments": args}, ensure_ascii=False))

        output = tool.handler(**args)
        trace.tool_results.append(ToolResult(name=tool_name, arguments=args, output=output, call_id=call_id))
        if debug:
            print(json.dumps({"event": "tool_result", "name": tool_name, "output": output}, ensure_ascii=False, default=str))

        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id or "call_0",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(args)},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id or "call_0",
                "name": tool_name,
                "content": json.dumps(output, ensure_ascii=False),
            }
        )

    raise RuntimeError("Max steps exceeded without final response")

