from __future__ import annotations

from typing import Any, Dict, List

from src.data.normalize import normalize_team_abbrev, season_id_from_date
from src.tools.nhl_api import NHLApiClient
from src.agent.types import ToolSpec


class NHLTools:
    def __init__(self, client: NHLApiClient | None = None) -> None:
        self.client = client or NHLApiClient()

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


def build_tool_specs(tools: NHLTools) -> List[ToolSpec]:
    return [
        ToolSpec(
            name="search_player",
            description="Search NHL players by name and return candidate matches.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=tools.search_player,
        ),
        ToolSpec(
            name="get_team_schedule",
            description="Fetch a team schedule for a date range.",
            parameters={
                "type": "object",
                "properties": {
                    "team_abbrev": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                "required": ["team_abbrev", "start_date", "end_date"],
            },
            handler=tools.get_team_schedule,
        ),
        ToolSpec(
            name="get_player_game_logs",
            description="Fetch player game logs for a date range.",
            parameters={
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                "required": ["player_id", "start_date", "end_date"],
            },
            handler=tools.get_player_game_logs,
        ),
    ]

