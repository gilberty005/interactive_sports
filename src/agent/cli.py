from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from src.agent.llm_clients import AnthropicClient, GeminiClient, OpenAIClient
from src.agent.prompt_loader import load_system_prompt
from src.agent.runner import run_agent_loop
from src.tools.tools import DEFAULT_FANTASY_SCORING, NHLTools, build_tool_specs


def _json_dumps(value: object) -> str:
    return json.dumps(value, default=lambda o: o.__dict__, indent=2, ensure_ascii=False)


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... (truncated)"


def _summarize_payload(payload: object) -> str:
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    if isinstance(payload, dict):
        keys = ", ".join(sorted(payload.keys()))
        return f"dict keys: {keys}"
    return f"{type(payload).__name__}"


def _summarize_tool_output(output: object) -> str:
    if not isinstance(output, dict):
        return _summarize_payload(output)
    if "error" in output:
        message = output.get("message")
        return f"error: {output.get('error')}" + (f" ({message})" if message else "")
    if "payload" in output:
        payload = output.get("payload")
        summary = _summarize_payload(payload)
        if isinstance(payload, dict):
            if "games" in payload and isinstance(payload["games"], list):
                summary += f", games: {len(payload['games'])}"
            if "gameWeek" in payload and isinstance(payload["gameWeek"], list):
                summary += f", weeks: {len(payload['gameWeek'])}"
        return summary
    return _summarize_payload(output)


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _compute_next_week(as_of_date: str) -> Tuple[str, str]:
    start = _parse_date(as_of_date) + timedelta(days=1)
    end = start + timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_scoring(scoring_json: str | None, scoring_file: Path | None) -> Dict[str, Any] | None:
    if scoring_json and scoring_file:
        raise ValueError("Use either --scoring-json or --scoring-file, not both.")
    if scoring_json:
        return json.loads(scoring_json)
    if scoring_file:
        data = _load_json(scoring_file)
        if not isinstance(data, dict):
            raise ValueError("scoring file must be a JSON object")
        return data
    return None


def _parse_player_ids(value: str) -> List[int]:
    ids = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _extract_prediction(data: Any) -> Tuple[Dict[str, Any] | None, int | None]:
    if isinstance(data, dict) and isinstance(data.get("final"), dict):
        data = data["final"]
    if not isinstance(data, dict):
        return None, None
    decision = data.get("decision")
    if not isinstance(decision, dict):
        return None, None

    for key in ("player_id", "predicted_player_id", "predicted_top_scorer_id"):
        value = decision.get(key)
        if value is not None:
            return decision, int(value)

    top_candidates = decision.get("top_candidates") or decision.get("candidates")
    if isinstance(top_candidates, list):
        ranked = [c for c in top_candidates if isinstance(c, dict)]
        ranked.sort(key=lambda c: c.get("rank", 9999))
        for candidate in ranked:
            value = candidate.get("player_id")
            if value is not None:
                return decision, int(value)
    return decision, None


def _extract_candidate_ids(data: Any) -> List[int]:
    candidates: List[int] = []
    if isinstance(data, dict) and isinstance(data.get("final"), dict):
        data = data["final"]
    if not isinstance(data, dict):
        return candidates
    decision = data.get("decision")
    if isinstance(decision, dict):
        for key in ("candidate_player_ids", "player_ids"):
            value = decision.get(key)
            if isinstance(value, list):
                candidates.extend(int(item) for item in value)
        top_candidates = decision.get("top_candidates") or decision.get("candidates")
        if isinstance(top_candidates, list):
            for entry in top_candidates:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("player_id")
                if value is not None:
                    candidates.append(int(value))
    return list(dict.fromkeys(candidates))

