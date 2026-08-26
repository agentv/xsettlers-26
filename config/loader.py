import os
import yaml
from dataclasses import dataclass, field
from typing import List

CONFIG_PATH = os.getenv("GAME_CONFIG_PATH", "config/game_config.yaml")

# How full every pod starts when a scenario doesn't say otherwise, as a
# fraction of storage_capacity. Kept low: a fleet starting at capacity cannot
# accumulate anything, so its production is pure waste and only spending moves
# the score -- in play-testing a 100%-full start held total holdings pinned
# for the whole game while a lean start grew steadily. Starting lean makes
# production the game rather than a formality. Scenarios override it freely;
# this is only the fallback.
DEFAULT_STARTING_FILL = 0.3

# Energy seeded into a player's home sector: rich, and finite. A starting
# position is a promise about a specific number rather than a bet on a range,
# so this is written straight over whatever home rolled (db/bootstrap.py) --
# home is not expressed as a hotspot even though the map layer could carry it.
# Per-scenario by design. Pod throughput, not the sector, is what actually
# limits a player; see ../xsettlers-designer/docs/design_direction.md.
#
# NOT the "sentinel sector" (id = -1), the parking slot for ships in transit,
# which must stay at 0 energy -- that zero is the entire mechanism
# suppressing energy harvesting mid-flight.
HOME_SECTOR_ENERGY = 2_200.0

@dataclass
class GameSettings:
    """
    Engine-wide settings from config/game_config.yaml's `game:` block.

    Only `max_players` and `score_weights` are actually consumed today.
    `tick_seconds`/`turn_limit`/`confidence_decay_per_turn` are parsed but
    then shadowed by GAME_TICK_SECONDS/TURN_LIMIT/CONFIDENCE_DECAY_PER_TURN
    -- kept here deliberately, pending the precedence rule (YAML supplies the
    default, env overrides it) that docs/TODO.md tracks applying to all three
    at once. A YAML key with no env counterpart and no consumer has nothing to
    reconcile against and does not belong here.
    """
    name: str
    tick_seconds: int
    confidence_decay_per_turn: int
    max_players: int
    turn_limit: int
    score_weights: dict

@dataclass
class HotspotDef:
    """
    A region of the map that rolls richer (or poorer) than open space.

    Nothing constrains the multiplier to be above 1. A value below it marks a
    lean region, which is the same mechanism read the other way, and is how a
    scenario draws a desert without a second vocabulary for it.

    `radius` is Euclidean and inclusive, measured in sectors from `center`,
    so 0 covers exactly the centre sector.
    """
    center: List[int]
    radius: float = 0.0
    multiplier: float = 1.0


@dataclass
class ScatterDef:
    """
    A rule for placing hotspots the author does not want to name individually
    -- count, the box they fall in, and the ranges their radius and multiplier
    are drawn from.

    Expanded into concrete HotspotDefs once, at bootstrap, from the map seed
    (db/bootstrap.py). So this is sugar over `hotspots:` rather than a second
    mechanism: by the time anything reads the map there is only one form, and
    a later distance-to-source gradient becomes a third way of producing
    entries rather than a third thing reveal_sector has to know about.
    """
    count: int
    within_min: List[int]
    within_max: List[int]
    radius: List[float]
    multiplier: List[float]


@dataclass
class MapDef:
    """A scenario's map layout: hotspots it places by hand, plus a rule for
    scattering the rest. Both are optional and they compose -- placed entries
    are exactly the ones an authored scenario wants guaranteed."""
    hotspots: List["HotspotDef"] = field(default_factory=list)
    scatter: "ScatterDef | None" = None


@dataclass
class PodTemplateDef:
    task: str
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
class LobbyDef:
    """
    The shape a scenario reports to GameHouse (see xsettlers_mcp/gamehouse.py)
    -- min/max player counts and how long GameHouse should wait for a second
    human before backfilling with an NPC.

    min_players/max_players are DERIVED from len(participants), never read
    from YAML -- resolve_seats() already requires an exact roster match, not
    a range, so a separately-authored number here could only ever drift out
    of sync with the participants list it's supposed to describe. Restating
    it in the file would be exactly the "changing a value in the YAML and
    expecting it to take effect" trap this codebase already warns against
    elsewhere (see CLAUDE.md on game_config.yaml's dead `game:` fields).

    wait_window_seconds IS scenario-authored (nothing to derive it from) but
    optional -- a scenario silent on `lobby:` entirely gets a sensible default
    rather than being unable to load at all, so ad hoc/minimal scenarios (test
    fixtures included) don't need to declare a GameHouse-specific block just
    to parse.
    """
    min_players: int
    max_players: int
    wait_window_seconds: int

DEFAULT_LOBBY_WAIT_WINDOW_SECONDS = 120

