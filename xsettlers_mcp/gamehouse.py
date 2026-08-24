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
    once a lobby closes."""
from xsettlers_mcp.tools.registry import mcp_tool
import asyncio
import os
import secrets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config.loader import load_starting_configuration
from db.connection import get_connection, read_one
from db.bootstrap import bootstrap_game
from db.events import record_event
from db.archive import archive_active_database
from engine.turn import get_final_scores
from npc.profiles import assign_npc_profile
from npc.strategies import strategy_names
from xsettlers_mcp.game_select import get_active_game

SCENARIO_FILE = "config/game0.yaml"
SCENARIO_NAME = "game0"
GAME_NAME = "xsettlers26"
VALID_KINDS = {"person", "npc"}

# The results envelope: the two fields every game handing results to GameHouse
# guarantees, and the only two it reads out of an otherwise opaque score
# object. `placement` is GameHouse's own reserved key (its
# gamehouse_mcp/scoreboard.py PLACEMENT_FIELD, 1 = won); `score` is ours,
# additive, and needs no change on that side because report_results stores the
# object whole.
PLACEMENT_FIELD = "placement"
SCORE_FIELD = "score"

# Written once when the hand-back succeeds, so a second poll doesn't push
# again. report_results only matches journal rows still 'in_progress', so a
# repeat call isn't destructive -- it just returns an error per entry, which
# is noise standing in for a fact we can record properly.
RESULTS_REPORTED_EVENT = "game.results_reported"

# How often the reporter wakes to check whether the game has finished. Slow on
# purpose: this is a once-per-game event with no deadline, and the check is
# three cheap indexed reads.
REPORT_POLL_SECONDS = 10

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
                    "scoreboard_schema": scoreboard_schema(),
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

def scoreboard_schema() -> dict:
    """
    The shape of the score object this game hands back, declared at
    registration. GameHouse stores it and deliberately does not consult it
    (../gamehouse/gamehouse_mcp/registry.py) -- it is documentation for
    whoever reads the catalogue, not a contract GameHouse enforces.

    It describes the ENVELOPE only. `placement` and `score` are the two
    fields every game guarantees and the only two GameHouse ever reads;
    everything else in the object is xsettlers' own and stays opaque.

    `direction` is declared even though nothing reads it yet. `placement` is
    direction-free -- 1 is best whether a game is won high or low -- but
    `score` is not, so a second game with inverted victory conditions would
    make "highest score" the wrong aggregate. Saying so now costs nothing and
    is the piece that would otherwise have to be guessed later.
    """
    return {
        "type": "object",
        "required": [PLACEMENT_FIELD, SCORE_FIELD],
        "direction": "higher_is_better",
        "properties": {
            PLACEMENT_FIELD: {"type": "integer",
                              "description": "Final ranking, 1 = winner."},
            SCORE_FIELD: {"type": "number",
                          "description": "Weighted final score. Comparable within "
                                         "this game only."},
        },
    }


def npc_profile_schema() -> dict:
    """
    The JSON schema an NPC roster entry must match, built from the strategy
    library rather than authored.

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
    satisfying the schema."""
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
    person_ids = {}   # xsettlers player id -> GameHouse person.id, person-kind only
    for i, p in enumerate(players):
        seat = roster_override[i]
        xsettlers_id = by_email[seat["email"]]
        entry = {"player_id": p["player_id"], "kind": p["kind"], "xsettlers_player_id": xsettlers_id}
        if p["kind"] == "person":
            entry["player_token"] = generated_tokens[p["player_id"]]
            person_ids[xsettlers_id] = p["player_id"]
        else:
            profile = p.get("profile") or {}
            assign_npc_profile(xsettlers_id, profile["strategy_ref"], config=profile.get("config"))
        result_players.append(entry)

    conn = get_connection(); cur = conn.cursor()
    # Stamp GameHouse's own person.id onto the rows bootstrap just created,
    # rather than threading the field through Seat/bootstrap_game(): Seat
    # lives in config/loader.py, and config/ must not carry GameHouse
    # concepts. Person-kind only -- see players.gamehouse_person_id in
    # db/schema.py for why NULL is load-bearing.
    for xsettlers_id, gh_person_id in person_ids.items():
        cur.execute("UPDATE players SET gamehouse_person_id=? WHERE id=?",
                    (gh_person_id, xsettlers_id))
    cur.execute("INSERT OR REPLACE INTO game_session (id, session_token) VALUES (1, ?)",
                (session_token,))
    conn.commit(); conn.close()

    return {"ok": True, "already_active": False, "scenario_name": SCENARIO_NAME,
            "players": result_players}


# ---------------------------------------------------------------------------
# Results hand-back: xsettlers acts as an MCP CLIENT again, once, at game over.
# ---------------------------------------------------------------------------

def build_results() -> list:
    """
    The recorded final scoreboard as GameHouse's `results` array, or [] if
    this game has no person-backed players to report.

    Reads `get_final_scores()` -- the persisted `game.final_scores` event --
    rather than recomputing standings. What GameHouse is told has to be what
    actually happened at the whistle, not a fresh calculation against state
    that may have moved on since.

    Keyed by GameHouse's own person.id, which is what lets each entry
    reattach to its game_journal row via (session_token, person_id) with no
    second identity mapping. Players with no `gamehouse_person_id` are
    silently absent: NPCs have no Person to attach a score to, and
    static-roster players were never GameHouse's to begin with."""
    final = get_final_scores()
    if not final:
        return []
    conn = get_connection()
    person_ids = {r["id"]: r["gamehouse_person_id"] for r in conn.execute(
        "SELECT id, gamehouse_person_id FROM players "
        "WHERE gamehouse_person_id IS NOT NULL").fetchall()}
    conn.close()

    results = []
    for standing in final["standings"]:
        person_id = person_ids.get(standing["player_id"])
        if person_id is None:
            continue
        results.append({
            "player_id": person_id,
            "score": {
                PLACEMENT_FIELD: standing["rank"],
                SCORE_FIELD: standing["score"],
                "energy": standing["energy"],
                "food": standing["food"],
                "goods": standing["goods"],
                "total": standing["total"],
                "display_name": standing["display_name"],
                "final_turn": final["final_turn"],
            },
        })
    return results


