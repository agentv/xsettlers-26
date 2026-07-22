import glob
import os
from db.connection import get_connection
from config.loader import load_starting_configuration
from db.bootstrap import bootstrap_game
from xsettlers_mcp.auth import authenticate

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCENARIO_GLOB = os.path.join(_REPO_ROOT, "config", "game*.yaml")

def list_scenarios(player_token: str = None) -> list:
    """
    Enumerate available game scenarios by scanning config/game*.yaml
    (excluding game_config.yaml itself, which is the shared game settings +
    player roster file, not a scenario). Each scenario file is a starting
    configuration and must declare its own name/description.

    player_token is accepted but unused -- kept for call-signature
    consistency with every other tool function, since the MCP dispatch
    layer calls every tool with the same arguments dict.
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
        })
    return scenarios

def get_active_game() -> dict:
    """The currently bootstrapped scenario, or None if none has been selected yet."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""SELECT scenario_name,scenario_file,selected_by,bootstrapped_at
        FROM games WHERE id=1""")
    row = cur.fetchone(); conn.close()
    return dict(row) if row else None

def select_scenario(player_token: str, scenario_name: str) -> dict:
    """
    The one real gate a player must pass before anything else works: must be
    on the roster (authenticate) and must name a real scenario. Bootstraps
    the chosen scenario if no game is active yet.

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
