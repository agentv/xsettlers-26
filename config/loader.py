import os
import yaml
from dataclasses import dataclass
from typing import List

CONFIG_PATH = os.getenv("GAME_CONFIG_PATH", "config/game_config.yaml")

# How full every pod starts when a scenario doesn't say otherwise, as a
# fraction of storage_capacity. 0.3 (set 2026-07-30, replacing the original
# hardcoded 1.0): a fleet starting at capacity cannot accumulate anything, so
# its production is pure waste and only spending moves the score -- measured
# in play-testing, a 100%-full start held total holdings pinned for the whole
# game while a lean start grew steadily. Starting lean makes production the
# game rather than a formality. Scenarios override it freely; this is only
# the fallback.
DEFAULT_STARTING_FILL = 0.3

@dataclass
class GameSettings:
    name: str
    tick_seconds: int
    confidence_decay_per_turn: int
    max_players: int
    turn_limit: int
    dimensions: int
    feature_flags: dict
    score_weights: dict

@dataclass
class PodTemplateDef:
    mission: str
    count: int
    storage_capacity: float = 100.0
    # Fraction of storage_capacity this pod holds at bootstrap, 0.0-1.0.
    # How rich a game starts is a scenario characteristic, not an engine
    # constant -- see StartingConfiguration.starting_fill.
    starting_fill: float = DEFAULT_STARTING_FILL

@dataclass
class ParticipantDef:
    """
    One seat in a scenario: which directory player fills it, and where they
    start. `player` is an email referencing config/game_config.yaml's player
    directory -- credentials never appear in a scenario file.
    """
    player: str
    home_sector: List[int]
    is_npc: bool = False

@dataclass
class StartingConfiguration:
    name: str
    description: str
    participants: List[ParticipantDef]
    ships_per_player: int
    pods_per_ship: List[PodTemplateDef]
    home_colony: bool = False
    # Scenario-wide default for how full every pod starts, 0.0-1.0. Individual
    # pod templates may override it. Falls back to DEFAULT_STARTING_FILL when
    # the scenario is silent.
    starting_fill: float = DEFAULT_STARTING_FILL

@dataclass
class PlayerDef:
    """One entry in the service-wide player directory. Identity, not participation."""
    email: str
    display_name: str
    player_token: str

@dataclass
class Seat:
    """
    A participant resolved against the directory: identity and starting
    position together. This is what bootstrap actually seeds from, so nothing
    downstream has to pair two lists up by position -- the defect class that
    made roster size and scenario size have to match by luck.
    """
    email: str
    display_name: str
    player_token: str
    home_sector: List[int]
    is_npc: bool = False

@dataclass
class GameConfig:
    game: GameSettings
    starting_configuration: StartingConfiguration
    players: List[PlayerDef]     # the service-wide directory
    seats: List[Seat]            # this scenario's resolved participants; [] if no scenario

def load_starting_configuration(path: str) -> StartingConfiguration:
    """
    Load a scenario from its own YAML file (e.g. config/game0.yaml). Adding a
    playable scenario is just adding a config/game<N>.yaml -- no code change,
    and nothing to register: list_scenarios() discovers it by glob.

    A scenario declares its own participants, so its player count is whatever
    that list's length is. One entry is a solo game, five a five-player game.
    """
    with open(path, "r") as f:
        sc_raw = yaml.safe_load(f)
    scenario_fill = _fraction(sc_raw.get("starting_fill", DEFAULT_STARTING_FILL),
                              "starting_fill")
    pods_per_ship = [PodTemplateDef(
        mission=_require(p, "mission", "pods_per_ship[].mission"),
        count=int(_require(p, "count", "pods_per_ship[].count")),
        storage_capacity=float(p.get("storage_capacity", 100.0)),
        starting_fill=_fraction(p.get("starting_fill", scenario_fill),
                                "pods_per_ship[].starting_fill"),
    ) for p in sc_raw.get("pods_per_ship", [])]
    participants = [ParticipantDef(
        player=_require(p, "player", "participants[].player"),
        home_sector=list(_require(p, "home_sector", "participants[].home_sector")),
        is_npc=bool(p.get("is_npc", False)),
    ) for p in _require(sc_raw, "participants", "participants")]
    if not participants:
        raise ValueError(f"Scenario {path} defines no participants")
    return StartingConfiguration(
        name=_require(sc_raw, "name", "name"),
        description=_require(sc_raw, "description", "description"),
        participants=participants,
        ships_per_player=int(_require(sc_raw, "ships_per_player",
                                      "ships_per_player")),
        pods_per_ship=pods_per_ship,
        home_colony=bool(sc_raw.get("home_colony", False)),
        starting_fill=scenario_fill,
    )


