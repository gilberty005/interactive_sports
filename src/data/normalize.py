from __future__ import annotations

from typing import Dict


ABBREV_OVERRIDES = {
    "TB": "TBL",
    "LA": "LAK",
    "NJ": "NJD",
    "SJ": "SJS",
}


def normalize_team_abbrev(abbrev: str) -> str:
    abbrev = abbrev.strip().upper()
    return ABBREV_OVERRIDES.get(abbrev, abbrev)


def build_team_id_map(teams_payload: Dict) -> Dict[str, int]:
    team_map: Dict[str, int] = {}
    for team in teams_payload.get("teams", []):
        if team.get("abbreviation") and team.get("id"):
            team_map[team["abbreviation"].upper()] = int(team["id"])
    return team_map


def season_id_from_date(date_str: str) -> str:
    year_str, month_str, _ = date_str.split("-")
    year = int(year_str)
    month = int(month_str)
    start_year = year if month >= 7 else year - 1
    return f"{start_year}{start_year + 1}"

