from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _parse_json(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Model did not return valid JSON") from exc


def _build_openai_tools(tool_specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"type": "function", "function": tool} for tool in tool_specs]


class OpenAIClient:
    def __init__(self, model: str, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=_build_openai_tools(tools),
            tool_choice="auto",
        )
        message = response.choices[0].message
        if message.tool_calls:
            if len(message.tool_calls) != 1:
                raise ValueError("Multiple tool calls are not supported in v0")
            tool_call = message.tool_calls[0]
            return {
                "type": "tool_call",
                "name": tool_call.function.name,
                "arguments": json.loads(tool_call.function.arguments or "{}"),
                "id": tool_call.id,
            }
        if not message.content:
            raise ValueError("No content returned by model")
        return {"type": "final", "content": _parse_json(message.content)}


class AnthropicClient:
    def __init__(self, model: str, api_key: Optional[str] = None) -> None:
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key)
        self.model = model

    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        system_text = ""
        anthropic_messages: List[Dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                system_text = message.get("content", "")
                continue

            if role == "assistant" and message.get("tool_calls"):
                content_blocks = []
                for call in message["tool_calls"]:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id", "call_0"),
                            "name": call.get("function", {}).get("name"),
                            "input": json.loads(call.get("function", {}).get("arguments", "{}")),
                        }
                    )
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
                continue

            if role == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id", "call_0"),
                                "content": message.get("content", ""),
                            }
                        ],
                    }
                )
                continue

            anthropic_messages.append({"role": role, "content": message.get("content", "")})

        response = self.client.messages.create(
            model=self.model,
            system=system_text or None,
            messages=anthropic_messages,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            max_tokens=1024,
        )

        if response.stop_reason == "tool_use":
            tool_block = next(block for block in response.content if block.type == "tool_use")
            return {
                "type": "tool_call",
                "name": tool_block.name,
                "arguments": tool_block.input,
                "id": tool_block.id,
            }

        content_text = "".join(block.text for block in response.content if block.type == "text")
        return {"type": "final", "content": _parse_json(content_text)}


class GeminiClient:
    def __init__(self, model: str, api_key: Optional[str] = None) -> None:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self.genai = genai
        self.model = model

    def _build_tools(self, tools: List[Dict[str, Any]]) -> List[Any]:
        function_declarations = []
        for tool in tools:
            function_declarations.append(
                self.genai.types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool["parameters"],
                )
            )
        return [self.genai.types.Tool(function_declarations=function_declarations)]

    def _to_contents(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                contents.append({"role": "user", "parts": [{"text": message.get("content", "")}]})
                continue

            if role == "assistant" and message.get("tool_calls"):
                parts = []
                for call in message["tool_calls"]:
                    parts.append(
                        {
                            "function_call": {
                                "name": call.get("function", {}).get("name"),
                                "args": json.loads(call.get("function", {}).get("arguments", "{}")),
                            }
                        }
                    )
                contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": message.get("name"),
                                    "response": json.loads(message.get("content", "{}")),
                                }
                            }
                        ],
                    }
                )
                continue

            contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": message.get("content", "")}]})
        return contents

    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        model = self.genai.GenerativeModel(model_name=self.model, tools=self._build_tools(tools))
        response = model.generate_content(self._to_contents(messages))
        candidate = response.candidates[0]
        parts = candidate.content.parts
        for part in parts:
            if "function_call" in part:
                call = part["function_call"]
                return {
                    "type": "tool_call",
                    "name": call["name"],
                    "arguments": call.get("args", {}),
                    "id": call.get("id"),
                }
        text = "".join(part.get("text", "") for part in parts)
        return {"type": "final", "content": _parse_json(text)}