def _fraction(value, path: str) -> float:
    f = float(value)
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"{path} must be between 0.0 and 1.0, got {f}")
    return f


def resolve_seats(starting_configuration: StartingConfiguration,
                  players: List[PlayerDef], max_players: int) -> List[Seat]:
    """
    Pair each of a scenario's participants with their directory entry.

    Fails loudly on a participant who isn't in the directory: that is a
    misconfigured scenario, and the alternative -- quietly seating fewer
    players than the scenario declares -- would show up much later as a
    game that silently has the wrong number of players in it.
    """
    by_email = {p.email: p for p in players}
    if len(starting_configuration.participants) > max_players:
        raise ValueError(
            f"Scenario '{starting_configuration.name}' seats "
            f"{len(starting_configuration.participants)} participants but "
            f"max_players={max_players}")
    seats = []
    for participant in starting_configuration.participants:
        entry = by_email.get(participant.player)
        if entry is None:
            raise ValueError(
                f"Scenario '{starting_configuration.name}' names participant "
                f"'{participant.player}', who is not in the player directory "
                f"(config/game_config.yaml's players list)")
        seats.append(Seat(
            email=entry.email,
            display_name=entry.display_name,
            player_token=entry.player_token,
            home_sector=participant.home_sector,
            is_npc=participant.is_npc,
        ))
    return seats

def load_config(path: str = CONFIG_PATH, scenario_override: str = None) -> GameConfig:
    """
    Load the service-wide config (engine settings + player directory), and
    optionally one scenario.

    scenario_override is a repo-root-relative path to a scenario file (e.g.
    "config/game1.yaml"). Omit it to load just the engine settings and
    directory -- callers that only want score_weights or need to resolve a
    token to an identity have no business asserting which game is being
    played. When it is given, the scenario's participants are resolved
    against the directory into `seats`; when it isn't, `seats` is empty and
    `starting_configuration` is None.

    There is no file-level default scenario: this service is a library of
    games, and which one is being bootstrapped is a runtime choice made by a
    player through select_scenario().
    """
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    g = raw.get("game", {})
    game = GameSettings(
        name=_require(g, "name", "game.name"),
        tick_seconds=int(_require(g, "tick_seconds", "game.tick_seconds")),
        confidence_decay_per_turn=int(_require(g, "confidence_decay_per_turn",
                                               "game.confidence_decay_per_turn")),
        max_players=int(_require(g, "max_players", "game.max_players")),
        turn_limit=int(_require(g, "turn_limit", "game.turn_limit")),
        dimensions=int(g.get("dimensions", 3)),
        feature_flags=g.get("feature_flags", {}),
        score_weights=g.get("score_weights", {"energy": 1, "goods": 1, "food": 1}),
    )
    players = [PlayerDef(
        email=_require(p, "email", "players[].email"),
        display_name=_require(p, "display_name", "players[].display_name"),
        player_token=_require(p, "player_token", "players[].player_token"),
    ) for p in raw.get("players", [])]
    seen = set()
    for p in players:
        if p.email in seen:
            raise ValueError(f"Duplicate email in player directory: {p.email}")
        seen.add(p.email)

    starting_configuration, seats = None, []
    if scenario_override:
        # Resolve relative to the directory of the main config file
        sc_path = os.path.join(os.path.dirname(os.path.abspath(path)), "..",
                               scenario_override)
        starting_configuration = load_starting_configuration(os.path.normpath(sc_path))
        seats = resolve_seats(starting_configuration, players, game.max_players)

    return GameConfig(game=game,
                      starting_configuration=starting_configuration,
                      players=players,
                      seats=seats)

def _require(d: dict, key: str, path: str):
    if key not in d:
        raise ValueError(f"Missing required config field: {path}")
    return d[key]
