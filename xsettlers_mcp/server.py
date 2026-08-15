import asyncio
import contextlib
import json
import os
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp import types
from xsettlers_mcp.game_select import list_scenarios, select_scenario
from xsettlers_mcp.gamehouse import start_session, register_with_gamehouse
from xsettlers_mcp.tools.player_tools import (
    get_player_state, declare_end_turn, rescind_end_turn, set_display_name
)
from xsettlers_mcp.tools.sector_tools import get_sector, get_sector_map, show_sector_neighborhood
from xsettlers_mcp.tools.navigation_tools import preview_move, confirm_move, cancel_move
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, set_pod_scan_bearing,
    rename_organization, set_org_scan_bearing, queue_command
)
from xsettlers_mcp.tools.organization_reports import (
    show_organization, show_civilization_status, show_game_status
)
from db.schema import init_schema
from engine.clock import run_clock
from views.render import render_status

# Sent once at MCP initialize, before the client sees any tool response --
# the strongest available lever for steering how an LLM client displays what
# this server sends back, short of controlling the client itself.
# Reinforced per-call by a matching directive block in call_tool()'s
# markdown_view response (below) -- one nudge at session start, one on every
# single call, since a long session can let a one-time instruction drift out
# of a client's attention.
SERVER_INSTRUCTIONS = (
    "Every tool response defaults to response_format='markdown_view': you get back "
    "a raw JSON block AND a pre-rendered markdown block (a table, or a map grid for "
    "show_sector_neighborhood) built server-side by views/render.py, not assembled "
    "by you. Render that markdown block to the user VERBATIM. Do not reconstruct, "
    "reformat, re-summarize, or build your own table from the JSON instead -- the "
    "JSON is there for your own reasoning and state-tracking (e.g. remembering an "
    "org_id), not as an alternate source to design a display from. Only deviate "
    "from the pre-rendered block if the player has explicitly asked for a "
    "different presentation than what was returned. If you want JSON with no "
    "markdown block at all (e.g. because you're only using the data internally and "
    "showing nothing to the player), call the tool with response_format='data_only'."
)

app = Server("xsettlers", instructions=SERVER_INSTRUCTIONS)

