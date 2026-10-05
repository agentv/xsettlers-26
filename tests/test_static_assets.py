import asyncio

from db.connection import connection, read_value
from db.static_assets import get_or_render
from engine.turn import end_of_turn
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


def _stored_rows():
    return read_value("SELECT COUNT(*) FROM static_assets", default=0)


def test_repeat_request_with_the_same_data_reuses_the_stored_body():
    renders = []

    def render(data):
        renders.append(data)
        return f"<svg>{data['x']}</svg>"

    assert get_or_render("card", {"x": 1}, render) == "<svg>1</svg>"
    assert get_or_render("card", {"x": 1}, render) == "<svg>1</svg>"
    assert len(renders) == 1


def test_changed_data_renders_again_and_keeps_both_bodies_apart():
    def render(data):
        return f"<svg>{data['x']}</svg>"

    assert get_or_render("card", {"x": 1}, render) == "<svg>1</svg>"
    assert get_or_render("card", {"x": 2}, render) == "<svg>2</svg>"
    assert _stored_rows() == 2


def test_the_asset_name_is_part_of_the_key():
    assert get_or_render("card", {"x": 1}, lambda d: "card") == "card"
    assert get_or_render("neighborhood", {"x": 1}, lambda d: "hood") == "hood"


def test_tick_clears_the_memo():
    get_or_render("card", {"x": 1}, lambda d: "<svg/>")
    assert _stored_rows() == 1
    end_of_turn()
    assert _stored_rows() == 0


def test_unavailable_store_falls_back_to_a_live_render():
    with connection() as conn:
        conn.execute("DROP TABLE static_assets")
    assert get_or_render("card", {"x": 1}, lambda d: "<svg/>") == "<svg/>"


def test_org_card_is_memoized_until_its_organization_changes():
    from xsettlers_mcp.server import call_tool
    pid = seed_player(player_token="U_TEST_001")
    sid = seed_sector(); oid = seed_ship(pid, sid); seed_pod(oid, task="produce_energy")
    args = {"player_token": "U_TEST_001", "org_id": oid, "response_format": "html_svg"}

    first = asyncio.run(call_tool("show_organization", args))[1].text
    again = asyncio.run(call_tool("show_organization", args))[1].text
    assert again == first
    assert _stored_rows() == 1

    with connection() as conn:
        conn.execute("UPDATE organizations SET mission='move' WHERE id=?", (oid,))
    changed = asyncio.run(call_tool("show_organization", args))[1].text
    assert changed != first
    assert _stored_rows() == 2
