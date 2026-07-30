import asyncio
import contextlib
import hmac
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
    set_mission, set_pod_mission, set_pod_scan_target, show_organization,
    show_civilization_status, show_game_status
)
from db.schema import init_schema
from engine.clock import run_clock

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
            description="All visible sectors within Euclidean distance 2 of a center. Center is either an org_id or explicit (center_x, center_y, center_z) coordinates.",
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
        types.Tool(name="set_pod_mission",
            description="Set a pod's mission (idle/produce_energy/produce_food/produce_goods/scan). For scan, optionally include target_x/target_y/target_z (all three, or none) -- response includes in_range so you know immediately if the target is out of scan range (fix it before end of turn or it'll cost food with no reveal).",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"pod_id":{"type":"integer"},
                "mission":{"type":"string"},
                "target_x":{"type":"integer"},"target_y":{"type":"integer"},"target_z":{"type":"integer"}},
                "required":["player_token","pod_id","mission"]}),
        types.Tool(name="set_pod_scan_target",
            description="Set or update the scan target coordinate for a pod in scan mission -- response includes in_range so you know immediately if the target is out of scan range.",
            inputSchema={"type":"object","properties":{
                "player_token":{"type":"string"},"pod_id":{"type":"integer"},
                "target_x":{"type":"integer"},"target_y":{"type":"integer"},"target_z":{"type":"integer"}},
                "required":["player_token","pod_id","target_x","target_y","target_z"]}),
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
        "set_pod_mission":            set_pod_mission,
        "set_pod_scan_target":        set_pod_scan_target,
    }
    fn = dispatch.get(name)
    if not fn:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    return [types.TextContent(type="text", text=str(fn(**arguments)))]

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

# Stopgap perimeter auth: a single static shared secret, not per-player keys.
# This only gates *reaching* /mcp at all -- it does not verify that a given
# player_token in a request's arguments really is who it claims to be (every
# tool still trusts player_token at face value; see docs/TODO.md). It stops
# an anonymous internet stranger from calling tools; it does not stop
# whoever holds the secret from impersonating any player in the roster.
# Fails closed: if MCP_SHARED_SECRET isn't set, every request is rejected
# rather than silently left open -- avoids "forgot to configure it" in prod.
MCP_SHARED_SECRET = os.getenv("MCP_SHARED_SECRET")

def _authorized(scope) -> bool:
    if not MCP_SHARED_SECRET:
        return False
    headers = dict(scope.get("headers") or [])
    presented = headers.get(b"authorization", b"").decode("utf-8", "ignore")
    expected = f"Bearer {MCP_SHARED_SECRET}"
    return hmac.compare_digest(presented, expected)

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
        if not _authorized(scope):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
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
