import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from mcp.game_select import list_scenarios, select_scenario
from mcp.tools.player_tools import get_player_state, declare_end_turn, rescind_end_turn
from mcp.tools.sector_tools import get_sector, get_sector_map, show_sector_neighborhood
from mcp.tools.navigation_tools import (
    get_organizations_in_range, preview_move, confirm_move, cancel_move
)
from mcp.tools.organization_tools import (
    set_mission, set_pod_mission, set_pod_scan_target, show_organization, show_game_status
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
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="select_scenario",
            description="Choose a scenario by name (see list_scenarios) to start playing. "
                        "Bootstraps the game on first selection; the MVP runs one shared "
                        "game per deployed instance, so this can't be used to switch "
                        "scenarios once one is already active.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"scenario_name":{"type":"string"}},
                "required":["slack_user_id","scenario_name"]}),
        types.Tool(name="get_player_state",
            description="Dashboard: player record, all organizations, all pods",
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="declare_end_turn",
            description="Player declares no further moves this tick",
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="rescind_end_turn",
            description="Player rescinds their end turn declaration",
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="get_sector",
            description="Get a specific sector (player-scoped visibility)",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"sector_id":{"type":"integer"}},
                "required":["slack_user_id","sector_id"]}),
        types.Tool(name="get_sector_map",
            description="All sectors visible to the calling player",
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="show_sector_neighborhood",
            description="All visible sectors within Euclidean distance 2 of a center. Center is either an org_id or explicit (center_x, center_y, center_z) coordinates.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},
                "org_id":{"type":"integer"},
                "center_x":{"type":"integer"},
                "center_y":{"type":"integer"},
                "center_z":{"type":"integer"},
                "radius":{"type":"integer"}},
                "required":["slack_user_id"]}),
        types.Tool(name="show_organization",
            description="Complete properties of one of the player's own organizations, including all pods.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"org_id":{"type":"integer"}},
                "required":["slack_user_id","org_id"]}),
        types.Tool(name="show_game_status",
            description="Player-scoped game summary: turn context, all organizations (in-transit marked), and aggregate asset totals.",
            inputSchema={"type":"object","properties":{"slack_user_id":{"type":"string"}},
                         "required":["slack_user_id"]}),
        types.Tool(name="get_organizations_in_range",
            description="Sectors within jump range of a player's ship",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"ship_id":{"type":"integer"},
                "jump_range":{"type":"integer"}},
                "required":["slack_user_id","ship_id","jump_range"]}),
        types.Tool(name="preview_move",
            description="Preview a move: calculate travel time without committing",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"ship_id":{"type":"integer"},
                "dest_sector_id":{"type":"integer"},
                "jump_range_per_turn":{"type":"integer"}},
                "required":["slack_user_id","ship_id","dest_sector_id"]}),
        types.Tool(name="confirm_move",
            description="Commit a previewed move. Ship enters transit until arrival turn.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"ship_id":{"type":"integer"},
                "dest_sector_id":{"type":"integer"},
                "jump_range_per_turn":{"type":"integer"}},
                "required":["slack_user_id","ship_id","dest_sector_id"]}),
        types.Tool(name="cancel_move",
            description="Cancel a move in progress. Rubber-bands ship to origin sector.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"ship_id":{"type":"integer"}},
                "required":["slack_user_id","ship_id"]}),
        types.Tool(name="set_mission",
            description="Set an organization's mission (idle/move/colonize/defend/attack)",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"org_id":{"type":"integer"},
                "mission":{"type":"string"},"params":{"type":"object"}},
                "required":["slack_user_id","org_id","mission"]}),
        types.Tool(name="set_pod_mission",
            description="Set a pod's mission (idle/produce_energy/produce_food/produce_goods/scan). For scan, optionally include target_sector_id.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"pod_id":{"type":"integer"},
                "mission":{"type":"string"},
                "target_sector_id":{"type":"integer"}},
                "required":["slack_user_id","pod_id","mission"]}),
        types.Tool(name="set_pod_scan_target",
            description="Set or update the scan target sector for a pod in scan mission.",
            inputSchema={"type":"object","properties":{
                "slack_user_id":{"type":"string"},"pod_id":{"type":"integer"},
                "target_sector_id":{"type":"integer"}},
                "required":["slack_user_id","pod_id","target_sector_id"]}),
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
        "show_game_status":           show_game_status,
        "get_organizations_in_range": get_organizations_in_range,
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

async def main():
    # Tables only -- no seeding. bootstrap_game() now runs lazily, triggered
    # by the first successful select_scenario() call. Until then, players is
    # empty and every gameplay tool naturally rejects with "Player not found".
    init_schema()
    async with stdio_server() as (read_stream, write_stream):
        await asyncio.gather(
            app.run(read_stream, write_stream, app.create_initialization_options()),
            run_clock()
        )

if __name__ == "__main__":
    asyncio.run(main())
