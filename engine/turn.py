import collections, os, json, math
from config.loader import load_config
from db.connection import get_connection, read_one, read_value
from db.sectors import reveal_sector, CONFIDENCE_DECAY_PER_TURN
from db.sightings import record_sightings
from db.events import record_event_direct
from db.orgs import org_position
from engine.ship_log import dispatch_due_commands
from engine.production import (get_production, get_consumption_recipe,
                               get_production_multiplier, ORG_UPKEEP_COST,
                               RESOURCE_CAPACITY_COLUMN)
from engine.org_resources import (available_org_resource, drain_org_resource,
                                  store_org_resource)
from engine.transfers import resolve_due_transfers
from engine.scoring import score_for, player_standings
from engine.bearings import get_scan_range

TURN_LIMIT = int(os.getenv("TURN_LIMIT", 20))

def get_current_turn() -> int:
    return read_value("SELECT current_turn FROM game_state WHERE id=1")

def is_game_over() -> bool:
    return get_current_turn() >= TURN_LIMIT

def get_next_tick_at() -> str | None:
    """
    ISO8601 timestamp of when the clock will next tick, as last written by
    engine/clock.py's run_clock() -- None if the clock has never run (no
    scenario selected yet) or is currently paused (server process stopped,
    so nothing is refreshing this value; a caller comparing it against the
    current time would see it drift into the past). Whether the clock is
    actually running right now is a process-liveness question this function
    can't answer on its own -- see scripts/status.py for how a caller
    reconciles this timestamp with a live health check.
    """
    return read_value("SELECT next_tick_at FROM game_state WHERE id=1")

def _player_holdings(cur) -> dict:
    """
    Per-player {"energy":..,"food":..,"goods":..,"total":..}, summed across
    all of that player's pods. Used both for the printed holdings line and
    as the before/after reference points end_of_turn() uses to derive this
    turn's resource waste for the turn.snapshot ledger (see _snapshot_holdings).
    """
    rows = cur.execute("""
        SELECT o.player_id,
               SUM(p.energy_stored) AS energy,
               SUM(p.food_stored) AS food,
               SUM(p.goods_stored) AS goods,
               SUM(p.energy_stored+p.food_stored+p.goods_stored) AS total
        FROM pods p JOIN organizations o ON o.id = p.org_id
        GROUP BY o.player_id
    """).fetchall()
    return {r["player_id"]: {"energy": r["energy"] or 0.0, "food": r["food"] or 0.0,
                             "goods": r["goods"] or 0.0, "total": r["total"] or 0.0}
            for r in rows}

def _affordable_ratio(cur, org_id: int, recipe: dict, cap: float = 1.0) -> float:
    """
    What fraction of `recipe` this org can actually pay for right now: the
    smallest available/required across every resource the recipe wants,
    starting from `cap` and only ever narrowing.

    Returned unclamped and uncharged -- callers that need to fold in a
    further constraint (the production pass caps by what the sector has
    left) do so before clamping, since min() composes in any order.
    """
    for resource, required in recipe.items():
        if required <= 0:
            continue
        ratio = min(cap, available_org_resource(cur, org_id, resource) / required)
        cap = ratio
    return cap

def _charge(cur, org_id: int, player_id: int, recipe: dict, ratio: float,
            consumption: dict):
    """
    Draw `recipe` scaled by `ratio` from the org's pooled stock, and tally
    what was drawn into the per-player consumption accumulator that feeds
    the turn.snapshot ledger's derived waste figure (see _snapshot_holdings).
    """
    for resource, required in recipe.items():
        amount = required * ratio
        drain_org_resource(cur, org_id, resource, amount)
        consumption[player_id][resource] += amount

def _pay_for(cur, org_id: int, player_id: int, recipe: dict, consumption: dict,
             cap: float = 1.0) -> float:
    """
    The economy's one rule for paying to do something, and the fraction of it
    you get to do: work out what the org can afford, clamp to [0,1], charge
    that much, and hand back the ratio so the caller can scale its output by
    the same figure.

    Graceful degradation rather than an all-or-nothing gate is the whole
    point -- half the required input on hand buys half the output. Four
    callers rely on it (org upkeep, pod production, pod scan, innate org
    scan), so how proration works is decided in exactly one place.

    A `cap` below 1.0 lets a caller impose a limit the org's own stock knows
    nothing about -- the production pass passes the sector's remaining energy
    that way (see _sector_cap).
    """
    ratio = max(0.0, min(1.0, _affordable_ratio(cur, org_id, recipe, cap)))
    if ratio > 0:
        _charge(cur, org_id, player_id, recipe, ratio, consumption)
    return ratio

