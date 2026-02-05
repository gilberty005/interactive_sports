from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

from src.data.normalize import normalize_team_abbrev, season_id_from_date
from src.tools.nhl_api import NHLApiClient, NHLStatsApiClient
from src.agent.types import ToolSpec

# ---- Tool catalog (treat as data / part of the agent's environment) ----
# NOTE: This is intentionally a *curated* catalog of allowed endpoints.
# The agent can browse this catalog and then call `nhl_api_call` to hit any of them.
# You can expand this over time (including NHL EDGE endpoints) without changing tool interfaces.
DEFAULT_ENDPOINT_CATALOG: List[Dict[str, Any]] = [
    {
        "name": "player_search",
        "base": "web",
        "path": "player-search/{name}",
        "params_schema": {"name": "string"},
        "category": "identity",
        "cost": 1,
        "description": "Search NHL players by name; returns candidate matches.",
    },
    {
        "name": "player_landing",
        "base": "web",
        "path": "player/{player_id}/landing",
        "params_schema": {"player_id": "int"},
        "category": "identity",
        "cost": 1,
        "description": "Player metadata/landing page payload.",
    },
    {
        "name": "club_schedule_season",
        "base": "web",
        "path": "club-schedule-season/{team_abbrev}/{season_id}",
        "params_schema": {"team_abbrev": "string", "season_id": "string"},
        "category": "schedule",
        "cost": 1,
        "description": "Team season schedule (filter client-side by date range).",
    },
    {
        "name": "club_schedule_week",
        "base": "web",
        "path": "club-schedule-week/{team_abbrev}/{date}",
        "params_schema": {"team_abbrev": "string", "date": "YYYY-MM-DD"},
        "category": "schedule",
        "cost": 1,
        "description": "Team schedule for week containing date.",
    },
    {
        "name": "scoreboard",
        "base": "web",
        "path": "scoreboard/{date}",
        "params_schema": {"date": "YYYY-MM-DD"},
        "category": "games",
        "cost": 1,
        "description": "Daily scoreboard for a date.",
    },
    {
        "name": "scoreboard_now",
        "base": "web",
        "path": "scoreboard/now",
        "params_schema": {},
        "category": "games",
        "cost": 1,
        "description": "Current scoreboard.",
    },
    {
        "name": "game_boxscore",
        "base": "web",
        "path": "gamecenter/{game_id}/boxscore",
        "params_schema": {"game_id": "int"},
        "category": "games",
        "cost": 2,
        "description": "Boxscore payload for a game.",
    },
    {
        "name": "player_gamelog_season",
        "base": "web",
        "path": "player/{player_id}/game-log/{season_id}/2",
        "params_schema": {"player_id": "int", "season_id": "string"},
        "category": "gamelog",
        "cost": 2,
        "description": "Skater game log for season (gameType=2 regular season).",
    },
    {
        "name": "edge_skater_detail",
        "base": "web",
        "path": "edge/skater-detail/{season_id}/{game_type_id}",
        "params_schema": {"season_id": "string", "game_type_id": "int"},
        "category": "edge",
        "cost": 4,
        "description": "NHL EDGE skater detail table (speed/distance/shot/zone metrics).",
    },
    {
        "name": "edge_goalie_detail",
        "base": "web",
        "path": "edge/goalie-detail/{season_id}/{game_type_id}",
        "params_schema": {"season_id": "string", "game_type_id": "int"},
        "category": "edge",
        "cost": 4,
        "description": "NHL EDGE goalie detail table.",
    },
    {
        "name": "stats_team_report",
        "base": "stats",
        "path": "en/team/{report}",
        "params_schema": {"report": "string", "cayenneExp": "string", "sort": "string?", "limit": "int?"},
        "category": "stats",
        "cost": 3,
        "description": "Stats REST reporting endpoint for team reports (Cayenne).",
    },
]


def _load_json_catalog(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"Catalog at {path} must be a list of endpoints.")
    return data


