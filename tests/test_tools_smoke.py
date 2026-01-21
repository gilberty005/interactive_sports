import pytest

from src.tools.tools import NHLTools


@pytest.mark.network
def test_search_player_smoke():
    tools = NHLTools()
    result = tools.search_player("Auston Matthews")
    assert "candidates" in result
    assert any(candidate.get("player_id") for candidate in result["candidates"])


@pytest.mark.network
def test_team_schedule_smoke():
    tools = NHLTools()
    result = tools.get_team_schedule("TOR", "2024-01-22", "2024-01-28")
    assert result["team"] == "TOR"
    assert isinstance(result["games"], list)


@pytest.mark.network
def test_player_game_logs_smoke():
    tools = NHLTools()
    search = tools.search_player("Auston Matthews")
    player_id = search["candidates"][0]["player_id"]
    result = tools.get_player_game_logs(player_id, "2024-01-01", "2024-01-15")
    assert result["player_id"] == player_id
    assert isinstance(result["games"], list)

