from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

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
    parser.add_argument("--verbose", action="store_true", help="Print tool calls and tool results.")
    parser.add_argument("--as-of", dest="as_of_date", help="Restrict data to on/before YYYY-MM-DD.")
    args = parser.parse_args()

    system_prompt = load_system_prompt(args.prompt)
    if args.as_of_date:
        system_prompt = (
            f"{system_prompt}\n\nData cutoff: {args.as_of_date}. "
            "Do not use /now endpoints and do not request data after this date."
        )

    tools = NHLTools(as_of_date=args.as_of_date)
    tool_specs = build_tool_specs(tools)
    llm_client = _build_client(args.provider, args.model)

    response = run_agent_loop(
        llm_client=llm_client,
        system_prompt=system_prompt,
        user_message=args.message,
        tool_specs=tool_specs,
        debug=args.verbose,
    )

    output = {"final": response.final, "trace": response.trace}
    results_dir = Path("results")
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"agent_result_{timestamp}"
    json_path = results_dir / f"{stem}.json"
    md_path = results_dir / f"{stem}.md"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, default=lambda o: o.__dict__, indent=2)

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Agent Result\n\n")
        handle.write("## Final\n\n")
        handle.write("```json\n")
        handle.write(json.dumps(response.final, default=lambda o: o.__dict__, indent=2))
        handle.write("\n```\n\n")
        handle.write("## Tool Trace\n\n")
        for idx, call in enumerate(response.trace.tool_calls, start=1):
            handle.write(f"### Tool {idx}: {call.name}\n\n")
            handle.write("Arguments:\n")
            handle.write("```json\n")
            handle.write(json.dumps(call.arguments, indent=2))
            handle.write("\n```\n\n")
            result = response.trace.tool_results[idx - 1].output if idx - 1 < len(response.trace.tool_results) else None
            handle.write("Output:\n")
            handle.write("```json\n")
            handle.write(json.dumps(result, indent=2, default=str))
            handle.write("\n```\n\n")

    print(f"Wrote results to {json_path} and {md_path}")


if __name__ == "__main__":
    main()