def _merge_catalogs(
    base_catalog: List[Dict[str, Any]],
    overrides: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged = [dict(item) for item in base_catalog]
    index_by_path = {item.get("path"): i for i, item in enumerate(merged) if item.get("path")}
    index_by_name = {item.get("name"): i for i, item in enumerate(merged) if item.get("name")}

    for override in overrides:
        if not isinstance(override, dict):
            continue
        key = override.get("path")
        idx = index_by_path.get(key) if key else None
        if idx is None:
            key = override.get("name")
            idx = index_by_name.get(key) if key else None

        if idx is None:
            merged.append(dict(override))
            continue

        updated = dict(merged[idx])
        for field, value in override.items():
            if (
                field == "params_schema"
                and isinstance(value, dict)
                and isinstance(updated.get("params_schema"), dict)
            ):
                params_schema = dict(updated["params_schema"])
                for subfield, subvalue in value.items():
                    if isinstance(subvalue, dict) and isinstance(params_schema.get(subfield), dict):
                        params_schema[subfield] = {**params_schema[subfield], **subvalue}
                    else:
                        params_schema[subfield] = subvalue
                updated["params_schema"] = params_schema
            else:
                updated[field] = value
        merged[idx] = updated

    return merged


def load_endpoint_catalog() -> List[Dict[str, Any]]:
    base_dir = Path(__file__).resolve().parent
    generated_path = base_dir / "endpoint_catalog.generated.json"
    catalog_path = base_dir / "endpoint_catalog.json"
    overrides_path = base_dir / "endpoint_catalog.overrides.json"

    if generated_path.exists():
        catalog = _load_json_catalog(generated_path)
    elif catalog_path.exists():
        catalog = _load_json_catalog(catalog_path)
    else:
        catalog = list(DEFAULT_ENDPOINT_CATALOG)

    if overrides_path.exists():
        overrides = _load_json_catalog(overrides_path)
        catalog = _merge_catalogs(catalog, overrides)

    return catalog


ENDPOINT_CATALOG: List[Dict[str, Any]] = load_endpoint_catalog()


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_FANTASY_SCORING: Dict[str, float] = {
    "goals": 2.0,
    "assists": 1.0,
    "shots": 0.1,
    "points": 0.0,
    "powerPlayPoints": 0.5,
    "shortHandedPoints": 1.0,
    "plusMinus": 0.0,
    "pim": 0.0,
    "hits": 0.0,
    "blocks": 0.0,
    "wins": 3.0,
    "saves": 0.2,
    "goalsAgainst": -1.0,
    "shutouts": 2.0,
}

FANTASY_STAT_ALIASES: Dict[str, str] = {
    "ppp": "powerPlayPoints",
    "power_play_points": "powerPlayPoints",
    "shp": "shortHandedPoints",
    "short_handed_points": "shortHandedPoints",
    "plus_minus": "plusMinus",
    "penalty_minutes": "pim",
    "pim": "pim",
    "shots_on_goal": "shots",
    "sog": "shots",
    "goals_against": "goalsAgainst",
}


def _parse_date(value: str) -> date | None:
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_toi_to_seconds(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + int(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    except ValueError:
        return 0
    return 0


def _normalize_scoring_rules(scoring: Dict[str, Any] | None) -> Dict[str, float] | None:
    if scoring is None:
        return dict(DEFAULT_FANTASY_SCORING)
    if not isinstance(scoring, dict):
        return None
    normalized = dict(DEFAULT_FANTASY_SCORING)
    for key, value in scoring.items():
        stat = FANTASY_STAT_ALIASES.get(str(key), str(key))
        weight = _coerce_float(value)
        if weight is None:
            return None
        normalized[stat] = weight
    return normalized


def _format_path(path_template: str, path_params: Dict[str, Any]) -> str:
    """Format a catalog path template with path params.

    Only replaces `{key}` tokens present in the template.
    """
    out = path_template
    for k, v in path_params.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class NHLTools:
    def __init__(
        self,
        client: NHLApiClient | None = None,
        stats_client: NHLStatsApiClient | None = None,
        as_of_date: str | None = None,
    ) -> None:
        self.client = client or NHLApiClient()
        self.stats_client = stats_client or NHLStatsApiClient()
        self.as_of_date = as_of_date

    def _allow_future_dates(self, path_template: str) -> bool:
        entry = next((item for item in ENDPOINT_CATALOG if item.get("path") == path_template), None)
        return (entry or {}).get("category") == "schedule"

    def _enforce_as_of_date(
        self, path_template: str, path_params: Dict[str, Any], query_params: Dict[str, Any]
    ) -> Dict[str, Any] | None:
        if not self.as_of_date:
            return None

        cutoff = _parse_date(self.as_of_date)
        if cutoff is None:
            return {
                "error": "invalid_as_of_date",
                "message": "as_of_date must be YYYY-MM-DD",
                "as_of_date": self.as_of_date,
            }

        if self._allow_future_dates(path_template):
            return None

        if (
            "/now" in path_template
            or path_template.endswith("now")
            or "/current" in path_template
            or path_template.endswith("current")
        ):
            return {
                "error": "as_of_date_violation",
                "message": "Endpoints using /now or /current are not allowed when as_of_date is set.",
                "path_template": path_template,
                "as_of_date": self.as_of_date,
            }

        game_id = path_params.get("game_id") if isinstance(path_params, dict) else None
        if game_id and "gamecenter/" in path_template:
            game_date = self._get_game_date_for_game_id(str(game_id))
            if game_date is None:
                return {
                    "error": "as_of_date_violation",
                    "message": "Unable to validate game date for game_id.",
                    "as_of_date": self.as_of_date,
                    "game_id": game_id,
                }
            if game_date > cutoff:
                return {
                    "error": "as_of_date_violation",
                    "message": "game_id is after as_of_date.",
                    "as_of_date": self.as_of_date,
                    "game_id": game_id,
                    "game_date": game_date.isoformat(),
            }

        for source_name, params in (("path_params", path_params), ("query_params", query_params)):
            for key, value in params.items():
                parsed = _parse_date(str(value))
                if parsed and parsed > cutoff:
                    return {
                        "error": "as_of_date_violation",
                        "message": f"{source_name}.{key} is after as_of_date.",
                        "as_of_date": self.as_of_date,
                        "value": value,
                    }

        return None

    def _get_game_date_for_game_id(self, game_id: str) -> date | None:
        try:
            payload = self.client.get_json(f"gamecenter/{game_id}/landing")
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("gameDate", "startTimeUTC", "gameTimeUTC"):
            value = payload.get(key)
            if not value:
                continue
            parsed = _parse_date(str(value)[:10])
            if parsed:
                return parsed
        return None

    def _validate_scoring_window(self, start_date: str, end_date: str) -> Dict[str, Any] | None:
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if not start or not end:
            return {
                "error": "invalid_date",
                "message": "start_date and end_date must be YYYY-MM-DD",
                "start_date": start_date,
                "end_date": end_date,
            }
        if end < start:
            return {
                "error": "invalid_date_range",
                "message": "end_date must be on/after start_date",
                "start_date": start_date,
                "end_date": end_date,
            }
        if self.as_of_date:
            cutoff = _parse_date(self.as_of_date)
            if cutoff and end > cutoff:
                return {
                    "error": "as_of_date_violation",
                    "message": "end_date is after as_of_date.",
                    "as_of_date": self.as_of_date,
                    "end_date": end_date,
                }
        return None

    def _collect_game_ids_for_week(self, start_date: str, end_date: str) -> List[int]:
        payload = self.client.get_json(f"schedule/{start_date}")
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

    def _filter_payload_as_of(self, payload: Any, path_template: str) -> Any:
        if not self.as_of_date:
            return payload
        if self._allow_future_dates(path_template):
            return payload
        cutoff = _parse_date(self.as_of_date)
        if cutoff is None:
            return payload

        def filter_value(value: Any) -> Any:
            if isinstance(value, list):
                return filter_list(value)
            if isinstance(value, dict):
                return {k: filter_value(v) for k, v in value.items()}
            return value

        def item_date(item: Dict[str, Any]) -> date | None:
            for key in ("gameDate", "date", "gameDay", "startDate", "endDate"):
                if key in item:
                    parsed = _parse_date(str(item[key])[:10])
                    if parsed:
                        return parsed
            return None

        def filter_list(items: List[Any]) -> List[Any]:
            filtered: List[Any] = []
            for entry in items:
                if isinstance(entry, dict):
                    parsed = item_date(entry)
                    if parsed and parsed > cutoff:
                        continue
                    filtered.append(filter_value(entry))
                elif isinstance(entry, str):
                    parsed = _parse_date(entry[:10])
                    if parsed and parsed > cutoff:
                        continue
                    filtered.append(entry)
                else:
                    filtered.append(filter_value(entry))
            return filtered

        return filter_value(payload)

    def nhl_api_list_endpoints(self, category: str | None = None) -> Dict[str, Any]:
        """Return the allowed endpoint catalog for agent discovery."""
        if category:
            filtered = [e for e in ENDPOINT_CATALOG if e.get("category") == category]
            return {"endpoints": filtered}
        return {"endpoints": ENDPOINT_CATALOG}

    def nhl_api_call(
        self,
        base: str,
        path_template: str,
        path_params: Dict[str, Any] | None = None,
        query_params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Generic caller for any *allowed* endpoint.

        - `base` must be one of: "web", "stats"
        - `path_template` should match an entry in ENDPOINT_CATALOG `path`.
        - `path_params` are substituted into `{}` tokens.
        - `query_params` are passed as URL query parameters.

        Returns a JSON payload plus basic call metadata.
        """
        path_params = path_params or {}
        query_params = query_params or {}

        as_of_error = self._enforce_as_of_date(path_template, path_params, query_params)
        if as_of_error:
            return as_of_error

        allowed_templates = {e["path"] for e in ENDPOINT_CATALOG}
        if path_template not in allowed_templates:
            return {
                "error": "path_template_not_allowed",
                "message": "This endpoint is not in the allowed catalog.",
                "path_template": path_template,
            }

        path = _format_path(path_template, path_params)

        if base == "web":
            payload = self.client.get_json(path, params=query_params)
        elif base == "stats":
            payload = self.stats_client.get_json(path, params=query_params)
        else:
            return {
                "error": "invalid_base",
                "message": "base must be one of: 'web', 'stats'",
                "base": base,
            }

        return {
            "base": base,
            "path": path,
            "path_template": path_template,
            "path_params": path_params,
            "query_params": query_params,
            "payload": self._filter_payload_as_of(payload, path_template),
        }

    def fantasy_score_player_week(
        self,
        player_id: int,
        start_date: str,
        end_date: str,
        scoring: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        scoring_rules = _normalize_scoring_rules(scoring)
        if scoring_rules is None:
            return {
                "error": "invalid_scoring_rules",
                "message": "scoring must be a JSON object of numeric weights",
                "scoring": scoring,
            }

        date_error = self._validate_scoring_window(start_date, end_date)
        if date_error:
            return date_error

        season_id = season_id_from_date(end_date)
        payload = self.client.get_json(f"player/{player_id}/game-log/{season_id}/2")
        splits = payload.get("gameLog", payload.get("games", []))

        totals = {stat: 0.0 for stat in scoring_rules}
        total_points = 0.0
        games = []

        for split in splits:
            game_date = (split.get("gameDate") or split.get("date") or "")[:10]
            if not (start_date <= game_date <= end_date):
                continue
            stat_line = split.get("stat", split)
            game_stats = {}
            for stat, weight in scoring_rules.items():
                value = _coerce_float(stat_line.get(stat))
                if value is None:
                    value = 0.0
                game_stats[stat] = value
                totals[stat] += value
                total_points += value * weight
            games.append(
                {
                    "date": game_date,
                    "opponent": split.get("opponent") or split.get("opponentAbbrev"),
                    "game_id": split.get("gameId") or split.get("gamePk"),
                    "stats": game_stats,
                }
            )

        return {
            "player_id": player_id,
            "start_date": start_date,
            "end_date": end_date,
            "games_played": len(games),
            "fantasy_points": round(total_points, 3),
            "stat_totals": {k: round(v, 3) for k, v in totals.items()},
            "scoring": scoring_rules,
            "games": games,
        }

    def fantasy_best_players_week(
        self,
        player_ids: List[int],
        start_date: str,
        end_date: str,
        scoring: Dict[str, Any] | None = None,
        top_n: int = 10,
    ) -> Dict[str, Any]:
        scoring_rules = _normalize_scoring_rules(scoring)
        if scoring_rules is None:
            return {
                "error": "invalid_scoring_rules",
                "message": "scoring must be a JSON object of numeric weights",
                "scoring": scoring,
            }

        date_error = self._validate_scoring_window(start_date, end_date)
        if date_error:
            return date_error

        results = []
        for player_id in player_ids:
            result = self.fantasy_score_player_week(
                player_id=player_id,
                start_date=start_date,
                end_date=end_date,
                scoring=scoring_rules,
            )
            if "error" in result:
                continue
            results.append(result)

        results.sort(key=lambda r: r.get("fantasy_points", 0), reverse=True)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "scoring": scoring_rules,
            "top_n": top_n,
            "results": results[: max(1, top_n)],
        }

    def fantasy_best_players_week_from_games(
        self,
        start_date: str,
        end_date: str,
        scoring: Dict[str, Any] | None = None,
        top_n: int = 10,
        min_toi_seconds: int = 60,
    ) -> Dict[str, Any]:
        scoring_rules = _normalize_scoring_rules(scoring)
        if scoring_rules is None:
            return {
                "error": "invalid_scoring_rules",
                "message": "scoring must be a JSON object of numeric weights",
                "scoring": scoring,
            }

        date_error = self._validate_scoring_window(start_date, end_date)
        if date_error:
            return date_error

        game_ids = self._collect_game_ids_for_week(start_date, end_date)
        player_totals: Dict[int, Dict[str, Any]] = {}

        for game_id in game_ids:
            boxscore = self.client.get_json(f"gamecenter/{game_id}/boxscore")
            player_stats = boxscore.get("playerByGameStats", {})
            for team_key in ("homeTeam", "awayTeam"):
                team = player_stats.get(team_key, {})
                for group_key in ("forwards", "defense", "goalies", "skaters"):
                    for player in team.get(group_key, []) or []:
                        player_id = player.get("playerId") or player.get("id")
                        if player_id is None:
                            continue
                        toi = player.get("toi") or player.get("timeOnIce")
                        if _parse_toi_to_seconds(toi) < min_toi_seconds:
                            continue
                        entry = player_totals.setdefault(
                            int(player_id),
                            {
                                "player_id": int(player_id),
                                "name": player.get("name") or player.get("fullName"),
                                "team": player.get("teamAbbrev"),
                                "games": set(),
                                "stat_totals": {stat: 0.0 for stat in scoring_rules},
                            },
                        )
                        entry["games"].add(game_id)
                        stat_line = player.get("stat", player)
                        for stat in scoring_rules:
                            value = _coerce_float(stat_line.get(stat))
                            if value is None:
                                value = 0.0
                            entry["stat_totals"][stat] += value

        results = []
        for entry in player_totals.values():
            totals = entry["stat_totals"]
            fantasy_points = sum(totals[stat] * weight for stat, weight in scoring_rules.items())
            results.append(
                {
                    "player_id": entry["player_id"],
                    "name": entry.get("name"),
                    "team": entry.get("team"),
                    "games_played": len(entry["games"]),
                    "fantasy_points": round(fantasy_points, 3),
                    "stat_totals": {k: round(v, 3) for k, v in totals.items()},
                    "scoring": scoring_rules,
                }
            )

        results.sort(key=lambda r: r.get("fantasy_points", 0), reverse=True)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "scoring": scoring_rules,
            "top_n": top_n,
            "results": results,
            "top_results": results[: max(1, top_n)],
            "game_count": len(game_ids),
        }

    def search_player(self, name: str) -> Dict[str, Any]:
        payload = self.client.get_json(f"player-search/{name}")
        candidates = []
        for person in payload.get("players", payload.get("data", [])):
            team_abbrev = person.get("teamAbbrev") or person.get("teamAbbrev", {}).get("default")
            candidates.append(
                {
                    "player_id": person.get("playerId") or person.get("id"),
                    "full_name": person.get("name") or person.get("fullName"),
                    "team_abbrev": team_abbrev,
                }
            )
        return {"candidates": candidates}

    def get_player_info(self, player_id: int) -> Dict[str, Any]:
        payload = self.client.get_json(f"player/{player_id}/landing")
        return payload

    def get_team_schedule(self, team_abbrev: str, start_date: str, end_date: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        season_id = season_id_from_date(start_date)
        payload = self.client.get_json(
            f"club-schedule-season/{team_abbrev}/{season_id}",
        )

        games: List[Dict[str, Any]] = []
        for game in payload.get("games", []):
            game_date = (game.get("gameDate") or game.get("date") or "")[:10]
            if not (start_date <= game_date <= end_date):
                continue
            home_team = (game.get("homeTeam") or {}).get("abbrev") or (game.get("homeTeam") or {}).get("abbreviation")
            away_team = (game.get("awayTeam") or {}).get("abbrev") or (game.get("awayTeam") or {}).get("abbreviation")
            is_home = normalize_team_abbrev(home_team or "") == team_abbrev
            opponent = away_team if is_home else home_team
            games.append(
                {
                    "date": game_date,
                    "opponent": normalize_team_abbrev(opponent) if opponent else None,
                    "home_away": "home" if is_home else "away",
                    "game_id": game.get("gameId") or game.get("id") or game.get("gamePk"),
                }
            )

        return {"team": team_abbrev, "games": games}

    def get_team_schedule_now(self, team_abbrev: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-schedule-season/{team_abbrev}/now")

    def get_team_schedule_week(self, team_abbrev: str, date: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-schedule-week/{team_abbrev}/{date}")

    def get_team_schedule_week_now(self, team_abbrev: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-schedule-week/{team_abbrev}/now")

    def get_player_game_logs(self, player_id: int, start_date: str, end_date: str) -> Dict[str, Any]:
        season_id = season_id_from_date(end_date)
        payload = self.client.get_json(
            f"player/{player_id}/game-log/{season_id}/2",
        )

        splits = payload.get("gameLog", payload.get("games", []))
        logs = []
        for split in splits:
            game_date = (split.get("gameDate") or split.get("date") or "")[:10]
            if not (start_date <= game_date <= end_date):
                continue
            stat = split.get("stat", split)
            logs.append(
                {
                    "date": game_date,
                    "game_id": split.get("gameId") or split.get("gamePk"),
                    "goals": stat.get("goals"),
                    "assists": stat.get("assists"),
                    "shots": stat.get("shots"),
                    "points": stat.get("points"),
                    "toi": stat.get("toi") or stat.get("timeOnIce"),
                }
            )

        return {"player_id": player_id, "games": logs}

    def get_player_game_logs_now(self, player_id: int) -> Dict[str, Any]:
        payload = self.client.get_json(f"player/{player_id}/game-log/now")
        return {"player_id": player_id, "games": payload.get("gameLog", payload.get("games", []))}

    def get_skater_stats_leaders_current(self, categories: Sequence[str], limit: int = 10) -> Dict[str, Any]:
        params = {"categories": ",".join(categories), "limit": limit}
        payload = self.client.get_json("skater-stats-leaders/current", params=params)
        return payload

    def get_skater_stats_leaders_season(
        self, season_id: str, game_type: int, categories: Sequence[str], limit: int = 10
    ) -> Dict[str, Any]:
        params = {"categories": ",".join(categories), "limit": limit}
        payload = self.client.get_json(
            f"skater-stats-leaders/{season_id}/{game_type}",
            params=params,
        )
        return payload

    def get_goalie_stats_leaders_current(self, categories: Sequence[str], limit: int = 10) -> Dict[str, Any]:
        params = {"categories": ",".join(categories), "limit": limit}
        payload = self.client.get_json("goalie-stats-leaders/current", params=params)
        return payload

    def get_goalie_stats_leaders_season(
        self, season_id: str, game_type: int, categories: Sequence[str], limit: int = 10
    ) -> Dict[str, Any]:
        params = {"categories": ",".join(categories), "limit": limit}
        payload = self.client.get_json(
            f"goalie-stats-leaders/{season_id}/{game_type}",
            params=params,
        )
        return payload

    def get_standings_now(self) -> Dict[str, Any]:
        return self.client.get_json("standings/now")

    def get_standings_by_date(self, date: str) -> Dict[str, Any]:
        return self.client.get_json(f"standings/{date}")

    def get_club_stats_now(self, team_abbrev: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-stats/{team_abbrev}/now")

    def get_club_stats_season(self, team_abbrev: str, season_id: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-stats/{team_abbrev}/{season_id}")

    def get_club_stats_season_game_type(
        self, team_abbrev: str, season_id: str, game_type: int
    ) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"club-stats/{team_abbrev}/{season_id}/{game_type}")

    def get_team_roster_now(self, team_abbrev: str) -> Dict[str, Any]:
        team_abbrev = normalize_team_abbrev(team_abbrev)
        return self.client.get_json(f"roster/{team_abbrev}/now")

    def get_schedule_now(self) -> Dict[str, Any]:
        return self.client.get_json("schedule/now")

    def get_schedule_by_date(self, date: str) -> Dict[str, Any]:
        return self.client.get_json(f"schedule/{date}")

    def get_daily_scores_now(self) -> Dict[str, Any]:
        return self.client.get_json("scoreboard/now")

    def get_daily_scores_by_date(self, date: str) -> Dict[str, Any]:
        return self.client.get_json(f"scoreboard/{date}")

    def get_scoreboard(self, date: str | None = None) -> Dict[str, Any]:
        if date:
            return self.client.get_json(f"scoreboard/{date}")
        return self.client.get_json("scoreboard/now")

    def get_game_boxscore(self, game_id: int) -> Dict[str, Any]:
        return self.client.get_json(f"gamecenter/{game_id}/boxscore")

    def get_seasons(self) -> Dict[str, Any]:
        return self.client.get_json("season")

    def get_stats_team_info(self) -> Dict[str, Any]:
        return self.stats_client.get_json("en/team")

    def get_stats_team_by_id(self, team_id: int) -> Dict[str, Any]:
        return self.stats_client.get_json(f"en/team/id/{team_id}")

    def get_stats_team_stats(
        self,
        report: str,
        cayenne_exp: str,
        sort: str | None = None,
        limit: int | None = None,
        start: int | None = None,
        direction: str | None = None,
        is_aggregate: bool | None = None,
        is_game: bool | None = None,
        fact_cayenne_exp: str | None = None,
        include: str | None = None,
        exclude: str | None = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"cayenneExp": cayenne_exp}
        if sort:
            params["sort"] = sort
        if limit is not None:
            params["limit"] = limit
        if start is not None:
            params["start"] = start
        if direction:
            params["dir"] = direction
        if is_aggregate is not None:
            params["isAggregate"] = is_aggregate
        if is_game is not None:
            params["isGame"] = is_game
        if fact_cayenne_exp:
            params["factCayenneExp"] = fact_cayenne_exp
        if include:
            params["include"] = include
        if exclude:
            params["exclude"] = exclude
        return self.stats_client.get_json(f"en/team/{report}", params=params)

    def get_stats_seasons(self) -> Dict[str, Any]:
        return self.stats_client.get_json("en/season")


def build_tool_specs(tools: NHLTools, include_eval_tools: bool = True) -> List[ToolSpec]:
    specs = [
        ToolSpec(
            name="nhl_api_list_endpoints",
            description="List the allowed NHL API endpoint catalog the agent can use. Optionally filter by category.",
            parameters={
                "type": "object",
                "properties": {"category": {"type": "string"}},
            },
            handler=tools.nhl_api_list_endpoints,
        ),
        ToolSpec(
            name="nhl_api_call",
            description=(
                "Call an NHL API endpoint from the allowed catalog. "
                "Provide base ('web' or 'stats'), a path_template that exactly matches a catalog entry, "
                "optional path_params to fill {tokens}, and optional query_params."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "base": {"type": "string", "enum": ["web", "stats"]},
                    "path_template": {"type": "string"},
                    "path_params": {"type": "object"},
                    "query_params": {"type": "object"},
                },
                "required": ["base", "path_template"],
            },
            handler=tools.nhl_api_call,
        ),
    ]
    if include_eval_tools:
        specs.extend(
            [
                ToolSpec(
                    name="fantasy_score_player_week",
                    description=(
                        "Score a single player's fantasy points between start_date and end_date using optional scoring weights."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "player_id": {"type": "integer"},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "scoring": {"type": "object"},
                        },
                        "required": ["player_id", "start_date", "end_date"],
                    },
                    handler=tools.fantasy_score_player_week,
                ),
                ToolSpec(
                    name="fantasy_best_players_week",
                    description=(
                        "Rank a list of players by fantasy points between start_date and end_date using optional scoring weights."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "player_ids": {"type": "array", "items": {"type": "integer"}},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "scoring": {"type": "object"},
                            "top_n": {"type": "integer"},
                        },
                        "required": ["player_ids", "start_date", "end_date"],
                    },
                    handler=tools.fantasy_best_players_week,
                ),
                ToolSpec(
                    name="fantasy_best_players_week_from_games",
                    description=(
                        "Rank all players by fantasy points for a week by aggregating game boxscores."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "scoring": {"type": "object"},
                            "top_n": {"type": "integer"},
                            "min_toi_seconds": {"type": "integer"},
                        },
                        "required": ["start_date", "end_date"],
                    },
                    handler=tools.fantasy_best_players_week_from_games,
                ),
            ]
        )
    return specs

