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


def _parse_date(value: str) -> date | None:
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


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


def build_tool_specs(tools: NHLTools) -> List[ToolSpec]:
    return [
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

