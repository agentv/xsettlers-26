"""
Read-only status reports for a player's organizations, and the public
scoreboard. These three tools mutate nothing -- they read state and shape it
for display.

The `display` block each report returns is a presentation hint, not a
rendering: it names which field holds the rows and which columns to show, so
views/render.py can draw any of them with no per-tool special-casing. Raw
fields are always present alongside, for a client that would rather build its
own view. Turning one value into the string a player reads is views/format.py's
job; what this module owns is the queries and which fields go in the block.
"""
from xsettlers_mcp.tools.registry import mcp_tool
from config.loader import load_config
from xsettlers_mcp.tools.session import player_tool, ORG_NOT_OWNED
from engine.production import org_production
from engine.turn import get_next_tick_at, get_final_scores, TURN_LIMIT
from engine.scoring import player_standings
from engine.scanning import aim_label, scanners_on
from views.format import (RESOURCE_ABBREV, TASK_ABBREV, TASK_DISPLAY,
                          resource_summary, scanner_footer, short_name,
                          stacked_header, tasking_summary, tick_countdown,
                          totals_footer, turn_header, winners_label)
import os

def _scanners_on(cur, org: dict) -> list:
    """
    engine.scanning.scanners_on rendered for reading: {"source", "bearing",
    "aimed"} dicts, where `bearing` is the compass name or the raw offset.

    An unaimed scan pod still costs its food and reveals nothing, so it is
    listed and flagged rather than silently dropped (see set_pod_task's
    docstring). views.format.scanner_footer turns this list into the one-line
    summary.
    """
    return [{"source": s["source"],
             "bearing": aim_label(s["offset"]) if s["offset"] else None,
             "aimed": s["offset"] is not None}
            for s in scanners_on(cur, org)]

@mcp_tool(
    "Complete properties of one of the player's own organizations, "
    "including all pods. Includes a display block with a ready-to-render "
    "header and the locked MVP cargo-table column order (Task, Count, "
    "Energy, Food, Goods, Capacity as current/total).")
@player_tool
def show_organization(sess, org_id: int) -> dict:
    """
    Return the complete properties of one of the player's own organizations:
    org record (type, mission, mission_params, is_mobile, sector location,
    task_force_id if it's a task force member) plus pods grouped by task — count of pods on each task, total
    capacity of that group, and what's actually stored there broken down by
    resource type (energy/food/goods). Storage is generic per pod and
    independent of current task (see engine/turn.py), so a task group's
    contents don't necessarily match its own task -- e.g. produce_goods pods
    can still be holding energy leftover from before a retask. Individual
    pods aren't listed separately; a ship with 6 pods reads as up to 3 task
    rows, not 6 pod rows.
    display: presentation hints (see views/format.py) -- a ready-to-render header ("<name> — at (x,y,z), <mission>")
    and the locked MVP column order for the cargo table (Task, Count, Energy,
    Food, Goods, Capacity). Each task row also gets task_display (e.g.
    "produce_energy" -> "Energy") and capacity_display ("current/total", e.g.
    "200/200") alongside the raw fields -- all raw fields stay present, this
    is purely additive. `display.rows_key` ("tasks") names which top-level
    field holds the row list, for a generic renderer -- see views/render.py.
    `scanners` (see _scanners_on) lists every active scanner on this org
    -- innate sensors plus scan-task pods -- and `display.footer`, when
    present, is that same information as a ready-to-render line below the
    table ("Scans: North, South, Southeast").
    Ownership-gated — only the calling player's orgs are accessible.
    """
    cur = sess.cur
    cur.execute("""
        SELECT o.id, o.org_type, o.name, o.mission, o.mission_params,
               o.is_mobile, o.sector_id, o.task_force_id,
               o.scan_offset_x, o.scan_offset_y, o.scan_offset_z,
               s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.id = ? AND o.player_id = ?""", (org_id, sess.player_id))
    org = cur.fetchone()
    if not org:
        return {"error": ORG_NOT_OWNED}
    result = dict(org)
    cur.execute("""
        SELECT task, COUNT(*) AS count,
               SUM(storage_capacity) AS capacity,
               SUM(energy_stored) AS energy,
               SUM(food_stored) AS food,
               SUM(goods_stored) AS goods
        FROM pods WHERE org_id = ? GROUP BY task""", (org_id,))
    result["tasks"] = [dict(t) for t in cur.fetchall()]
    for t in result["tasks"]:
        t["task_display"] = TASK_DISPLAY.get(t["task"], t["task"])
        current = (t["energy"] or 0) + (t["food"] or 0) + (t["goods"] or 0)
        t["capacity_display"] = f"{current:.0f}/{t['capacity']:.0f}"
        # Whole numbers in the table: nothing in the game yields a fraction of
        # a resource, so a trailing ".0" on every cell is noise. The raw
        # columns stay floats for a client that computes with them, the same
        # split show_game_status makes with its *_display fields.
        for resource in RESOURCE_ABBREV:
            t[f"{resource}_display"] = f"{t[resource] or 0:.0f}"
    status = (f"at ({org['coord_x']},{org['coord_y']},{org['coord_z']})"
              if org["sector_id"] != -1 else "in transit")
    result["short_name"] = short_name(org["name"])
    result["status"] = status
    scanners = _scanners_on(cur, org)
    result["scanners"] = scanners
    result["display"] = {
        "header": f"{org['name']} — {status}, {org['mission']}",
        "rows_key": "tasks",
        "columns": ["task_display", "count", "energy_display", "food_display",
                    "goods_display", "capacity_display"],
        "column_labels": {"task_display": "Task", "count": "Count",
                          "energy_display": "Energy", "food_display": "Food",
                          "goods_display": "Goods", "capacity_display": "Cargo"},
    }
    footer = scanner_footer(scanners)
    if footer:
        result["display"]["footer"] = footer
    return result