def _sector_cap(cur, sector_id: int, base_production: dict) -> float:
    """
    How much of a full-rate harvest the org's current sector can actually
    supply, as a fraction -- energy is the only sector-sourced resource (see
    RESOURCE_CAPACITY_COLUMN), so in practice this is the energy pool alone.

    A ship in transit sits at the sentinel sector (id -1, permanently 0
    capacity), so this returns 0 for it and drives energy production to zero
    while travelling with no in-transit branch anywhere.
    """
    cap = 1.0
    for resource, base_amount in base_production.items():
        if resource not in RESOURCE_CAPACITY_COLUMN or base_amount <= 0:
            continue
        col = RESOURCE_CAPACITY_COLUMN[resource]
        sector = cur.execute(f"SELECT {col} AS remaining FROM sectors WHERE id=?",
                             (sector_id,)).fetchone()
        available = sector["remaining"] if sector else 0.0
        cap = min(cap, available / base_amount)
    return cap

def _resolve_scan(cur, current_turn: int, org_id: int, player_id: int, origin,
                  offset, subject_id: int, subject_type: str, payload_extra: dict):
    """
    Resolve one aimed scanner: reveal the sector it points at, or log an
    out-of-range alert if the aim overreaches.

    `origin` is the scanning org's absolute (x,y,z); `offset` is the relative
    aim (see bearings.SCAN_BEARINGS -- aim is relative so it survives a
    move). Range is re-checked here even though it was validated at set time
    and cannot drift, because get_scan_range() will eventually vary per org.
    """
    dx, dy, dz = offset
    tx, ty, tz = origin[0] + dx, origin[1] + dy, origin[2] + dz
    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
    scan_range = get_scan_range(org_id)
    if distance <= scan_range:
        target_sector_id = reveal_sector(cur, player_id, tx, ty, tz)
        # Order matters and is load-bearing: the sector is revealed first and
        # unconditionally, so a scan NEVER misses what the ground holds. Only
        # the organizations standing on it roll to be noticed (see
        # db/sightings.py). Resources are terrain and terrain does not hide;
        # a fleet does.
        seen = record_sightings(cur, player_id, target_sector_id, current_turn)
        if seen:
            record_event_direct(cur, current_turn, "scan.contact",
                actor_id=player_id, subject_id=subject_id, subject_type=subject_type,
                payload={**payload_extra, "org_id": org_id,
                         "target_x": tx, "target_y": ty, "target_z": tz,
                         "detected": seen})
        # TODO: emit pod.scanned/org.scanned event
        return
    record_event_direct(cur, current_turn, "alert.scan_out_of_range",
        actor_id=player_id, subject_id=subject_id, subject_type=subject_type,
        payload={**payload_extra, "org_id": org_id,
                 "target_x": tx, "target_y": ty, "target_z": tz,
                 "distance": distance, "range": scan_range})

def _apply_org_upkeep(cur, consumption: dict):
    """
    Per-organization upkeep, once per turn (not per pod) -- every ship/colony
    costs ORG_UPKEEP_COST to keep running at all, on top of its pods. Applies
    regardless of transit state, and is not discounted or surcharged by org
    type: the colony advantage is purely on the output side.

    Prorated like pod recipes -- not enough on hand draws partially rather
    than not at all -- and runs before the per-pod production pass, so upkeep
    gets first claim on an org's stock for the turn.

    `consumption` is a {player_id: {resource: amount}} accumulator that
    upkeep drains tally into alongside pod recipe costs, so the ledger's
    derived waste figure accounts for every sink in the turn.
    """
    for org in cur.execute("SELECT id, player_id FROM organizations").fetchall():
        _pay_for(cur, org["id"], org["player_id"], ORG_UPKEEP_COST, consumption)

