from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv
from src.agent.llm_clients import AnthropicClient, GeminiClient, OpenAIClient
from src.agent.prompt_loader import load_system_prompt
from src.agent.runner import run_agent_loop
from src.tools.tools import NHLTools, build_tool_specs


def _build_client(provider: str, model: str):
    if provider == "openai":
        return OpenAIClient(model=model, api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))
    if provider == "gemini":
        return GeminiClient(model=model, api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if provider == "anthropic":
        return AnthropicClient(model=model, api_key=os.getenv("ANTHROPIC_API_KEY"))
    raise ValueError(f"Unknown provider: {provider}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the fantasy NHL agent.")
    parser.add_argument("message", help="User message to send to the agent.")
    parser.add_argument("--provider", choices=["openai", "gemini", "anthropic"], required=True)
    parser.add_argument("--model", required=True, help="Model name, e.g., gpt-4o-mini, gemini-1.5-pro.")
    parser.add_argument("--prompt", default="prompts/system_prompt_v0.md")
    args = parser.parse_args()

    system_prompt = load_system_prompt(args.prompt)
    tools = NHLTools()
    tool_specs = build_tool_specs(tools)
    llm_client = _build_client(args.provider, args.model)

    response = run_agent_loop(
        llm_client=llm_client,
        system_prompt=system_prompt,
        user_message=args.message,
        tool_specs=tool_specs,
    )

    output = {"final": response.final, "trace": response.trace}
    print(json.dumps(output, default=lambda o: o.__dict__, indent=2))


if __name__ == "__main__":
    main()