@mcp_tool(
    "Player-scoped fleet report (aka fleet status / my status): turn "
    "context, all organizations (in-transit marked, with per-org tasking "
    "breakdown), and fleet-wide aggregate assets (including capacity and "
    "percent_full).")
@player_tool
def show_civilization_status(sess) -> dict:
    """
    Return a player-scoped fleet/status report (aliases: "fleet status",
    "my status") -- the full roster (ships and colonies) plus fleet-wide
    aggregates in one call:
    - Turn context: current turn, turn limit, next_tick_countdown (the same
      value pre-formatted "MM:SS", or "--:--" when there is no clock running)
      and next_tick_at (ISO8601, from
      engine/clock.py -- None if the clock has never run or is paused; a
      caller also needs to check the server is actually live before trusting
      it, since a paused clock leaves a stale value -- see get_next_tick_at()
      and scripts/status.py for how the CLI report reconciles the two)
    - All player organizations (ships and colonies) with name, org_type, mission,
      sector location, a per-org cargo summary (current/capacity summed across
      that org's pods), a per-org storage breakdown (energy/food/goods
      currently held, summed across that org's pods regardless of each pod's
      own task -- storage is generic per pod, see RESOURCE_STORAGE_COLUMN),
      a tasking breakdown (pod count per task, e.g.
      {"produce_energy": 2, "produce_food": 2, "produce_goods": 2} -- this is
      the pod-deployment picture), and a production breakdown (see
      engine.production.org_production -- gross per-turn output at full input availability,
      not netted against consumption costs). Ships in
      transit are marked with in_transit=True, destination sector, expected
      arrival turn, and turns_remaining as raw fields -- arrival_turn is the
      turn the ship is actually free to act, not the turn whose end_of_turn()
      pass performs the landing (that happens one turn earlier; see
      engine/turn.py's arrival resolution). turns_remaining counts down to
      that same turn, so it only reaches 0 once the ship has actually landed
      and can take a new mission. The display `status` string itself is
      intentionally terse -- just "in transit", full stop, no destination or
      ETA; dest_sector/turns_remaining/arrival_turn are raw fields on the
      entry for a client that wants to build a richer status string itself.
      The sentinel sector (-1,-1,-1) an in-transit ship is parked at is never
      shown -- "in transit" is what a player reads instead, and the column
      heads as "Location".
    - Accumulated assets: aggregate energy, food, goods, and total across all
      pods, plus total capacity and percent_full.
    - display: presentation hints (see views/format.py
      module docstring) -- every org also carries ready-to-use short_name,
      status, cargo_display, tasking_summary, and production_summary fields
      alongside the raw ones, so a client with no LLM in the loop doesn't have
      to build its own formatting logic. `display.rows_key` names which
      top-level field holds the row list ("organizations", here) and
      `display.columns` lists which of each row's fields to render, in order
      -- see views/render.py's render_status() for a renderer driven entirely
      by these hints, with no per-tool special-casing. All raw fields are
      present alongside. `display.header` carries the turn line drawn above
      the table and `display.footer` the fleet totals drawn below it: an
      aggregate is a different question than a per-unit row, so it does not
      go in the table.
    Ownership-gated — only the calling player's data.
    """
    cur = sess.cur

    # Turn context
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()["current_turn"]
    next_tick_at = get_next_tick_at()

    # Organizations
    cur.execute("""
        SELECT o.id, o.name, o.org_type, o.mission, o.mission_params,
               o.sector_id, s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.player_id = ?
        ORDER BY o.org_type, o.name""", (sess.player_id,))
    orgs_raw = cur.fetchall()
    orgs = []
    for o in orgs_raw:
        cargo = cur.execute("""SELECT SUM(energy_stored+food_stored+goods_stored) AS current,
            SUM(storage_capacity) AS capacity,
            SUM(energy_stored) AS energy, SUM(food_stored) AS food, SUM(goods_stored) AS goods
            FROM pods WHERE org_id=?""",
            (o["id"],)).fetchone()
        storage = {"energy": cargo["energy"] or 0.0, "food": cargo["food"] or 0.0,
                   "goods": cargo["goods"] or 0.0}
        tasks = cur.execute("SELECT task, COUNT(*) AS n FROM pods WHERE org_id=? GROUP BY task",
                           (o["id"],)).fetchall()
        tasking = {t["task"]: t["n"] for t in tasks}
        in_transit = o["sector_id"] == -1
        production = org_production(tasking, in_transit, o["org_type"])
        entry = {
            "id": o["id"],
            "name": o["name"],
            "short_name": short_name(o["name"]),
            "org_type": o["org_type"],
            "mission": o["mission"],
            "cargo": {"current": cargo["current"] or 0.0, "capacity": cargo["capacity"] or 0.0},
            "cargo_display": f"{cargo['current'] or 0.0:.0f}/{cargo['capacity'] or 0.0:.0f}",
            "storage": storage,
            "storage_summary": resource_summary(storage),
            "tasking": tasking,
            "tasking_summary": tasking_summary(tasking),
            "production": production,
            "production_summary": resource_summary(production),
        }
        if o["sector_id"] == -1:
            # In transit — fetch destination and arrival turn from arrival_queue.
            # No join to sectors: the destination may not exist as a row yet
            # (sectors are lazily instantiated, see db/sectors.py) until arrival.
            cur.execute("""
                SELECT dest_x, dest_y, dest_z, arrival_turn
                FROM arrival_queue WHERE org_id = ?""", (o["id"],))
            aq = cur.fetchone()
            entry["in_transit"] = True
            if aq:
                entry["dest_sector"] = {
                    "coords": [aq["dest_x"], aq["dest_y"], aq["dest_z"]]
                }
                entry["arrival_turn"] = aq["arrival_turn"]
                # arrival_turn is the turn the ship is actually free to act
                # -- landing itself happens one turn earlier, during the
                # end_of_turn() pass for arrival_turn-1 (see engine/turn.py).
                # turns_remaining counts down to arrival_turn directly, so it
                # reaches 0 exactly when the ship can take a new mission, not
                # one turn before. It stays a raw field: the display string
                # says only "in transit".
                entry["turns_remaining"] = max(0, aq["arrival_turn"] - current_turn)
                entry["status"] = "in transit"
            else:
                entry["status"] = "in transit"
        else:
            entry["in_transit"] = False
            entry["sector"] = {
                "id": o["sector_id"],
                "coords": [o["coord_x"], o["coord_y"], o["coord_z"]]
            }
            entry["status"] = f"at ({o['coord_x']},{o['coord_y']},{o['coord_z']})"
        orgs.append(entry)

    # Accumulated assets — aggregate across all pods for this player
    cur.execute("""
        SELECT
            SUM(p.energy_stored) AS energy,
            SUM(p.food_stored) AS food,
            SUM(p.goods_stored) AS goods,
            SUM(p.energy_stored+p.food_stored+p.goods_stored) AS total,
            SUM(p.storage_capacity) AS capacity
        FROM pods p
        JOIN organizations o ON o.id = p.org_id
        WHERE o.player_id = ?""", (sess.player_id,))
    assets_row = cur.fetchone()
    total = assets_row["total"] or 0
    capacity = assets_row["capacity"] or 0
    assets = {
        "energy":       round(assets_row["energy"] or 0, 2),
        "food":         round(assets_row["food"]   or 0, 2),
        "goods":        round(assets_row["goods"]  or 0, 2),
        "total":        round(total, 2),
        "capacity":     round(capacity, 2),
        "percent_full": round(total / capacity * 100, 1) if capacity else 0.0,
    }

    return {
        "turn": current_turn,
        "turn_limit": TURN_LIMIT,
        "next_tick_at": next_tick_at,
        "next_tick_countdown": tick_countdown(next_tick_at),
        "organizations": orgs,
        "assets": assets,
        "display": {
            "header": turn_header(current_turn, TURN_LIMIT, next_tick_at),
            "rows_key": "organizations",
            "columns": ["short_name", "status", "cargo_display", "storage_summary",
                        "tasking_summary", "production_summary"],
            # Storage, tasking and production each hold a slashed run of
            # numbers, so what each slot counts lives in the header once
            # instead of on every cell -- see views/format.slashed and
            # stacked_header. Tasking runs over TASK_ABBREV, not the three
            # resources: a scanning or idle pod has to have a slot of its own
            # or the cell reads as a complete crew count while omitting pods.
            "column_labels": {
                "short_name": "Unit", "status": "Location", "cargo_display": "Cargo",
                "storage_summary": stacked_header("Storage"),
                "tasking_summary": stacked_header("Tasking", TASK_ABBREV),
                "production_summary": stacked_header("Production/Turn"),
            },
            "footer": totals_footer("Fleet totals", assets),
        },
    }