def _extract_json_from_text(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced_match:
        candidate = fenced_match.group(1)
    else:
        candidate = None
        start = text.find("{")
        if start != -1:
            depth = 0
            for idx in range(start, len(text)):
                char = text[idx]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : idx + 1]
                        break
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_top3_prediction(final: Any) -> List[Dict[str, Any]]:
    if isinstance(final, dict) and isinstance(final.get("prediction"), dict):
        top_3 = final["prediction"].get("top_3")
        if isinstance(top_3, list):
            return [entry for entry in top_3 if isinstance(entry, dict)]
    if isinstance(final, dict) and final.get("status") == "model_invalid_json":
        raw = final.get("raw")
        parsed = _extract_json_from_text(str(raw)) if raw else None
        if parsed and isinstance(parsed.get("prediction"), dict):
            top_3 = parsed["prediction"].get("top_3")
            if isinstance(top_3, list):
                return [entry for entry in top_3 if isinstance(entry, dict)]
    return []


def _parse_toi_to_seconds(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + int(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return 0


def _collect_game_ids_for_week(tools: NHLTools, start_date: str, end_date: str) -> List[int]:
    payload = tools.client.get_json(f"schedule/{start_date}")
    game_ids: List[int] = []
    for day in payload.get("gameWeek", []):
        date_str = (day.get("date") or "")[:10]
        if not (start_date <= date_str <= end_date):
            continue
        for game in day.get("games", []):
            game_id = game.get("id") or game.get("gameId") or game.get("gamePk")
            if game_id is not None:
                game_ids.append(int(game_id))
    return list(dict.fromkeys(game_ids))


def _collect_player_ids_for_week(
    tools: NHLTools, start_date: str, end_date: str, min_toi_seconds: int = 60
) -> List[int]:
    player_ids: List[int] = []
    game_ids = _collect_game_ids_for_week(tools, start_date, end_date)
    for game_id in game_ids:
        boxscore = tools.client.get_json(f"gamecenter/{game_id}/boxscore")
        player_stats = boxscore.get("playerByGameStats", {})
        for team_key in ("homeTeam", "awayTeam"):
            team = player_stats.get(team_key, {})
            for group_key in ("forwards", "defense", "goalies", "skaters"):
                for player in team.get(group_key, []) or []:
                    toi = player.get("toi") or player.get("timeOnIce")
                    if _parse_toi_to_seconds(toi) >= min_toi_seconds:
                        player_id = player.get("playerId") or player.get("id")
                        if player_id is not None:
                            player_ids.append(int(player_id))
    return list(dict.fromkeys(player_ids))


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
    parser.add_argument("message", nargs="?", help="Optional user message to send to the agent.")
    parser.add_argument("--provider", choices=["openai", "gemini", "anthropic"], required=True)
    parser.add_argument("--model", required=True, help="Model name, e.g., gpt-4o-mini, gemini-1.5-pro.")
    parser.add_argument("--prompt", default="prompts/system_prompt_v0.md")
    parser.add_argument("--verbose", action="store_true", help="Print tool calls and tool results.")
    parser.add_argument("--as-of", dest="as_of_date", required=True, help="Set present date as YYYY-MM-DD.")
    parser.add_argument("--scoring-json", help="JSON string of scoring rules to override defaults.")
    parser.add_argument("--scoring-file", help="Path to JSON scoring rules.")
    parser.add_argument("--player-ids", help="Comma-separated list of player IDs for evaluation.")
    parser.add_argument("--player-ids-file", help="JSON file with list of player IDs for evaluation.")
    parser.add_argument("--top-n", type=int, default=5, help="How many players to return in evaluation.")
    args = parser.parse_args()

    system_prompt = load_system_prompt(args.prompt)
    scoring_overrides = _load_scoring(args.scoring_json, Path(args.scoring_file) if args.scoring_file else None)
    scoring_rules = scoring_overrides or dict(DEFAULT_FANTASY_SCORING)
    system_prompt = (
        f"{system_prompt}\n\nData cutoff: {args.as_of_date}. "
        "Do not use /now or /current endpoints and do not request data after this date. "
        "Future schedule data is allowed; future results/stats are not.\n\n"
        f"Fantasy scoring rules (weights): {json.dumps(scoring_rules)}\n"
        "When predicting, include the numeric player_id for the predicted player and "
        "include a candidate_player_ids list used for ranking."
    )

    tools = NHLTools(as_of_date=args.as_of_date)
    tool_specs = build_tool_specs(tools, include_eval_tools=False)
    llm_client = _build_client(args.provider, args.model)

    start_date, end_date = _compute_next_week(args.as_of_date)
    user_message = (
        args.message
        or f"Predict the best fantasy player for {start_date} to {end_date} using the scoring rules provided."
    )

    response = run_agent_loop(
        llm_client=llm_client,
        system_prompt=system_prompt,
        user_message=user_message,
        tool_specs=tool_specs,
        debug=args.verbose,
    )

    predicted_decision, predicted_player_id = _extract_prediction(response.final)
    if args.player_ids:
        candidate_player_ids = _parse_player_ids(args.player_ids)
    elif args.player_ids_file:
        candidate_player_ids = _load_json(Path(args.player_ids_file))
    else:
        candidate_player_ids = _extract_candidate_ids(response.final)

    evaluation: Dict[str, Any] | None = None
    eval_tools = NHLTools(as_of_date=None)
    if not candidate_player_ids:
        ground_truth = eval_tools.fantasy_best_players_week_from_games(
            start_date=start_date,
            end_date=end_date,
            scoring=scoring_rules,
            top_n=args.top_n,
        )
        candidate_source = "boxscore_aggregation"
    else:
        ground_truth = eval_tools.fantasy_best_players_week(
            player_ids=[int(pid) for pid in candidate_player_ids],
            start_date=start_date,
            end_date=end_date,
            scoring=scoring_rules,
            top_n=args.top_n,
        )
        candidate_source = "user_supplied"

    if "error" in ground_truth:
        evaluation = {"error": ground_truth}
    else:
        predicted_points = None
        predicted_rank = None
        if predicted_player_id is not None:
            for idx, entry in enumerate(ground_truth.get("results", []), start=1):
                if entry.get("player_id") == predicted_player_id:
                    predicted_rank = idx
                    predicted_points = entry.get("fantasy_points")
                    break
        results = ground_truth.get("results", [])
        best_entry = (results or [{}])[0]
        actual_points_by_player = {
            entry.get("player_id"): entry.get("fantasy_points") for entry in results if isinstance(entry, dict)
        }
        predicted_top3 = _extract_top3_prediction(response.final)
        predicted_top3_with_actual = []
        for entry in predicted_top3[:3]:
            player_id = entry.get("player_id")
            predicted_top3_with_actual.append(
                {
                    "player_id": player_id,
                    "player_name": entry.get("player_name") or entry.get("player_id"),
                    "predicted_fantasy_points": entry.get("predicted_fantasy_points"),
                    "actual_fantasy_points": actual_points_by_player.get(player_id),
                }
            )
        evaluation = {
            "start_date": start_date,
            "end_date": end_date,
            "scoring": ground_truth.get("scoring"),
            "candidate_count": len(results) if results else len(candidate_player_ids),
            "candidate_source": candidate_source,
            "prediction": {
                "player_id": predicted_player_id,
                "decision": predicted_decision,
                "fantasy_points": predicted_points,
                "rank": predicted_rank,
                "top_3": predicted_top3_with_actual,
            },
            "ground_truth": {
                "best_player_id": best_entry.get("player_id"),
                "best_fantasy_points": best_entry.get("fantasy_points"),
                "top_n": ground_truth.get("top_results", results[: args.top_n]),
            },
            "evaluation": {
                "correct_best": predicted_player_id == best_entry.get("player_id"),
                "prediction_rank": predicted_rank,
            },
        }

    output = {"final": response.final, "trace": response.trace, "evaluation": evaluation}
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
        handle.write("## Summary\n\n")
        if isinstance(response.final, dict):
            status = response.final.get("status")
            if status:
                handle.write(f"- Status: {status}\n")
            error = response.final.get("error")
            if error:
                handle.write(f"- Error: {error}\n")
            handle.write("\n")
        if evaluation is None:
            handle.write("- Evaluation: not run\n\n")
        elif "error" in evaluation:
            handle.write("- Evaluation: error\n\n")
        else:
            correct = evaluation.get("evaluation", {}).get("correct_best")
            handle.write(f"- Evaluation: correct_best={correct}\n")
            predicted_top3 = evaluation.get("prediction", {}).get("top_3", [])
            actual_top3 = evaluation.get("ground_truth", {}).get("top_n", [])
            if predicted_top3:
                handle.write("- Predicted Top 3 (predicted vs actual):\n")
                for idx, pred in enumerate(predicted_top3[:3], start=1):
                    pred_name = pred.get("player_name") or pred.get("player_id") or ""
                    pred_fp = pred.get("predicted_fantasy_points", "")
                    actual_fp = pred.get("actual_fantasy_points", "")
                    handle.write(f"  {idx}. {pred_name} — {pred_fp} (actual {actual_fp})\n")
            if actual_top3:
                handle.write("- Actual Top 3:\n")
                for idx, act in enumerate(actual_top3[:3], start=1):
                    act_name = None
                    if isinstance(act.get("name"), dict):
                        act_name = act.get("name", {}).get("default")
                    act_name = act_name or act.get("player_id") or ""
                    act_fp = act.get("fantasy_points", "")
                    handle.write(f"  {idx}. {act_name} — {act_fp}\n")
            handle.write("\n")
            # No separate Prediction vs Actual section; summary contains the comparison.

        handle.write("## Final\n\n")
        if isinstance(response.final, dict) and "prediction" in response.final:
            handle.write("### Prediction\n\n")
            handle.write("```json\n")
            handle.write(_json_dumps(response.final.get("prediction")))
            handle.write("\n```\n\n")

        if isinstance(response.final, dict) and "reasoning" in response.final:
            reasoning = response.final.get("reasoning")
            handle.write("### Reasoning\n\n")
            if isinstance(reasoning, list):
                for item in reasoning:
                    handle.write(f"- {item}\n")
                handle.write("\n")
            else:
                handle.write("```json\n")
                handle.write(_json_dumps(reasoning))
                handle.write("\n```\n\n")

        if isinstance(response.final, dict) and "reasoning" not in response.final and "raw" in response.final:
            handle.write("### Reasoning (from model output, truncated)\n\n")
            handle.write("```text\n")
            handle.write(_truncate(str(response.final.get("raw"))))
            handle.write("\n```\n\n")

        if isinstance(response.final, dict) and "data_used" in response.final:
            data_used = response.final.get("data_used")
            handle.write("### Data Used\n\n")
            tool_calls = data_used.get("tool_calls") if isinstance(data_used, dict) else None
            if isinstance(tool_calls, list):
                handle.write("| Tool | Path | Date Coverage | Notes |\n")
                handle.write("| --- | --- | --- | --- |\n")
                for entry in tool_calls:
                    if not isinstance(entry, dict):
                        continue
                    tool = entry.get("tool", "")
                    path = entry.get("path_template") or entry.get("path") or ""
                    date_coverage = entry.get("date_coverage", "")
                    notes = entry.get("notes", "")
                    handle.write(f"| {tool} | {path} | {date_coverage} | {notes} |\n")
                handle.write("\n")
            else:
                handle.write("```json\n")
                handle.write(_json_dumps(data_used))
                handle.write("\n```\n\n")

        if not isinstance(response.final, dict) or "decision" not in response.final:
            handle.write("### Final (raw JSON)\n\n")
            handle.write("```json\n")
            handle.write(_json_dumps(response.final))
            handle.write("\n```\n\n")

        if evaluation:
            handle.write("## Evaluation\n\n")
            handle.write("```json\n")
            handle.write(_json_dumps(evaluation))
            handle.write("\n```\n\n")

        handle.write("## Tool Trace (summary)\n\n")
        for idx, call in enumerate(response.trace.tool_calls, start=1):
            handle.write(f"### Tool {idx}: {call.name}\n\n")
            handle.write("Arguments:\n")
            handle.write("```json\n")
            handle.write(_truncate(_json_dumps(call.arguments)))
            handle.write("\n```\n\n")
            result = response.trace.tool_results[idx - 1].output if idx - 1 < len(response.trace.tool_results) else None
            handle.write("Output summary:\n")
            handle.write("```text\n")
            handle.write(_summarize_tool_output(result))
            handle.write("\n```\n\n")
        handle.write("Full outputs are saved in the JSON result file.\n")

    print(f"Wrote results to {json_path} and {md_path}")


if __name__ == "__main__":
    main()