@dataclass
class StartingConfiguration:
    name: str
    description: str
    participants: List[ParticipantDef]
    ships_per_player: int
    pods_per_ship: List[PodTemplateDef]
    lobby: LobbyDef
    home_colony: bool = False
    # Scenario-wide default for how full every pod starts, 0.0-1.0. Individual
    # pod templates may override it. Falls back to DEFAULT_STARTING_FILL when
    # the scenario is silent.
    starting_fill: float = DEFAULT_STARTING_FILL
    # Energy seeded into each player's home sector, overriding the ordinary
    # discovery roll. See HOME_SECTOR_ENERGY.
    home_sector_energy: float = HOME_SECTOR_ENERGY
    # Where the map is richer than open space. Home is deliberately NOT
    # expressed here: home_sector_energy above is an absolute figure written
    # over whatever home rolled, because a starting position is a promise
    # about a specific number rather than a bet on a range, and the home
    # coordinates live in participants[] where a duplicate copy here could
    # only drift out of sync.
    map: MapDef = field(default_factory=MapDef)

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
        task=_require(p, "task", "pods_per_ship[].task"),
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
    lobby_raw = sc_raw.get("lobby", {})
    lobby = LobbyDef(
        min_players=len(participants),
        max_players=len(participants),
        wait_window_seconds=int(lobby_raw.get("wait_window_seconds",
                                              DEFAULT_LOBBY_WAIT_WINDOW_SECONDS)),
    )
    return StartingConfiguration(
        name=_require(sc_raw, "name", "name"),
        description=_require(sc_raw, "description", "description"),
        participants=participants,
        ships_per_player=int(_require(sc_raw, "ships_per_player",
                                      "ships_per_player")),
        pods_per_ship=pods_per_ship,
        lobby=lobby,
        home_colony=bool(sc_raw.get("home_colony", False)),
        starting_fill=scenario_fill,
        home_sector_energy=float(sc_raw.get("home_sector_energy",
                                            HOME_SECTOR_ENERGY)),
        map=_load_map(sc_raw.get("map") or {}),
    )


def _load_map(map_raw: dict) -> MapDef:
    """
    Parse a scenario's `map:` block. Absent or empty means open space
    everywhere -- every sector takes the ordinary discovery roll."""
    if not isinstance(map_raw, dict):
        raise ValueError("map must be a mapping with 'hotspots' and/or 'scatter'")
    unknown = set(map_raw) - {"hotspots", "scatter"}
    if unknown:
        raise ValueError(f"unknown map keys {sorted(unknown)}. "
                         f"Valid: ['hotspots', 'scatter']")
    hotspots = [_load_hotspot(h, i) for i, h in enumerate(map_raw.get("hotspots") or [])]
    scatter_raw = map_raw.get("scatter")
    return MapDef(hotspots=hotspots,
                  scatter=_load_scatter(scatter_raw) if scatter_raw else None)


def _load_hotspot(raw: dict, index: int) -> HotspotDef:
    where = f"map.hotspots[{index}]"
    center = list(_require(raw, "center", f"{where}.center"))
    if len(center) != 3:
        raise ValueError(f"{where}.center must be [x, y, z], got {center}")
    return HotspotDef(center=[int(c) for c in center],
                      radius=_non_negative(raw.get("radius", 0), f"{where}.radius"),
                      multiplier=_positive(raw.get("multiplier", 1.0),
                                           f"{where}.multiplier"))


def _load_scatter(raw: dict) -> ScatterDef:
    where = "map.scatter"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    within = _require(raw, "within", f"{where}.within")
    lo = list(_require(within, "min", f"{where}.within.min"))
    hi = list(_require(within, "max", f"{where}.within.max"))
    if len(lo) != 3 or len(hi) != 3:
        raise ValueError(f"{where}.within min/max must each be [x, y, z]")
    if any(b < a for a, b in zip(lo, hi)):
        raise ValueError(f"{where}.within max {hi} is below min {lo} on some axis")
    count = int(_require(raw, "count", f"{where}.count"))
    if count < 0:
        raise ValueError(f"{where}.count must not be negative, got {count}")
    return ScatterDef(
        count=count,
        within_min=[int(v) for v in lo],
        within_max=[int(v) for v in hi],
        radius=_range(raw.get("radius", [0, 0]), f"{where}.radius", _non_negative),
        multiplier=_range(raw.get("multiplier", [1.0, 1.0]),
                          f"{where}.multiplier", _positive),
    )


def _range(value, path: str, check) -> list:
    """A [low, high] pair. A bare scalar is accepted as a degenerate range so
    a scenario wanting every scattered hotspot identical does not have to
    write the number twice."""
    pair = value if isinstance(value, list) else [value, value]
    if len(pair) != 2:
        raise ValueError(f"{path} must be [low, high] or a single value, got {value}")
    low, high = (check(v, path) for v in pair)
    if high < low:
        raise ValueError(f"{path} high {high} is below low {low}")
    return [low, high]


def _non_negative(value, path: str) -> float:
    f = float(value)
    if f < 0:
        raise ValueError(f"{path} must not be negative, got {f}")
    return f


def _positive(value, path: str) -> float:
    f = float(value)
    if f <= 0:
        raise ValueError(f"{path} must be greater than 0, got {f}")
    return f


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
    `starting_configuration` is None."""
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