@app.list_tools()
async def list_tools():
    return [
        types.Tool(name="list_scenarios",
            description="List available game scenarios a player can choose to start/join. "
                        "Must be called (and select_scenario used to pick one) before any "
                        "other tool works -- until a scenario is selected, no game exists "
                        "and every other tool will report 'Player not found'.",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="select_scenario",
            description="Choose a scenario by name (see list_scenarios) to start playing. "
                        "Bootstraps the game on first selection; the MVP runs one shared "
                        "game per deployed instance, so this can't be used to switch "
                        "scenarios once one is already active.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"scenario_name":{"type":"string"}},
                "required":["player_token","scenario_name"]}),
        types.Tool(name="get_player_state",
            description="Dashboard: player record, all organizations, all pods",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="declare_end_turn",
            description="Player declares no further moves this tick",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="rescind_end_turn",
            description="Player rescinds their end turn declaration",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="set_display_name",
            description="Choose your own in-game display name, shown to every player on the "
                        "leaderboard -- independent of any name GameHouse or bootstrap supplied. "
                        "Must be unique game-wide.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"display_name":{"type":"string"}},
                "required":["player_token","display_name"]}),
        types.Tool(name="get_sector",
            description="Get a specific sector (player-scoped visibility)",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"sector_id":{"type":"integer"}},
                "required":["player_token","sector_id"]}),
        types.Tool(name="get_sector_map",
            description="All sectors visible to the calling player",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="show_sector_neighborhood",
            description="Map the neighborhood around one of your organizations (aka view/visualize the neighborhood). "
                        "Center is either an org_id -- the normal way to call it -- or explicit (center_x, center_y, center_z) "
                        "coordinates; a ship in transit has no location and can't be a center. Default radius 5 (an 11x11 grid), "
                        "max 10. Returns the complete lattice, not just known sectors: display.grid holds a ready-to-draw grid "
                        "with absolute coordinates on both axes and one <=3-character marker per cell (your orgs, rival presence, "
                        "'*' for seen-and-empty, '·' for never seen, or blank for out of range), plus display.legend. Pure view "
                        "-- reveals nothing, costs nothing, changes no confidence.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},
                "org_id":{"type":"integer"},
                "center_x":{"type":"integer"},
                "center_y":{"type":"integer"},
                "center_z":{"type":"integer"},
                "radius":{"type":"integer"}},
                "required":["player_token"]}),
        types.Tool(name="show_organization",
            description="Complete properties of one of the player's own organizations, including all pods. "
                        "Includes a display block with a ready-to-render header and the locked MVP cargo-table "
                        "column order (Task, Count, Energy, Food, Goods, Capacity as current/total).",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"org_id":{"type":"integer"}},
                "required":["player_token","org_id"]}),
        types.Tool(name="show_civilization_status",
            description="Player-scoped fleet report (aka fleet status / my status): turn context, all organizations (in-transit marked, with per-org tasking breakdown), and fleet-wide aggregate assets (including capacity and percent_full).",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="show_game_status",
            description="Public scoreboard: turn context plus every player's aggregate resource totals (energy/food/goods/total/percent_full), ranked highest-first. Does not reveal other players' fleet composition or position -- only aggregate totals are public.",
            inputSchema={"type":"object","properties":{"player_token":{"type":"string"}},
                         "required":["player_token"]}),
        types.Tool(name="preview_move",
            description="Preview a move: calculate travel time without committing",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"ship_id":{"type":"integer"},
                "dest_x":{"type":"integer"},"dest_y":{"type":"integer"},"dest_z":{"type":"integer"},
                "jump_range_per_turn":{"type":"integer"}},
                "required":["player_token","ship_id","dest_x","dest_y","dest_z"]}),
        types.Tool(name="confirm_move",
            description="Commit a previewed move. Ship enters transit until arrival turn.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"ship_id":{"type":"integer"},
                "dest_x":{"type":"integer"},"dest_y":{"type":"integer"},"dest_z":{"type":"integer"},
                "jump_range_per_turn":{"type":"integer"}},
                "required":["player_token","ship_id","dest_x","dest_y","dest_z"]}),
        types.Tool(name="cancel_move",
            description="Cancel a move in progress. Rubber-bands ship to origin sector.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"ship_id":{"type":"integer"}},
                "required":["player_token","ship_id"]}),
        types.Tool(name="set_mission",
            description="Set an organization's mission (idle/move/colonize/defend/attack). For mission='move', params must include dest_x/dest_y/dest_z (optionally jump_range_per_turn) -- delegates to the same confirm_move flow as the dedicated tool, so prefer preview_move first to check travel time.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"org_id":{"type":"integer"},
                "mission":{"type":"string"},"params":{"type":"object"}},
                "required":["player_token","org_id","mission"]}),
        types.Tool(name="set_pod_task",
            description="Set a pod's task -- what its crew does, as distinct from the parent organization's mission, which is what the vehicle does (idle/produce_energy/produce_food/produce_goods/scan). For scan, optionally aim it in the same call with a compass bearing (N/NE/E/SE/S/SW/W/NW/N2/E2/S2/W2) or explicit offset_x/y/z -- aim is relative to the pod's organization and survives a move.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"pod_id":{"type":"integer"},
                "task":{"type":"string"},"bearing":{"type":"string"},
                "offset_x":{"type":"integer"},"offset_y":{"type":"integer"},"offset_z":{"type":"integer"}},
                "required":["player_token","pod_id","task"]}),
        types.Tool(name="rename_organization",
            description="Give one of your own ships or colonies a name of your choosing (max 24 chars). "
                        "Names are how a player refers to a unit, so they must be unique among your own "
                        "organizations; defaults are short and sayable (S1..Sn for ships, C1 for a colony).",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"org_id":{"type":"integer"},"name":{"type":"string"}},
                "required":["player_token","org_id","name"]}),
        types.Tool(name="set_org_scan_bearing",
            description="Aim an organization's own sensors. Every ship and colony can scan one sector per turn "
                        "on its own account -- a ship's bridge, a colony's headquarters -- without dedicating a "
                        "pod to it. Scanning is scanning: identical rules to a scan pod (same food cost, range, "
                        "transit suppression). Aim by compass bearing (N/NE/E/SE/S/SW/W/NW, or N2/E2/S2/W2 for "
                        "two sectors out) or by explicit offset_x/y/z. The aim is RELATIVE to the org's own "
                        "sector and persists across turns, so it survives a move with no re-aiming. Out-of-range "
                        "aims are rejected outright. Pass neither bearing nor offset to clear it.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"org_id":{"type":"integer"},"bearing":{"type":"string"},
                "offset_x":{"type":"integer"},"offset_y":{"type":"integer"},"offset_z":{"type":"integer"}},
                "required":["player_token","org_id"]}),
        types.Tool(name="set_pod_scan_bearing",
            description="Aim a pod already on the scan task. Same rules and same bearing vocabulary as "
                        "set_org_scan_bearing -- scanning is scanning, whoever carries the equipment. Relative "
                        "aim, persists across turns, out-of-range rejected. Pass neither bearing nor offset to clear.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"pod_id":{"type":"integer"},"bearing":{"type":"string"},
                "offset_x":{"type":"integer"},"offset_y":{"type":"integer"},"offset_z":{"type":"integer"}},
                "required":["player_token","pod_id"]}),
        types.Tool(name="queue_command",
            description="Queue a one-shot command for an organization, resolved automatically by the engine "
                        "instead of you having to call the underlying tool again by hand. Four trigger_phase "
                        "values: 'during_transit' fires the instant this org next enters transit (only "
                        "action='set_pod_task' is legal here -- pod tasking is the one thing not locked by a "
                        "departing org); 'before_arrival' fires the same tick this org's current move resolves; "
                        "'after_arrival' fires exactly one turn later -- both require the org to already be in "
                        "transit; 'at_turn' fires at an explicit absolute turn (pass `turn`), independent of any "
                        "move, for orders that don't fit the arrival-relative phases (e.g. \"on turn 7, jump "
                        "somewhere else\"). Action whitelist: 'move' (either dest_x/dest_y/dest_z absolute or "
                        "d_x/d_y/d_z relative to wherever the org is when the order fires, never both, plus "
                        "optional jump_range_per_turn); 'set_pod_task' (params: pod_id, task, optionally "
                        "bearing/offset_x/y/z -- same shape set_pod_task takes); 'colonize' (no params -- commits "
                        "the ship at whatever sector it occupies when the order fires, and is refused if it "
                        "cannot afford the energy by then); 'aim_scan' (optionally bearing/offset_x/y/z, same "
                        "shape set_org_scan_bearing takes; pass none to clear the aim). If you "
                        "give the org new orders before a before_arrival/after_arrival/at_turn command fires, "
                        "the queued one is silently dropped rather than overriding your manual orders.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"org_id":{"type":"integer"},
                "trigger_phase":{"type":"string"},"action":{"type":"string"},"params":{"type":"object"},
                "turn":{"type":"integer"}},
                "required":["player_token","org_id","trigger_phase","action"]}),
        types.Tool(name="start_session",
            description="GameHouse handoff: called once GameHouse closes a lobby, to actually hand the game "
                        "off. players is a list of {player_id, kind: 'person'|'npc', profile?} entries -- "
                        "person player_ids are GameHouse's real person.id, npc player_ids are GameHouse-minted "
                        "ephemeral labels with a profile.strategy_name matching the npc_profile_schema this "
                        "game registered via register_game. Bootstraps the game and returns each entry's "
                        "xsettlers_player_id, plus a freshly generated player_token for person-kind entries -- "
                        "GameHouse is responsible for relaying that token back to the actual human player. "
                        "Not something a player calls directly.",
            inputSchema={"type":"object","properties":{
                "session_token":{"type":"string"},
                "scenario_key":{"type":["string","null"]},
                "players":{"type":"array","items":{"type":"object"}}},
                "required":["session_token","players"]}),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    dispatch = {
        "list_scenarios":             list_scenarios,
        "select_scenario":            select_scenario,
        "get_player_state":           get_player_state,
        "declare_end_turn":           declare_end_turn,
        "rescind_end_turn":           rescind_end_turn,
        "set_display_name":           set_display_name,
        "get_sector":                 get_sector,
        "get_sector_map":             get_sector_map,
        "show_sector_neighborhood":   show_sector_neighborhood,
        "show_organization":          show_organization,
        "show_civilization_status":   show_civilization_status,
        "show_game_status":           show_game_status,
        "preview_move":               preview_move,
        "confirm_move":               confirm_move,
        "cancel_move":                cancel_move,
        "set_mission":                set_mission,
        "set_pod_task":               set_pod_task,
        "set_pod_scan_bearing":       set_pod_scan_bearing,
        "rename_organization":        rename_organization,
        "set_org_scan_bearing":       set_org_scan_bearing,
        "queue_command":              queue_command,
        "start_session":              start_session,
    }
    fn = dispatch.get(name)
    if not fn:
        return [types.TextContent(type="text",
                                  text=json.dumps({"error": f"Unknown tool: {name}"}))]

    # response_format is a dispatch-level convention, not a per-tool schema
    # property -- none of the inputSchemas above declare it, and none need to,
    # since jsonschema.validate() (in the SDK's call_tool wrapper) only rejects
    # extra properties when a schema sets additionalProperties: false, which
    # none of these do. Popped here so it never reaches a tool function.
    #
    # Three values:
    #   markdown_view (default) -- JSON + markdown table, for a client showing both.
    #   data_only               -- JSON alone, for a client that renders its own view.
    #   html_svg                -- reserved for a future rendered-graphics response;
    #                              not built yet, so it's treated as markdown_view
    #                              (JSON + markdown table) until it exists, same as
    #                              any other value that isn't data_only.
    arguments = dict(arguments)
    response_format = arguments.pop("response_format", "markdown_view")
    result = fn(**arguments)

    if response_format == "data_only":
        return [types.TextContent(type="text", text=_as_json(result))]
    return [types.TextContent(type="text", text=_as_json(result)),
            types.TextContent(type="text", text=_as_markdown(result)),
            types.TextContent(type="text", text=RENDER_DIRECTIVE)]