def end_of_turn():
    if is_game_over():
        print("Turn limit reached — game over."); return
    if not read_value("SELECT COUNT(*) FROM games WHERE id=1"):
        # No scenario selected yet (xsettlers_mcp/game_select.py's select_scenario
        # populates this on bootstrap). Nothing to process -- don't burn turns
        # on an empty game.
        return

    # 0. NPC decisions, before this turn resolves. Each opens and commits its
    #    own connection, so nothing is left open when the turn's own conn/cur
    #    starts. Function-level import, and the one place anything below the
    #    tool layer reaches up through it -- at module level it would close
    #    the loop engine -> npc -> tools -> engine during this module's own
    #    top-level imports.
    from npc.strategies import run_npc_decisions
    run_npc_decisions()

    conn = get_connection(); cur = conn.cursor()

    # 1. Reset declarations
    cur.execute("UPDATE players SET end_turn_declared=0")

    # 2. Resolve arrivals
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()[0]
    # arrival_turn names the turn the ship becomes free to act (see
    # navigation_tools.confirm_move), one turn after the landing pass itself
    # -- so this end_of_turn() call (which is about to advance current_turn
    # to current_turn+1) is the one that must perform the landing.
    cur.execute("SELECT org_id,dest_x,dest_y,dest_z FROM arrival_queue WHERE arrival_turn<=?",
                (current_turn + 1,))
    for arrival in cur.fetchall():
        org = cur.execute("SELECT player_id FROM organizations WHERE id=?",
                          (arrival["org_id"],)).fetchone()
        if org:
            dest_sector_id = reveal_sector(cur, org["player_id"],
                arrival["dest_x"], arrival["dest_y"], arrival["dest_z"])
            cur.execute("""UPDATE organizations SET sector_id=?,mission='idle',mission_params=NULL,
                           is_mobile=1 WHERE id=?""", (dest_sector_id,arrival["org_id"]))
    cur.execute("DELETE FROM arrival_queue WHERE arrival_turn<=?", (current_turn + 1,))

    # 2.3. Resolve resource transfers ordered last tick -- AFTER arrivals, so a
    #      ship that only lands at the receiver's sector this turn still
    #      completes a transfer ordered while it was inbound, and so a queued
    #      upon_arrival transfer has somewhere to resolve against. Co-location
    #      is judged on where each org sits once this turn's arrivals have
    #      settled; a departing org is parked at the -1 sentinel by then and
    #      correctly fails the check (see engine/transfers.py).
    resolve_due_transfers(cur, current_turn)

    # 2.5. Ship's log: dispatch any due upon_arrival/at_turn queued
    #      commands (see engine/ship_log.py). Must run after the arrival loop
    #      above (so a upon_arrival chained move sees this org's just-landed
    #      sector as its departure point) and before step 3's production pass
    #      (so if that chained move re-parks the org at the sentinel sector,
    #      this turn's production correctly shows zero energy for it -- same
    #      in-transit-suppresses-energy rule already in effect below, no
    #      special-casing needed here).
    dispatch_due_commands(cur, current_turn)

    # 3. Org upkeep, then pod production (input-costed, see engine/production.py's
    #    POD_CONSUMPTION_RECIPE), then scan resolution.
    #    Produce tasks run for all pods regardless of transit state, but
    #    produce_energy specifically can't harvest anything while in transit
    #    (see below). Scanning in transit pays its cost and reveals nothing. before_holdings/production/consumption feed the
    #    turn.snapshot ledger (see _snapshot_holdings, step 6).
    before_holdings = _player_holdings(cur)
    production = collections.defaultdict(lambda: collections.defaultdict(float))
    consumption = collections.defaultdict(lambda: collections.defaultdict(float))
    _apply_org_upkeep(cur, consumption)

    cur.execute("""SELECT p.id,p.task,p.task_params,p.org_id,
                o.sector_id AS org_sector_id, o.player_id, o.org_type
        FROM pods p JOIN organizations o ON o.id = p.org_id
        ORDER BY p.id""")
    for pod in cur.fetchall():
        task = pod["task"]
        player_id = pod["player_id"]

        # 3a/b. Production, gated by input cost.
        #    Each producing task (plus scan) costs other resources to run,
        #    drawn from the org's pooled stock; idle costs nothing. Output is
        #    prorated to the fraction of required input actually available,
        #    not gated all-or-nothing. produce_energy is additionally capped
        #    by the sector's remaining pool -- a ship in transit sits at the
        #    sentinel sector (permanently 0 capacity), so that alone drives
        #    energy production to 0 while travelling, with no special case.
        #    Known gap: pods of different players sharing a sector are served
        #    by pod id, so whoever processes first gets first claim.
        if task in ("produce_energy", "produce_food", "produce_goods"):
            # Colony bonus (see COLONY_PRODUCTION_MULTIPLIER): applied to the
            # output side only -- `recipe` below is left at the base rate, so
            # a colony pays a ship's costs and gets 1.5x back. Folded into the
            # amounts here rather than at the point of storage so that every
            # downstream consequence follows automatically: the sector cap is
            # measured against what will actually be taken, and the sector is
            # drained by that same larger figure.
            multiplier = get_production_multiplier(pod["org_type"])
            base_production = {resource: amount * multiplier
                               for resource, amount in get_production(task).items()}
            recipe = get_consumption_recipe(task)
            org_id = pod["org_id"]

            # Two independent limits on how much of a turn's work actually
            # happens: what the org can pay for, and what the sector has left
            # to give. _pay_for takes the smaller of the two, charges the
            # inputs at that rate, and returns it to scale the output by.
            ratio = _pay_for(cur, org_id, player_id, recipe, consumption,
                             cap=_sector_cap(cur, pod["org_sector_id"], base_production))

            if ratio > 0:
                for resource, base_amount in base_production.items():
                    amount = base_amount * ratio
                    if resource in RESOURCE_CAPACITY_COLUMN and amount > 0:
                        col = RESOURCE_CAPACITY_COLUMN[resource]
                        cur.execute(f"UPDATE sectors SET {col}=MAX(0,{col}-?) WHERE id=?",
                                    (amount, pod["org_sector_id"]))
                    # Fills this pod's own free space first, then spills into
                    # other org pods with room, then is lost if the whole org
                    # is full (see store_org_resource) -- production tallied
                    # here regardless of whether it actually fit; the gap
                    # between this and what _player_holdings later shows
                    # stored is exactly the turn.snapshot ledger's waste figure.
                    store_org_resource(cur, org_id, pod["id"], resource, amount)
                    if amount > 0:
                        production[player_id][resource] += amount

        # 3c. Scan: costs food (see POD_CONSUMPTION_RECIPE) but produces no
        #     output. Transit suppresses the *reveal*, not the cost -- a
        #     scanner left deployed on a voyage burns its upkeep and returns
        #     nothing. That waste is the intended cost of not standing the
        #     pod down before departure.
        elif task == "scan":
            org_id = pod["org_id"]
            ratio = _pay_for(cur, org_id, player_id, get_consumption_recipe(task),
                             consumption)

            # org_position returns None for an in-transit org (parked at the
            # sentinel sector), which is what suppresses the reveal. _pay_for
            # has already run, so the food is gone either way.
            org = org_position(cur, org_id)
            if ratio > 0 and org:
                params = json.loads(pod["task_params"] or "{}")
                offset = (params.get("offset_x"), params.get("offset_y"),
                          params.get("offset_z"))
                if all(c is not None for c in offset):
                    _resolve_scan(cur, current_turn, org_id, org["player_id"],
                        origin=(org["coord_x"], org["coord_y"], org["coord_z"]),
                        offset=offset, subject_id=pod["id"], subject_type="pod",
                        payload_extra={"pod_id": pod["id"]})

    # 3d. Innate organization scan -- one sector per turn per org, no pod
    #     required. An org that also has scan pods gets both, each paying its
    #     own way. Runs after pod scans so both resolve against the same
    #     pre-existing state. The aim persists across turns and re-scanning is
    #     idempotent, so holding a target keeps a sector from blinking out.
    scan_recipe = get_consumption_recipe("scan")
    cur.execute("""SELECT o.id, o.player_id, o.sector_id,
                          o.scan_offset_x AS dx, o.scan_offset_y AS dy, o.scan_offset_z AS dz,
                          s.coord_x, s.coord_y, s.coord_z
        FROM organizations o LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.scan_offset_x IS NOT NULL
          AND o.scan_offset_y IS NOT NULL
          AND o.scan_offset_z IS NOT NULL""")
    for org in cur.fetchall():
        ratio = _pay_for(cur, org["id"], org["player_id"], scan_recipe, consumption)
        if ratio <= 0 or org["sector_id"] == -1:
            continue          # starved, or in transit: cost paid, no reveal
                              # (deliberate -- see 3c)
        _resolve_scan(cur, current_turn, org["id"], org["player_id"],
            origin=(org["coord_x"], org["coord_y"], org["coord_z"]),
            offset=(org["dx"], org["dy"], org["dz"]),
            subject_id=org["id"], subject_type="organization", payload_extra={})

    # 4. Colonization resolution — only orgs whose 3-turn colonize_complete event has matured.
    #    Scheduled by set_mission() via record_event(resolve_at_turn=...). Idempotent via the
    #    org_type='ship' filter: once resolved, org_type flips to 'colony' and this stops
    #    matching, so no separate "resolved" flag is needed.
    #    resolve_at_turn was scheduled against get_current_turn() (the turn-in-progress at
    #    scheduling time); current_turn here is that same in-progress value read at the top
    #    of *this* call, one turn behind what it'll be once this call's increment (step 7)
    #    lands. Compare against current_turn+1 so "3 turns" means 3 completed end_of_turn()
    #    passes, not 4.
    cur.execute("""
        SELECT o.id,o.org_type,o.player_id,o.sector_id,o.mission,o.mission_params
        FROM events e
        JOIN organizations o ON o.id = e.subject_id AND e.subject_type = 'organization'
        WHERE e.event_type = 'colonize_complete' AND e.resolve_at_turn <= ?
          AND o.org_type = 'ship' AND o.mission = 'colonize'
    """, (current_turn + 1,))
    for org in cur.fetchall():
        _handle_colonize(cur, org, current_turn)

    # 5. Mission dispatch (defend/attack — stationary orgs only; stubs for now)
    cur.execute("""SELECT id,org_type,player_id,sector_id,mission,mission_params
        FROM organizations WHERE mission IN ('defend','attack') AND sector_id!=-1""")
    for org in cur.fetchall():
        params = json.loads(org["mission_params"] or "{}")
        {"defend": _handle_defend, "attack": _handle_attack}.get(org["mission"], lambda *a: None)(cur, org, params)

    # 5. Fog decay — a flat subtraction, floored at 0. A sector that reaches 0
    #    blinks out: the row is kept (it's the player's history of having been
    #    there) but every read path filters on confidence > 0, so it leaves the
    #    map entirely rather than lingering as a stale ghost.
    cur.execute("""UPDATE player_sectors SET confidence = MAX(0, confidence - ?)
        WHERE confidence > 0 AND NOT EXISTS (
            SELECT 1 FROM organizations o
            WHERE o.sector_id = player_sectors.sector_id
              AND o.player_id = player_sectors.player_id
        )""", (CONFIDENCE_DECAY_PER_TURN,))

    # 6. Holdings snapshot — calculated AFTER all processing is complete
    #    This is the canonical end-state: production, arrivals, missions, fog all resolved.
    conn.commit()  # flush all mutations before snapshotting
    _snapshot_holdings(cur, current_turn, before_holdings, production, consumption)

    # 7. Increment turn
    cur.execute("UPDATE game_state SET current_turn=current_turn+1 WHERE id=1")
    conn.commit(); conn.close()
    print(f"End of turn {current_turn} complete.")

    # 8. Check for game over
    if is_game_over():
        print(f"Turn limit {TURN_LIMIT} reached — game over. Calculating final scores...")
        _calculate_final_scores()

