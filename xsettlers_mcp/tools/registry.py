"""
The MCP tool surface, declared once per tool at the function itself.

A tool used to be declared in three places that had to agree by hand: an
import in server.py, a hand-written inputSchema in list_tools(), and an entry
in call_tool()'s dispatch dict. Twenty-two tools times three declarations is
sixty-six chances to disagree, and one of them cost a live cross-process
handoff -- start_session's schema said scenario_key was a string, which JSON
Schema rejects for null, while the Python happily accepted None.

Now `@mcp_tool(description=...)` on the function is the whole declaration. The
schema is derived from the signature, which cannot drift from the parameters
the function actually takes, and the dispatch table is the registry itself.

The description stays hand-written rather than lifted from the docstring: it
is addressed to a language model deciding whether to call this tool, while the
docstring is addressed to whoever maintains it. They are different audiences
and, in practice, different text.
"""
import inspect

# Registration order is preserved, so list_tools() reports tools in the order
# their modules are imported rather than alphabetically -- selection and
# player-state tools first, which is roughly the order a client needs them.
TOOLS = {}

JSON_TYPES = {int: "integer", str: "string", bool: "boolean",
              float: "number", dict: "object", list: "array"}

PLAYER_TOKEN_DESCRIPTION = "The calling player's opaque token."


def mcp_tool(description: str, **schema_overrides):
    """
    Register a function as an MCP tool.

    `description` is what the client sees. `schema_overrides` replaces the
    derived entry for a named parameter, for the rare type a signature cannot
    express -- e.g. a list whose items have a shape worth declaring.
    """
    def register(fn):
        TOOLS[fn.__name__] = {"fn": fn, "description": description,
                              "schema": _schema_for(fn, schema_overrides)}
        return fn
    return register


def _schema_for(fn, overrides: dict) -> dict:
    """
    An inputSchema built from the function's own signature.

    A @player_tool is written as fn(session, ...) but called as
    fn(player_token, ...), so the session parameter is dropped and
    player_token put in its place -- see xsettlers_mcp/tools/session.py.

    A parameter with a default is optional, and a parameter defaulting to None
    is additionally declared nullable. That second rule matters: a caller that
    sends an optional field explicitly as null (GameHouse does, for every
    handoff) fails validation against a bare "type": "string" before the tool
    body ever runs.
    """
    properties, required = {}, []
    if getattr(fn, "takes_player_token", False):
        properties["player_token"] = {"type": "string",
                                      "description": PLAYER_TOKEN_DESCRIPTION}
        required.append("player_token")

    parameters = list(inspect.signature(fn).parameters.values())
    if getattr(fn, "takes_player_token", False):
        parameters = parameters[1:]          # drop `session`

    for parameter in parameters:
        json_type = JSON_TYPES.get(parameter.annotation, "string")
        optional = parameter.default is not inspect.Parameter.empty
        if optional and parameter.default is None:
            properties[parameter.name] = {"type": [json_type, "null"]}
        else:
            properties[parameter.name] = {"type": json_type}
        if not optional:
            required.append(parameter.name)

    properties.update(overrides)
    return {"type": "object", "properties": properties, "required": required}
