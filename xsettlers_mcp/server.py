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
from xsettlers_mcp.tools.player_tools import get_player_state, declare_end_turn, rescind_end_turn
from xsettlers_mcp.tools.sector_tools import get_sector, get_sector_map, show_sector_neighborhood
from xsettlers_mcp.tools.navigation_tools import preview_move, confirm_move, cancel_move
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, set_pod_scan_bearing, show_organization,
    rename_organization, set_org_scan_bearing,
    show_civilization_status, show_game_status
)
from db.schema import init_schema
from engine.clock import run_clock
from views.render import render_status

app = Server("xsettlers")

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
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    dispatch = {
        "list_scenarios":             list_scenarios,
        "select_scenario":            select_scenario,
        "get_player_state":           get_player_state,
        "declare_end_turn":           declare_end_turn,
        "rescind_end_turn":           rescind_end_turn,
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
    arguments = dict(arguments)
    response_format = arguments.pop("response_format", "markdown_view")
    result = fn(**arguments)

    content = [types.TextContent(type="text", text=_as_json(result))]
    if response_format != "data_only":
        content.append(types.TextContent(type="text", text=_as_markdown(result)))
    return content


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

    Tools return plain dicts and lists, but this used to go over the wire as
    `str(result)` -- Python's repr, with single quotes and True/None. An LLM
    client reads that fine, which is why it survived this long; anything that
    actually parses the payload cannot, since it is not JSON and never was.
    Responses are JSON now, which is what a tool result is supposed to be.

    `default=str` is a backstop, not a design: every field a tool returns today
    is a primitive out of sqlite3, but a response that fails to serialize would
    fail the whole call, and losing type fidelity on some future stray value is
    a far better outcome than a tool that errors at the transport layer.
    """
    return json.dumps(result, default=str)

# Streamable HTTP transport -- deployed on Fly.io (see fly.toml), which routes
# network traffic to internal_port 8080; stdio (piped stdin/stdout) only works
# for a client that spawns this process locally, which Slackbot calling a
# remote Fly.io deployment cannot do.
#
# stateless=True: each HTTP request gets its own throwaway transport/session,
# matching how every tool function already re-opens its own DB connection per
# call with no session state carried between requests.
session_manager = StreamableHTTPSessionManager(app=app, stateless=True)

async def health(request):
    return PlainTextResponse("ok")

# SECURITY POSTURE: /mcp IS OPEN. There is no perimeter auth.
#
# The `Authorization: Bearer <MCP_SHARED_SECRET>` gate that used to sit here
# was removed on 2026-07-31, deliberately. It was incompatible with the way
# MCP clients actually connect: Claude's custom-connector flow accepts a
# server URL and optionally OAuth, with no field for a static header, so a
# connector pointed at this server could only ever receive a 401. Choosing
# between "reachable from Slack" and "has a perimeter", the perimeter went.
#
# What now stands between the internet and the game is `player_token` alone --
# every tool resolves it against the roster and rejects anything else. That
# was always the real gate; the shared secret only ever decided who could
# knock. But note the two things that makes true right now, neither of them
# comfortable:
#
#   1. The roster in config/game_config.yaml holds placeholder tokens
#      (REPLACE_WITH_GENERATED_TOKEN_*) and that file is in a PUBLIC repo, as
#      is this server's URL. Anyone who reads the repo can play as anyone.
#   2. There is no rate limiting, so nothing slows a caller down.
#
# Accepted knowingly for now: the game holds nothing of value and nobody knows
# it exists. Both facts stop being true the moment either changes. Real
# hardening means OAuth on this endpoint plus per-player tokens that live
# somewhere other than git -- see docs/TODO.md.

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
    # Tables only -- no seeding. bootstrap_game() now runs lazily, triggered
    # by the first successful select_scenario() call. Until then, players is
    # empty and every gameplay tool naturally rejects with "Player not found".
    init_schema()
    port = int(os.getenv("PORT", 8080))
    config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), run_clock())

if __name__ == "__main__":
    asyncio.run(main())
