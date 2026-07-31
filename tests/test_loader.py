import pytest
from config.loader import (
    load_config, load_starting_configuration, resolve_seats,
    PlayerDef, ParticipantDef, StartingConfiguration,
)

DIRECTORY = [
    PlayerDef(email="a@test.com", display_name="Ada", player_token="TOK_A"),
    PlayerDef(email="b@test.com", display_name="Bo", player_token="TOK_B"),
]

def _scenario(participants, name="Test Scenario"):
    return StartingConfiguration(
        name=name, description="d", participants=participants,
        ships_per_player=1, pods_per_ship=[], home_colony=False)

# --- resolve_seats: pairing participants with directory identities ---

def test_resolve_seats_pairs_identity_with_starting_position():
    """Identity and home sector travel together on one Seat, so nothing
    downstream has to pair two lists by index -- the defect class that made
    roster size and scenario size have to match by luck."""
    sc = _scenario([ParticipantDef(player="b@test.com", home_sector=[4, 4, 0]),
                    ParticipantDef(player="a@test.com", home_sector=[9, 9, 0])])
    seats = resolve_seats(sc, DIRECTORY, max_players=8)
    assert [(s.display_name, s.home_sector) for s in seats] == \
           [("Bo", [4, 4, 0]), ("Ada", [9, 9, 0])]
    assert [s.player_token for s in seats] == ["TOK_B", "TOK_A"]

def test_resolve_seats_supports_a_solo_scenario():
    """max_players is a ceiling, not a floor -- one participant is valid."""
    sc = _scenario([ParticipantDef(player="a@test.com", home_sector=[1, 1, 0])])
    assert len(resolve_seats(sc, DIRECTORY, max_players=8)) == 1

def test_resolve_seats_rejects_a_participant_missing_from_the_directory():
    """Failing loudly beats quietly seating fewer players than the scenario
    declares, which would only surface much later as a game with the wrong
    number of players in it."""
    sc = _scenario([ParticipantDef(player="ghost@test.com", home_sector=[1, 1, 0])])
    with pytest.raises(ValueError, match="not in the player directory"):
        resolve_seats(sc, DIRECTORY, max_players=8)

def test_resolve_seats_enforces_max_players_as_a_ceiling():
    sc = _scenario([ParticipantDef(player="a@test.com", home_sector=[1, 1, 0]),
                    ParticipantDef(player="b@test.com", home_sector=[2, 2, 0])])
    with pytest.raises(ValueError, match="max_players=1"):
        resolve_seats(sc, DIRECTORY, max_players=1)

def test_resolve_seats_carries_npc_flag():
    sc = _scenario([ParticipantDef(player="a@test.com", home_sector=[1, 1, 0], is_npc=True)])
    assert resolve_seats(sc, DIRECTORY, max_players=8)[0].is_npc is True

# --- load_config: directory always, scenario only on request ---

def test_load_config_without_a_scenario_has_directory_but_no_seats():
    """Callers that only want score_weights or need to resolve a token to an
    identity have no business asserting which game is being played."""
    cfg = load_config()
    assert cfg.starting_configuration is None
    assert cfg.seats == []
    assert {p.email for p in cfg.players} == {"vincent@example.com", "player2@example.com"}

def test_load_config_with_a_scenario_resolves_its_seats():
    cfg = load_config(scenario_override="config/game0.yaml")
    assert cfg.starting_configuration.name == "Diaspora"
    assert [s.home_sector for s in cfg.seats] == [[25, 25, 0], [25, 50, 0]]
    assert [s.display_name for s in cfg.seats] == ["Vincent", "Player Two"]

def test_load_config_rejects_a_scenario_naming_an_unknown_participant(tmp_path):
    bad = tmp_path / "game_bad.yaml"
    bad.write_text(
        'name: "Bad"\ndescription: "d"\n'
        'participants:\n  - {player: "nobody@example.com", home_sector: [1, 1, 0]}\n'
        'ships_per_player: 1\npods_per_ship: []\n')
    with pytest.raises(ValueError, match="not in the player directory"):
        load_config(scenario_override=str(bad))

# --- scenario files declare their own player count ---

def test_shipped_scenarios_declare_their_own_player_counts():
    """The whole point of the participants model: player count is a property
    of the scenario, not of the service."""
    counts = {name: len(load_starting_configuration(f"config/{name}.yaml").participants)
              for name in ("game0", "game1", "game_solo")}
    assert counts == {"game0": 2, "game1": 2, "game_solo": 1}

def test_scenario_without_participants_is_rejected():
    with pytest.raises(ValueError, match="Missing required config field: participants"):
        load_starting_configuration("config/game_config.yaml")