def _snapshot_holdings(cur, turn: int, before_holdings: dict, production: dict, consumption: dict):
    """
    Record per-player resource totals at the end of the turn, after all pod
    execution, arrivals, and mission dispatch have resolved. Called as step 6
    of end_of_turn() -- never before processing is complete.

    Waste is derived, not directly measured -- for each resource:
        wasted = produced - consumed - (after - before)
    i.e. whatever was produced and consumed this turn but doesn't show up in
    the actual before/after delta was lost to a full pod with nowhere to
    spill (see store_org_resource). `production`/`consumption` are
    {player_id: {resource: amount}} accumulators built inline during this
    same end_of_turn() pass (_apply_org_upkeep and the pod loop above) --
    this function costs exactly one extra query (the after-holdings read,
    via _player_holdings) beyond what it already did; before_holdings was
    likewise one query taken at the top of step 3, before any mutation.
    """
    after_holdings = _player_holdings(cur)
    weights = load_config().game.score_weights
    empty = {"energy": 0.0, "food": 0.0, "goods": 0.0, "total": 0.0}
    for player in cur.execute("SELECT id, display_name FROM players").fetchall():
        pid = player["id"]
        after = after_holdings.get(pid, empty)
        before = before_holdings.get(pid, empty)
        produced, consumed = production.get(pid, {}), consumption.get(pid, {})
        print(f"  Holdings (turn {turn}) — player {pid}: "
              f"energy={after['energy']:.1f} food={after['food']:.1f} "
              f"goods={after['goods']:.1f} total={after['total']:.1f}")
        wasted = {r: max(0.0, produced.get(r, 0.0) - consumed.get(r, 0.0)
                         - (after[r] - before[r])) for r in ("energy", "food", "goods")}
        score = score_for(after, weights)
        record_event_direct(cur, turn, "turn.snapshot", actor_id=pid, subject_id=pid,
            subject_type="player", payload={
                "player_id": pid, "display_name": player["display_name"],
                "energy": after["energy"], "food": after["food"], "goods": after["goods"],
                "total": after["total"], "score": round(score, 2),
                "energy_wasted": round(wasted["energy"], 2),
                "food_wasted": round(wasted["food"], 2),
                "goods_wasted": round(wasted["goods"], 2),
            })