@mcp_tool(
    "Public scoreboard: turn context plus every player's aggregate resource "
    "totals (energy/food/goods/total/percent_full), ranked highest-first. "
    "Does not reveal other players' fleet composition or position -- only "
    "aggregate totals are public.")
@player_tool
def show_game_status(sess) -> dict:
    """
    Return the public scoreboard -- turn context (including next_tick_at,
    see show_civilization_status's docstring for the caveat about a paused
    clock, and next_tick_countdown -- the same value pre-formatted "MM:SS",
    or "--:--" when next_tick_at is None) plus every player's aggregate
    resource totals, side by side.
    Unlike show_civilization_status (or every other tool in this module),
    this is NOT ownership-gated to the caller's own data: aggregate totals
    are treated as public standing, the same information
    _calculate_final_scores reveals at game-over, available on demand
    throughout the game. player_token is only used to confirm the caller is a
    real player in this game -- it does not filter or restrict what's
    returned. Detailed fleet composition, position, and tasking of other
    players is NOT included here and stays private -- only aggregate resource
    totals are public.
    `score` is the actual game score, not just another resource total:
    energy/food/goods currently held are weighted by `config/game_config.yaml`'s
    `score_weights` (energy is a means of production, not a scored asset;
    goods score double food) and summed. Same formula `engine/turn.py`'s
    `_calculate_final_scores()` uses to decide the winner at game-over, so
    the standing shown here is checkable against the eventual result.
    Standings are ranked by `score` (highest first, "rank" field included),
    NOT by the raw `total` (an unweighted sum, included for
    capacity/fullness context). Ranking is standard competition ranking, so
    players level on score share a rank -- and `winners` is a list for the
    same reason: nothing breaks a tie, so everyone on rank 1 has won. It is
    empty until the game ends.
    Carries a display block (rows_key="standings", suggested column order) for clients that want a ready-to-use presentation instead
    of building one -- see views/render.py's render_status().
    """
    cur = sess.cur

    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()["current_turn"]
    next_tick_at = get_next_tick_at()
    weights = load_config().game.score_weights

    # Ranking and scoring both come from engine/scoring.py -- the same call
    # _calculate_final_scores makes, so the standing shown here and the winner
    # declared at game over cannot disagree. Rounding and utilization are
    # added on top: those are presentation, and the scoring module
    # deliberately returns raw figures (see its player_standings docstring).
    standings = player_standings(cur, weights)
    for s in standings:
        capacity = s["capacity"]
        s["utilization"] = round(s["total"] / capacity * 100, 1) if capacity else 0.0
        for field in ("energy", "food", "goods", "total", "capacity", "score"):
            s[field] = round(s[field], 2)

    # Once the game is over this becomes the end-of-game scoreboard: the
    # RECORDED result, not a recomputation. get_final_scores() reads the
    # game.final_scores event written at the whistle, so what a player is
    # shown afterwards is what actually happened, and stays right even if
    # anything touches state later.
    final = get_final_scores()
    game_over = final is not None
    if game_over:
        standings = final["standings"]
        for s in standings:
            s.setdefault("utilization",
                         round(s["total"] / s["capacity"] * 100, 1) if s.get("capacity") else 0.0)
    next_tick_countdown = tick_countdown(next_tick_at)
    winners = []
    if game_over:
        # A game recorded before winners became a list carries a single
        # `winner` string instead; read either shape rather than fail on an
        # old scoreboard.
        winners = final.get("winners") or [n for n in [final.get("winner")] if n]
    header = (f"FINAL — game over at turn {final['final_turn']} of {final['turn_limit']}. "
              f"{winners_label(winners)}" if game_over
              else turn_header(current_turn, TURN_LIMIT, next_tick_at))

    # Whole-number display variants -- score/energy/food/goods never carry a
    # meaningful fraction (production and upkeep are integer per-turn amounts),
    # so the raw rounded-to-2dp float is noise in a table meant for a phone
    # screen. The raw floats above are untouched for a client that wants to
    # compute with them.
    for s in standings:
        s["score_display"] = f"{s['score']:.0f}"
        s["energy_display"] = f"{s['energy']:.0f}"
        s["food_display"] = f"{s['food']:.0f}"
        s["goods_display"] = f"{s['goods']:.0f}"

    return {
        "turn": current_turn,
        "turn_limit": TURN_LIMIT,
        "next_tick_at": next_tick_at,
        "next_tick_countdown": next_tick_countdown,
        "game_over": game_over,
        "is_final": game_over,
        "winners": winners,
        "score_weights": final["score_weights"] if game_over else dict(weights),
        "standings": standings,
        "display": {
            "header": header,
            "rows_key": "standings",
            # utilization deliberately excluded from this report -- still a
            # raw field on every standings row, just not part of the table.
            "columns": ["rank", "display_name", "score_display", "energy_display",
                        "food_display", "goods_display"],
            "column_labels": {"rank": "Rank", "display_name": "Player",
                               "score_display": "Score", "energy_display": "Energy",
                               "food_display": "Food", "goods_display": "Goods"},
        },
    }
