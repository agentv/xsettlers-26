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
# Importing these modules is what registers their tools -- @mcp_tool runs at
# import time and fills xsettlers_mcp.tools.registry.TOOLS. Nothing here lists
# tool names; the registry is the list, the schema and the dispatch table.
import xsettlers_mcp.game_select          # noqa: F401  (registers list_scenarios, select_scenario)
import xsettlers_mcp.gamehouse            # noqa: F401  (registers start_session)
import xsettlers_mcp.tools.player_tools   # noqa: F401
import xsettlers_mcp.tools.sector_tools   # noqa: F401
import xsettlers_mcp.tools.navigation_tools    # noqa: F401
import xsettlers_mcp.tools.organization_tools  # noqa: F401
import xsettlers_mcp.tools.organization_reports  # noqa: F401
import xsettlers_mcp.tools.task_force_tools     # noqa: F401
from xsettlers_mcp.gamehouse import register_with_gamehouse, run_results_reporter
from xsettlers_mcp.tools.registry import TOOLS
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
    "the neighborhood maps) built server-side by views/render.py, not assembled "
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
    return [types.Tool(name=name, description=spec["description"],
                       inputSchema=spec["schema"])
            for name, spec in TOOLS.items()]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    spec = TOOLS.get(name)
    fn = spec["fn"] if spec else None
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
    # The results reporter is gathered here, in the server layer, rather than
    # hooked into game-over: both paths that end a game live in engine/, which
    # may not import xsettlers_mcp/. It no-ops on every pass until a
    # GameHouse-started game actually finishes -- see run_results_reporter.
    await asyncio.gather(server.serve(), run_clock(), run_results_reporter())

if __name__ == "__main__":
    asyncio.run(main())