# Reinforces SERVER_INSTRUCTIONS (sent once at initialize) on every single
# markdown_view response -- a long session can let a one-time instruction
# drift out of a client's attention, so this repeats the same directive right
# next to the content it governs, on every call, not just at session start.
RENDER_DIRECTIVE = (
    "[Render the markdown block above verbatim to the player. Do not "
    "reconstruct, reformat, or summarize a table from the JSON block instead "
    "-- the JSON is for your own reasoning/state-tracking, not a display "
    "source. Only deviate if the player explicitly asked for a different "
    "presentation than what was returned.]"
)


def _as_markdown(result) -> str:
    """
    Render a tool's result as a markdown table, following the display-hints
    convention in views/render.py. Tools that return a bare list rather than a
    dict (get_sector_map, list_scenarios) have no `display` block to key off,
    so they fall back to a plain notice instead of crashing render_status's
    dict-only `.get()` calls.
    """
    if not isinstance(result, dict):
        return "(no table view for this tool's response shape)"
    return render_status(result)


def _as_json(result) -> str:
    """
    Serialize a tool's return value as JSON.

    `default=str` is a backstop, not a design: every field a tool returns today
    is a primitive out of sqlite3, but a response that fails to serialize would
    fail the whole call, and losing type fidelity on some future stray value is
    a far better outcome than a tool that errors at the transport layer.
    """
    return json.dumps(result, default=str)

