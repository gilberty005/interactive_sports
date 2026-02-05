from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.tools.tools import NHLTools


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _parse_player_ids(value: str) -> List[int]:
    ids = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _load_player_ids(path: Path) -> List[int]:
    data = _load_json(path)
    if isinstance(data, list):
        return [int(item) for item in data]
    raise ValueError("player_ids file must be a JSON list of integers")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate fantasy predictions for a given week.")
    parser.add_argument("--prediction-file", required=True, help="Path to agent_result_*.json")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--player-ids", help="Comma-separated list of player IDs")
    parser.add_argument("--player-ids-file", help="JSON file with list of player IDs")
    parser.add_argument("--scoring-json", help="JSON string of scoring rules")
    parser.add_argument("--scoring-file", help="Path to JSON scoring rules")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--as-of", dest="as_of_date", help="Restrict data to on/before YYYY-MM-DD.")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    prediction_path = Path(args.prediction_file)
    prediction_data = _load_json(prediction_path)
    decision, predicted_player_id = _extract_prediction(prediction_data)

    player_ids: List[int] = []
    if args.player_ids:
        player_ids = _parse_player_ids(args.player_ids)
    elif args.player_ids_file:
        player_ids = _load_player_ids(Path(args.player_ids_file))
    else:
        player_ids = _extract_candidate_ids(prediction_data)

    if not player_ids:
        raise SystemExit(
            "No player IDs provided. Use --player-ids or --player-ids-file, "
            "or include candidate_player_ids in the prediction JSON."
        )

    scoring_rules = _load_scoring(args.scoring_json, Path(args.scoring_file) if args.scoring_file else None)

    tools = NHLTools(as_of_date=args.as_of_date)
    if player_ids:
        ground_truth = tools.fantasy_best_players_week(
            player_ids=player_ids,
            start_date=args.start_date,
            end_date=args.end_date,
            scoring=scoring_rules,
            top_n=args.top_n,
        )
    else:
        ground_truth = tools.fantasy_best_players_week_from_games(
            start_date=args.start_date,
            end_date=args.end_date,
            scoring=scoring_rules,
            top_n=args.top_n,
        )

    if "error" in ground_truth:
        output = {
            "prediction_file": str(prediction_path),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "error": ground_truth,
        }
    else:
        predicted_points = None
        predicted_rank = None
        results = ground_truth.get("results", [])
        if predicted_player_id is not None:
            for idx, entry in enumerate(results, start=1):
                if entry.get("player_id") == predicted_player_id:
                    predicted_rank = idx
                    predicted_points = entry.get("fantasy_points")
                    break

        best_entry = (results or [{}])[0]
        output = {
            "prediction_file": str(prediction_path),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "scoring": ground_truth.get("scoring"),
            "candidate_count": len(results) if results else len(player_ids),
            "prediction": {
                "player_id": predicted_player_id,
                "decision": decision,
                "fantasy_points": predicted_points,
                "rank": predicted_rank,
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

    if args.output:
        output_path = Path(args.output)
    else:
        results_dir = Path("results")
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = results_dir / f"fantasy_eval_{stamp}.json"

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
    print(f"Wrote evaluation to {output_path}")


if __name__ == "__main__":
    main()

