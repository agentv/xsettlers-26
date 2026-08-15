from xsettlers_mcp.tools.registry import mcp_tool
import glob
import os
from db.connection import read_one
from config.loader import load_starting_configuration
from db.bootstrap import bootstrap_game
from xsettlers_mcp.auth import authenticate

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCENARIO_GLOB = os.path.join(_REPO_ROOT, "config", "game*.yaml")

@mcp_tool(
    "List available game scenarios a player can choose to start/join. Must "
    "be called (and select_scenario used to pick one) before any other tool "
    "works -- until a scenario is selected, no game exists and every other "
    "tool will report 'Player not found'.")
def list_scenarios(player_token: str = None) -> list:
    """
    Enumerate the game library by scanning config/game*.yaml (excluding
    game_config.yaml itself, which holds engine settings + the player
    directory, not a scenario). Each scenario file declares its own
    name/description and its own participants.

    Given a player_token, this returns only the scenarios that player is a
    participant in -- a token is an invitation to specific games, not to
    every game on the service. Called with no token (as select_scenario does
    internally) it returns the whole library.

    An unrecognized token gets an empty list rather than the full library:
    someone who isn't in the directory has no games, and enumerating what
    exists isn't something to hand out for free.
    """
    scenarios = []
    for path in sorted(glob.glob(_SCENARIO_GLOB)):
        if os.path.basename(path) == "game_config.yaml":
            continue
        scenario_name = os.path.splitext(os.path.basename(path))[0]
        rel_path = os.path.relpath(path, _REPO_ROOT)
        sc = load_starting_configuration(path)
        scenarios.append({
            "scenario_name": scenario_name,
            "file": rel_path,
            "name": sc.name,
            "description": sc.description,
            "player_count": len(sc.participants),
            "participants": [p.player for p in sc.participants],
        })
    if player_token is None:
        return scenarios
    identity = authenticate(player_token)
    if not identity["ok"]:
        return []
    return [s for s in scenarios if identity["email"] in s["participants"]]

def get_active_game() -> dict:
    """The currently bootstrapped scenario, or None if none has been selected yet."""
    row = read_one("""SELECT scenario_name,scenario_file,selected_by,bootstrapped_at
        FROM games WHERE id=1""")
    return dict(row) if row else None

@mcp_tool(
    "Choose a scenario by name (see list_scenarios) to start playing. "
    "Bootstraps the game on first selection; the MVP runs one shared game "
    "per deployed instance, so this can't be used to switch scenarios once "
    "one is already active.")
def select_scenario(player_token: str, scenario_name: str) -> dict:
    """
    The one real gate a player must pass before anything else works: must be
    in the player directory, must name a real scenario, and must be a
    participant in that scenario. Bootstraps it if no game is active yet.

    Identity is checked before the scenario is resolved, so an unrecognized
    token learns nothing about which scenarios exist.

    Once this succeeds, bootstrap_game() has populated the players table
    from the roster -- every other tool's existing internal
    "SELECT id FROM players WHERE player_token=?" check now finds a row.
    Before this succeeds, players is empty and every other tool naturally
    rejects with "Player not found", so no separate per-call gate is needed
    elsewhere.

    If a game is already active with a *different* scenario, this is
    rejected -- the MVP runs one shared game per deployed instance;
    switching scenarios mid-game isn't supported.
    """
    auth = authenticate(player_token)
    if not auth["ok"]:
        return auth
    scenarios = {s["scenario_name"]: s for s in list_scenarios()}
    if scenario_name not in scenarios:
        return {"error": f"Unknown scenario '{scenario_name}'. Available: {sorted(scenarios)}"}
    seated = authenticate(player_token, scenario_file=scenarios[scenario_name]["file"])
    if not seated["ok"]:
        return seated
    active = get_active_game()
    if active:
        if active["scenario_name"] == scenario_name:
            return {"ok": True, "already_active": True, "scenario": active}
        return {"error": f"A game is already in progress with scenario "
                          f"'{active['scenario_name']}' — cannot switch mid-game"}
    scenario = scenarios[scenario_name]
    bootstrap_game(scenario_file=scenario["file"], scenario_name=scenario_name,
                   selected_by=player_token)
    return {"ok": True, "already_active": False, "scenario": get_active_game()}