# Streamable HTTP transport -- deployed on Fly.io (see fly.toml), which routes
# network traffic to internal_port 8080. stdio (piped stdin/stdout) only works
# for a client that spawns this process locally, which a remotely-hosted
# deployment rules out.
#
# stateless=True: each HTTP request gets its own throwaway transport/session,
# matching how every tool function already re-opens its own DB connection per
# call with no session state carried between requests.
session_manager = StreamableHTTPSessionManager(app=app, stateless=True)

async def health(request):
    return PlainTextResponse("ok")

# SECURITY POSTURE: /mcp IS OPEN. There is no perimeter auth.
#
# A static `Authorization: Bearer` gate is not an option here: MCP client
# connector flows accept a server URL and optionally OAuth, with no field for
# a static header, so a connector pointed at a header-gated endpoint can only
# ever receive a 401.
#
# What stands between the internet and the game is `player_token` alone --
# every tool resolves it against the roster and rejects anything else. Note
# the two things that makes true right now, neither of them comfortable:
#
#   1. The roster in config/game_config.yaml holds placeholder tokens
#      (REPLACE_WITH_GENERATED_TOKEN_*) and that file is in a PUBLIC repo, as
#      is this server's URL. Anyone who reads the repo can play as anyone.
#   2. There is no rate limiting, so nothing slows a caller down.
#
# Accepted knowingly: the game holds nothing of value and nobody knows it
# exists. Both facts stop being true the moment either changes. Real hardening
# means OAuth on this endpoint plus per-player tokens that live somewhere
# other than git -- see docs/TODO.md.

class _MCPASGIApp:
    """
    Wraps session_manager.handle_request as a plain-class ASGI callable.
    Route() only passes an endpoint through unwrapped when it's neither a
    function nor a method (inspect.isfunction/ismethod) -- a bound method or
    bare async function gets wrapped in request_response(), which calls
    endpoint(request) instead of (scope, receive, send) and breaks at request
    time. A class instance sidesteps that check. Using Route (not Mount) also
    avoids Mount's trailing-slash subpath matching, which would 307-redirect
    POST /mcp to /mcp/.
    """
    async def __call__(self, scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

@contextlib.asynccontextmanager
async def lifespan(_app):
    async with session_manager.run():
        yield

starlette_app = Starlette(
    routes=[
        Route("/health", health),
        Route("/mcp", _MCPASGIApp()),
    ],
    lifespan=lifespan,
)

async def main():
    # Tables only -- no seeding. bootstrap_game() runs lazily, triggered by
    # the first successful select_scenario() call. Until then, players is
    # empty and every gameplay tool naturally rejects with "Player not found".
    init_schema()
    # Best-effort, not fatal: GAMEHOUSE_URL/XSETTLERS_PUBLIC_URL are both
    # unset in most dev environments (nothing to register with, and that's a
    # normal, supported case -- see xsettlers_mcp/gamehouse.py's
    # register_with_gamehouse docstring), and even when set, GameHouse being
    # unreachable shouldn't block xsettlers from serving players directly.
    registration = await register_with_gamehouse()
    if registration.get("ok"):
        print(f"Registered with GameHouse: {registration.get('response')}")
    else:
        print(f"GameHouse registration skipped/failed: {registration.get('error')}")
    port = int(os.getenv("PORT", 8080))
    config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), run_clock())

if __name__ == "__main__":
    asyncio.run(main())