def _results_already_reported() -> bool:
    return read_one("SELECT id FROM events WHERE event_type=? LIMIT 1",
                    (RESULTS_REPORTED_EVENT,)) is not None


def _session_token():
    row = read_one("SELECT session_token FROM game_session WHERE id=1")
    return row["session_token"] if row else None


def _game_settled() -> bool:
    """
    True once the game is over and nothing GameHouse-related is still
    pending -- either this was never a GameHouse game, or the hand-back has
    already gone out. This is the gate for archive_active_database(): moving
    the live DB aside before a pending hand-back succeeds would strand the
    scoreboard GameHouse still needs to poll for, the same way archiving
    would break a plain (non-GameHouse) player's ability to read their final
    score if it ran before the game was actually over.
    """
    if not get_final_scores():
        return False
    return _session_token() is None or _results_already_reported()


async def report_results() -> dict:
    """
    Hand the finished game's scoreboard back to GameHouse, once.

    Returns a dict describing what happened; never raises. Like
    register_with_gamehouse, this is a call across an external boundary at a
    moment nothing else depends on, so a GameHouse that is down must not take
    anything here with it -- the attempt is simply not recorded, and the next
    poll tries again.

    Four conditions, in order, each of which means "nothing to do" rather
    than "something is wrong":
      - no session_token: this game did not come from GameHouse (a
        select_scenario game, or a designer harness DB), so there is nobody
        to report to. This is the guard that keeps the whole mechanism out of
        ../xsettlers-designer without designer knowing it exists.
      - no recorded final scores: the game is still being played.
      - already reported: a previous poll succeeded.
      - no person-backed players: an all-NPC game has no Person to credit.
    """
    session_token = _session_token()
    if not session_token:
        return {"ok": False, "skipped": "no GameHouse session for this game"}
    if not get_final_scores():
        return {"ok": False, "skipped": "game is not over"}
    if _results_already_reported():
        return {"ok": False, "skipped": "results already reported"}

    results = build_results()
    if not results:
        record_event(RESULTS_REPORTED_EVENT,
                     {"reported": [], "note": "no person-backed players to report"})
        return {"ok": True, "reported": 0}

    gamehouse_url = os.getenv("GAMEHOUSE_URL")
    if not gamehouse_url:
        return {"ok": False, "skipped": "GAMEHOUSE_URL is not set"}
    try:
        async with streamablehttp_client(gamehouse_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("report_results", arguments={
                    "session_token": session_token,
                    "results": results,
                })
    except Exception as exc:
        # Deliberately NOT recorded as reported: leaving the event unwritten
        # is what lets the next poll retry, and what lets a restart pick the
        # hand-back back up.
        return {"ok": False, "error": f"report_results call to {gamehouse_url} failed: {exc}"}

    record_event(RESULTS_REPORTED_EVENT, {"reported": [r["player_id"] for r in results],
                                          "results": results})
    return {"ok": True, "reported": len(results)}


async def _reporter_tick():
    """
    One pass of run_results_reporter's work, split out so a test can drive a
    single iteration without unrolling an infinite loop: report results if
    the game just ended, then archive the finished database once nothing is
    still waiting on it (see _game_settled()).
    """
    outcome = await report_results()
    if outcome.get("ok") and outcome.get("reported") is not None:
        print(f"Reported final results to GameHouse for "
              f"{outcome['reported']} player(s).")
    elif outcome.get("error"):
        print(f"Results hand-back failed (will retry): {outcome['error']}")

    if _game_settled():
        archived = archive_active_database()
        if archived:
            print(f"Game over -- archived finished database to {archived}")


async def run_results_reporter():
    """
    Watch for the game ending, hand results back, then archive.

    A poll rather than a hook at game-over, because both paths that can end a
    game -- engine/clock.py's tick and engine/turn.py's
    check_consensus_acceleration -- live in engine/, which may not import
    this module (CLAUDE.md's layering rule allows exactly one function-level
    exception and warns against a second). Polling from the server layer
    needs no change in engine/ at all."""
    while True:
        await asyncio.sleep(REPORT_POLL_SECONDS)
        try:
            await _reporter_tick()
        except Exception as exc:      # belt and braces: this loop must not die
            print(f"Results reporter error: {exc}")