FINAL_SCORES_EVENT = "game.final_scores"

def _calculate_final_scores() -> list:
    """
    Weighted game score per player: config/game_config.yaml's score_weights
    applied to each player's total stored energy/food/goods, highest wins.
    `winners` is a list because nothing breaks a tie: players level on score
    share rank 1 and all of them win.

    Both the ranking and the formula come from engine/scoring.py, which is
    also what show_game_status calls, so the winner here matches what was
    checkable mid-game via that tool by construction rather than by two
    copies of an expression happening to stay in step.

    Idempotent: writing twice would give a game two endings, so an existing
    event for this game is left alone and returned as-is.
    """
    conn = get_connection(); cur = conn.cursor()
    existing = cur.execute(
        "SELECT payload FROM events WHERE event_type=? ORDER BY id LIMIT 1",
        (FINAL_SCORES_EVENT,)).fetchone()
    if existing:
        conn.close()
        return json.loads(existing["payload"])["standings"]

    weights = load_config().game.score_weights
    standings = player_standings(cur, weights)

    final_turn = cur.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()["current_turn"]
    record_event_direct(cur, final_turn, FINAL_SCORES_EVENT,
                         payload={"final_turn": final_turn, "turn_limit": TURN_LIMIT,
                                  "score_weights": dict(weights),
                                  # Every player on the top rank, not just the
                                  # first row: scoring has no tiebreak, so a tie
                                  # is a shared win rather than something the
                                  # sort order gets to decide.
                                  "winners": [s["display_name"] for s in standings
                                              if s["rank"] == 1],
                                  "standings": standings})
    conn.commit(); conn.close()
    for s in standings:
        print(f"  {s['rank']}. {s['display_name']}: {s['score']:.1f} pts")
    return standings


