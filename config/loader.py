import os
import yaml
from dataclasses import dataclass
from typing import List

CONFIG_PATH = os.getenv("GAME_CONFIG_PATH", "config/game_config.yaml")

@dataclass
class GameSettings:
    name: str
    tick_seconds: int
    confidence_decay: float
    max_players: int
    turn_limit: int
    dimensions: int
    feature_flags: dict
    score_weights: dict

@dataclass
class SectorDef:
    coords: List[int]
    energy_capacity: float
    food_capacity: float
    goods_capacity: float

@dataclass
class PodTemplateDef:
    mission: str
    count: int
    storage_capacity: float = 100.0
    energy_consumption: float = 0.0
    food_consumption: float = 0.0

@dataclass
class StartingConfiguration:
    name: str
    description: str
    home_sector_by_player: List[List[int]]
    ships_per_player: int
    pods_per_ship: List[PodTemplateDef]
    home_colony: bool = False

@dataclass
class PlayerDef:
    email: str
    display_name: str
    slack_user_id: str

@dataclass
class GameConfig:
    game: GameSettings
    sectors: List[SectorDef]
    starting_configuration: StartingConfiguration
    players: List[PlayerDef]

def load_starting_configuration(path: str) -> StartingConfiguration:
    """Load a starting configuration from its own YAML file (e.g. config/game0.yaml).
    To use a different game variant, point starting_configuration_file in game_config.yaml
    to a different file — no code change needed.
    """
    with open(path, "r") as f:
        sc_raw = yaml.safe_load(f)
    pods_per_ship = [PodTemplateDef(
        mission=_require(p, "mission", "pods_per_ship[].mission"),
        count=int(_require(p, "count", "pods_per_ship[].count")),
        storage_capacity=float(p.get("storage_capacity", 100.0)),
        energy_consumption=float(p.get("energy_consumption", 0.0)),
        food_consumption=float(p.get("food_consumption", 0.0)),
    ) for p in sc_raw.get("pods_per_ship", [])]
    return StartingConfiguration(
        name=_require(sc_raw, "name", "name"),
        description=_require(sc_raw, "description", "description"),
        home_sector_by_player=_require(sc_raw, "home_sector_by_player",
                                       "home_sector_by_player"),
        ships_per_player=int(_require(sc_raw, "ships_per_player",
                                      "ships_per_player")),
        pods_per_ship=pods_per_ship,
        home_colony=bool(sc_raw.get("home_colony", False)),
    )

def load_config(path: str = CONFIG_PATH, scenario_override: str = None) -> GameConfig:
    """
    scenario_override, if given, is a repo-root-relative path to a scenario file
    (e.g. "config/game1.yaml") used instead of the starting_configuration_file
    named inside game_config.yaml. Lets game_select.select_scenario() bootstrap
    a specific, player-chosen scenario without needing to rewrite game_config.yaml.
    """
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    g = raw.get("game", {})
    game = GameSettings(
        name=_require(g, "name", "game.name"),
        tick_seconds=int(_require(g, "tick_seconds", "game.tick_seconds")),
        confidence_decay=float(_require(g, "confidence_decay", "game.confidence_decay")),
        max_players=int(_require(g, "max_players", "game.max_players")),
        turn_limit=int(_require(g, "turn_limit", "game.turn_limit")),
        dimensions=int(g.get("dimensions", 3)),
        feature_flags=g.get("feature_flags", {}),
        score_weights=g.get("score_weights", {"energy": 1, "goods": 1, "food": 1}),
    )
    sectors = [SectorDef(
        coords=_require(s, "coords", "sectors[].coords"),
        energy_capacity=float(s.get("energy_capacity", 0.0)),
        food_capacity=float(s.get("food_capacity", 0.0)),
        goods_capacity=float(s.get("goods_capacity", 0.0)),
    ) for s in raw.get("sectors", [])]
    # Load starting configuration from its own file
    sc_file = scenario_override or _require(raw, "starting_configuration_file",
                                            "starting_configuration_file")
    # Resolve relative to the directory of the main config file
    sc_path = os.path.join(os.path.dirname(os.path.abspath(path)), "..", sc_file)
    starting_configuration = load_starting_configuration(
        os.path.normpath(sc_path))
    players = [PlayerDef(
        email=_require(p, "email", "players[].email"),
        display_name=_require(p, "display_name", "players[].display_name"),
        slack_user_id=_require(p, "slack_user_id", "players[].slack_user_id"),
    ) for p in raw.get("players", [])]
    if len(players) > game.max_players:
        raise ValueError(
            f"Config defines {len(players)} players but max_players={game.max_players}")
    return GameConfig(game=game, sectors=sectors,
                      starting_configuration=starting_configuration,
                      players=players)

def _require(d: dict, key: str, path: str):
    if key not in d:
        raise ValueError(f"Missing required config field: {path}")
    return d[key]
