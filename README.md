# Fantasy Agent Eval

NHL fantasy prediction and evaluation system focused on fantasy scoring outcomes.

## Structure

- `prompts/system_prompt_v0.md`: system prompt and output schema.
- `src/agent/`: CLI, runner, and LLM client wiring.
- `src/tools/`: NHL API tools and fantasy scoring rules.
- `src/data/`: cache and normalization helpers.
- `tests/`: smoke tests for tool calls.

## Setup

```
pip install -e .[dev]
```

## Run the agent

```
pip install -e .[llm]
export ANTHROPIC_API_KEY=...
python -m src.agent.cli --provider anthropic --model claude-sonnet-4-5-20250929 --as-of 2018-01-15 --verbose
```

## Notes
- Tool calls use the NHL Stats API and cache under `.cache/nhl_api/`.
- The agent loop in `src/agent/runner.py` is provider-agnostic.
- Fantasy scoring rules live in `src/tools/tools.py` (`DEFAULT_FANTASY_SCORING`).
- Rules are applied by the tools during evaluation; update the defaults there to change baseline scoring.