def get_final_scores() -> dict:
    """
    The recorded end-of-game scoreboard, or None if the game hasn't ended.
    Reads the persisted `game.final_scores` event rather than recomputing, so
    what a player is shown afterwards is what actually happened at the whistle.
    """
    row = read_one("SELECT payload FROM events WHERE event_type=? ORDER BY id LIMIT 1",
                   (FINAL_SCORES_EVENT,))
    return json.loads(row["payload"]) if row else None

def _handle_colonize(cur, org, current_turn):
    """Resolve a matured colonize_complete event: flip org_type to 'colony' and log ship.colonized.

    Clears task_force_id in the same statement -- a task force cannot hold
    anything but a ship, so a member that colonizes leaves the moment
    org_type flips (see docs/dev_history.md's task-forces entry)."""
    if org["org_type"] != "ship":
        return
    record_event_direct(cur, current_turn, "ship.colonized", subject_id=org["id"],
        subject_type="organization", payload={"org_id": org["id"], "sector_id": org["sector_id"]})
    cur.execute("""UPDATE organizations
        SET org_type='colony',is_mobile=0,mission='idle',mission_params=NULL,task_force_id=NULL
        WHERE id=?""", (org["id"],))

def _handle_defend(cur, org, params): pass   # stub
def _handle_attack(cur, org, params): pass   # stub

def check_consensus_acceleration():
    undeclared = read_value("SELECT COUNT(*) FROM players WHERE end_turn_declared=0")
    if undeclared == 0:
        print("All players declared end of turn — accelerating clock.")
        end_of_turn(); return True
    return False
