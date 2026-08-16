"""
GameHouse handoff surface -- xsettlers' side of the wire mechanism in
../gamehouse/docs/data_model.md. Scoped to Diaspora (config/game0.yaml) only
for v1, registered with GameHouse as a scenario-less game (empty scenarios
list) -- xsettlers has three scenarios, but the registration/lobby model
only carries one lobby shape per registered game, and multi-scenario support
on GameHouse's side is still unresolved. start_session's scenario_key is
therefore always None in practice today; accepted and ignored rather than
validated, with a note below for where real branching would go once
multi-scenario support exists on both sides.

Two directions of traffic:
  - register_with_gamehouse(): xsettlers acts as an MCP CLIENT against
    GameHouse, once at server startup, to publish its lobby shape. The
    registration model is push, not pull -- xsettlers announces itself;
    GameHouse does not interrogate it.
  - start_session(): xsettlers acts as an MCP SERVER, GameHouse calls this
    once a lobby closes.

The existing static-roster auth (xsettlers_mcp/auth.py, config/game_config.yaml's
players: directory) is untouched by this module. start_session() is an
additional bootstrap path alongside select_scenario(), not a replacement --
it still produces ordinary player_token-based players rows that every
existing gameplay tool already knows how to check.
"""
from xsettlers_mcp.tools.registry import mcp_tool
import os
import secrets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config.loader import load_starting_configuration
from db.connection import get_connection
from db.bootstrap import bootstrap_game
from npc.profiles import assign_npc_profile
from npc.strategies import strategy_names
from xsettlers_mcp.game_select import get_active_game

SCENARIO_FILE = "config/game0.yaml"
SCENARIO_NAME = "game0"
GAME_NAME = "xsettlers26"
VALID_KINDS = {"person", "npc"}

