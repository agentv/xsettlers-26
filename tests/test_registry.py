"""
The MCP tool surface is derived from the functions themselves
(xsettlers_mcp/tools/registry.py). These tests are what makes that derivation
trustworthy: a schema that disagrees with its function is exactly the class of
bug that used to reach production, because nothing compared the two.
"""
import asyncio
import inspect
import jsonschema
import pytest

from xsettlers_mcp.server import list_tools
from xsettlers_mcp.tools.registry import TOOLS

TOOL_LIST = asyncio.run(list_tools())


def test_every_registered_tool_is_listed():
    assert {t.name for t in TOOL_LIST} == set(TOOLS)
    assert len(TOOL_LIST) == 31


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_matches_what_the_function_accepts(name):
    """Every declared property is a real parameter, and every parameter with no
    default is declared required -- so a client obeying the schema cannot
    produce a TypeError, and cannot be refused for omitting something optional."""
    spec = TOOLS[name]
    fn = spec["fn"]
    parameters = list(inspect.signature(fn).parameters.values())
    if getattr(fn, "takes_player_token", False):
        parameters = parameters[1:]

    declared = set(spec["schema"]["properties"])
    actual = {p.name for p in parameters}
    if getattr(fn, "takes_player_token", False):
        actual.add("player_token")
    assert declared == actual

    mandatory = {p.name for p in parameters if p.default is inspect.Parameter.empty}
    if getattr(fn, "takes_player_token", False):
        mandatory.add("player_token")
    assert set(spec["schema"]["required"]) == mandatory


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_is_valid_json_schema(name):
    jsonschema.Draft7Validator.check_schema(TOOLS[name]["schema"])


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_parameters_defaulting_to_none_accept_an_explicit_null(name):
    """A caller that sends such a field explicitly as null must validate.
    GameHouse does this on every handoff, and a bare "type": "string" rejects
    it before the tool body -- which is how a live cross-process handoff broke
    once (see tests/test_gamehouse.py).

    Deliberately scoped to parameters whose default is None. A parameter with
    a real default (radius, confidence) is optional but NOT nullable:
    the function uses it in arithmetic and would raise on None, so the schema
    refusing null is the honest contract -- omit the field to get the default.
    """
    spec = TOOLS[name]
    schema = spec["schema"]
    payload = {}
    for field in schema["required"]:
        declared = schema["properties"][field]["type"]
        primary = declared[0] if isinstance(declared, list) else declared
        payload[field] = {"string": "x", "integer": 1, "number": 1.0,
                          "boolean": True, "object": {}, "array": []}[primary]

    parameters = inspect.signature(spec["fn"]).parameters
    for field in set(schema["properties"]) - set(schema["required"]):
        parameter = parameters.get(field)
        if parameter is not None and parameter.default is None:
            jsonschema.validate({**payload, field: None}, schema)