async def register_with_gamehouse() -> dict:
    """
    Publishes this deployment's lobby shape to GameHouse, once at server
    startup (see xsettlers_mcp/server.py's main()). Mirrors GameHouse's own
    gamehouse_mcp/game_client.py pattern: a genuine MCP client
    (mcp.client.streamable_http + ClientSession) against another server's
    /mcp endpoint, not a bespoke HTTP call.

    GAMEHOUSE_URL and XSETTLERS_PUBLIC_URL are both required env vars --
    GameHouse needs to know where to push start_session back to
    (XSETTLERS_PUBLIC_URL), which xsettlers can't infer about itself from
    inside the process. Failure (GameHouse not running, wrong URL, etc.) is
    caught and returned rather than raised -- registration is best-effort at
    startup, same "external boundary call, don't crash on it" posture
    GameHouse's own push_start_session takes, and a dev environment running
    xsettlers alone with no GameHouse at all is a normal, supported case.
    """
    gamehouse_url = os.getenv("GAMEHOUSE_URL")
    public_url = os.getenv("XSETTLERS_PUBLIC_URL")
    if not gamehouse_url or not public_url:
        return {"ok": False, "error": "GAMEHOUSE_URL and XSETTLERS_PUBLIC_URL must both be set"}

    sc = load_starting_configuration(SCENARIO_FILE)
    lobby = sc.lobby
    try:
        async with streamablehttp_client(gamehouse_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("register_game", arguments={
                    "game_name": GAME_NAME,
                    "endpoint_url": public_url,
                    "min_players": lobby.min_players,
                    "max_players": lobby.max_players,
                    "wait_window_seconds": lobby.wait_window_seconds,
                    "npc_profile_schema": npc_profile_schema(),
                    "scenarios": [],
                })
    except Exception as exc:
        return {"ok": False, "error": f"register_game call to {gamehouse_url} failed: {exc}"}

    text = ""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    return {"ok": True, "response": text}

def npc_profile_schema() -> dict:
    """
    The JSON schema an NPC roster entry must match, built from the strategy
    library rather than authored.

    Every scenario used to restate this enum in its own `lobby:` block, and
    each copy went stale the moment a strategy was added or renamed -- the
    same drift the loader already refuses for min_players. The library is
    service-wide, not per-scenario, so there is nothing for a scenario to say
    about it.

    `config` is intentionally unconstrained: it overlays the strategy
    document's own config block, whose keys differ per strategy, and a
    conditional schema keyed on strategy_ref would be a second copy of the
    library to keep in step.
    """
    return {
        "type": "object",
        "required": ["strategy_ref"],
        "properties": {
            "strategy_ref": {"type": "string", "enum": strategy_names()},
            "config": {"type": "object"},
        },
    }


def _validate_players(players: list, lobby) -> dict | None:
    """Returns an error dict if the players list is malformed, else None."""
    if not isinstance(players, list) or not players:
        return {"error": "players must be a non-empty list"}
    if not (lobby.min_players <= len(players) <= lobby.max_players):
        return {"error": f"{SCENARIO_NAME} requires between {lobby.min_players} and "
                         f"{lobby.max_players} players, got {len(players)}"}
    for p in players:
        if "player_id" not in p or "kind" not in p:
            return {"error": "each player entry requires player_id and kind"}
        if p["kind"] not in VALID_KINDS:
            return {"error": f"Invalid kind '{p['kind']}'. Valid: {sorted(VALID_KINDS)}"}
        if p["kind"] == "npc":
            profile = p.get("profile") or {}
            strategy_ref = profile.get("strategy_ref")
            # strategy_ref is opaque to GameHouse -- it carries the value and
            # never interprets it, the same way it treats a score object.
            # xsettlers owns the catalogue and is the only side that can say
            # whether a reference resolves, so adding a strategy never
            # requires telling GameHouse anything.
            valid = strategy_names()
            if strategy_ref not in valid:
                return {"error": f"Unknown npc strategy_ref '{strategy_ref}'. "
                                 f"Valid: {valid}"}
    return None

@mcp_tool(
    "GameHouse handoff: called once GameHouse closes a lobby, to actually "
    "hand the game off. players is a list of {player_id, kind: "
    "'person'|'npc', profile?} entries -- person player_ids are GameHouse's "
    "real person.id, npc player_ids are GameHouse-minted ephemeral labels "
    "with a profile.strategy_ref matching the npc_profile_schema this game "
    "registered via register_game. Bootstraps the game and returns each "
    "entry's xsettlers_player_id, plus a freshly generated player_token for "
    "person-kind entries -- GameHouse is responsible for relaying that "
    "token back to the actual human player. Not something a player calls "
    "directly.",
    players={"type": "array", "items": {"type": "object"}})
def start_session(session_token: str, players: list, scenario_key: str = None) -> dict:
    """
    The actual handoff: GameHouse closes a lobby and calls this once,
    minting session_token and describing every participant (see
    ../gamehouse/docs/data_model.md's start_session wire shape). Builds a
    roster_override -- the shape db.bootstrap.bootstrap_game() already
    accepts -- rather than modifying bootstrap_game() itself.

    scenario_key is accepted but not yet acted on: xsettlers registered with
    an empty scenarios list (see register_with_gamehouse), so GameHouse will
    only ever send None here today. Once xsettlers registers more than one
    scenario, this is where scenario_key would pick which of
    config/game*.yaml to bootstrap instead of the hardcoded SCENARIO_FILE.

    person-kind player_ids are GameHouse's real person.id, trusted as-is
    (they arrived over the session-token-authenticated channel). npc-kind
    player_ids are GameHouse-minted ephemeral labels. Neither carries an
    email or display_name, so both are synthesized here -- xsettlers'
    players.email/display_name columns exist for the existing static-roster
    auth path, which a GameHouse-driven session has no use for beyond
    satisfying the schema.

    Response shape (player_id/xsettlers_player_id/player_token per entry) is
    xsettlers' own invention -- GameHouse's doc defines what it sends here,
    not what a game returns, so this is the piece GameHouse would need to
    relay each generated player_token back to its actual human player.
    """
    sc = load_starting_configuration(SCENARIO_FILE)
    err = _validate_players(players, sc.lobby)
    if err:
        return err

    active = get_active_game()
    conn = get_connection(); cur = conn.cursor()
    existing_session = cur.execute("SELECT session_token FROM game_session WHERE id=1").fetchone()
    conn.close()
    if active:
        if existing_session and existing_session["session_token"] == session_token:
            return {"ok": True, "already_active": True, "scenario_name": active["scenario_name"]}
        return {"error": f"A game is already in progress (scenario '{active['scenario_name']}') "
                         f"under a different session -- cannot start a new one"}

    roster_override = []
    generated_tokens = {}  # gamehouse player_id -> xsettlers player_token, person-kind only
    for i, p in enumerate(players):
        gh_id = p["player_id"]
        kind = p["kind"]
        token = secrets.token_hex(16)
        email = f"gamehouse-{gh_id}@handoff"
        display_name = f"Player {gh_id}" if kind == "person" else str(gh_id)
        # Home sectors taken positionally from game0.yaml's own authored
        # participants -- reuses the scenario's spatial design (opposite
        # corners) without a real email to match against. v1 simplification:
        # doesn't generalize past however many participants game0.yaml
        # itself declares (2 today, matching lobby.max_players).
        home_sector = sc.participants[i].home_sector
        roster_override.append({
            "email": email, "display_name": display_name, "player_token": token,
            "home_sector": home_sector, "is_npc": (kind == "npc"),
        })
        if kind == "person":
            generated_tokens[gh_id] = token

    bootstrap_game(scenario_file=SCENARIO_FILE, scenario_name=SCENARIO_NAME,
                   selected_by=session_token, roster_override=roster_override)

    conn = get_connection(); cur = conn.cursor()
    by_email = {r["email"]: r["id"] for r in cur.execute("SELECT id, email FROM players").fetchall()}
    conn.close()

    result_players = []
    for i, p in enumerate(players):
        seat = roster_override[i]
        xsettlers_id = by_email[seat["email"]]
        entry = {"player_id": p["player_id"], "kind": p["kind"], "xsettlers_player_id": xsettlers_id}
        if p["kind"] == "person":
            entry["player_token"] = generated_tokens[p["player_id"]]
        else:
            profile = p.get("profile") or {}
            assign_npc_profile(xsettlers_id, profile["strategy_ref"], config=profile.get("config"))
        result_players.append(entry)

    conn = get_connection(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO game_session (id, session_token) VALUES (1, ?)",
                (session_token,))
    conn.commit(); conn.close()

    return {"ok": True, "already_active": False, "scenario_name": SCENARIO_NAME,
            "players": result_players}
