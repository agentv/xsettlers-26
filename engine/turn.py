import collections, os, json, math
from config.loader import load_config
from db.connection import get_connection
from db.sectors import reveal_sector, CONFIDENCE_DECAY_PER_TURN
from engine.production import (get_production, get_consumption_recipe,
                               get_production_multiplier, ORG_UPKEEP_COST,
                               RESOURCE_CAPACITY_COLUMN, RESOURCE_STORAGE_COLUMN)
from xsettlers_mcp.tools.sector_tools import get_scan_range

TURN_LIMIT = int(os.getenv("TURN_LIMIT", 20))

def get_current_turn() -> int:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    turn = cur.fetchone()[0]; conn.close(); return turn

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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT next_tick_at FROM game_state WHERE id=1")
    row = cur.fetchone(); conn.close()
    return row["next_tick_at"] if row else None

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

def _available_org_resource(cur, org_id: int, resource: str) -> float:
    """
    An org's pooled stock of a resource: summed across ALL of that org's
    pods' <resource>_stored column, regardless of each pod's current task
    -- storage is generic per pod, so retasking a pod never hides whatever
    it already has stored (see RESOURCE_STORAGE_COLUMN).
    """
    col = RESOURCE_STORAGE_COLUMN[resource]
    return cur.execute(
        f"SELECT COALESCE(SUM({col}),0) AS total FROM pods WHERE org_id=?",
        (org_id,)).fetchone()["total"]

def _drain_org_resource(cur, org_id: int, resource: str, amount: float):
    """Drain amount of a resource from an org's pooled stock, sequentially
    (by pod id) across whichever of its pods currently hold that resource --
    regardless of their current task."""
    if amount <= 0:
        return
    col = RESOURCE_STORAGE_COLUMN[resource]
    remaining = amount
    source_pods = cur.execute(
        f"SELECT id, {col} AS have FROM pods WHERE org_id=? AND {col}>0 ORDER BY id",
        (org_id,)).fetchall()
    for sp in source_pods:
        if remaining <= 0:
            break
        draw = min(sp["have"], remaining)
        if draw > 0:
            cur.execute(f"UPDATE pods SET {col}={col}-? WHERE id=?", (draw, sp["id"]))
            remaining -= draw

def _store_org_resource(cur, org_id: int, producing_pod_id: int, resource: str, amount: float):
    """
    Add amount of a resource to storage: fills the producing pod's own free
    space first, then spills into other pods in the same org that still
    have free space (by pod id), then is lost if no pod in the org has room
    left. Free space on a pod = storage_capacity minus everything currently
    stored there (energy+food+goods combined) -- storage is one shared
    container per pod, not resource-specific, so a pod already full of one
    resource has no room for another regardless of type.
    """
    if amount <= 0:
        return
    col = RESOURCE_STORAGE_COLUMN[resource]
    remaining = amount
    # Producing pod first (id != producing_pod_id sorts to 0/False first), then by id.
    pods = cur.execute(
        """SELECT id, storage_capacity, energy_stored, food_stored, goods_stored
           FROM pods WHERE org_id=? ORDER BY (id != ?), id""",
        (org_id, producing_pod_id)).fetchall()
    for p in pods:
        if remaining <= 0:
            break
        free = p["storage_capacity"] - (p["energy_stored"] + p["food_stored"] + p["goods_stored"])
        add = min(free, remaining)
        if add > 0:
            cur.execute(f"UPDATE pods SET {col}={col}+? WHERE id=?", (add, p["id"]))
            remaining -= add
    # Anything still remaining here means the whole org is full -- lost.

def _record_event_direct(cur, turn: int, event_type: str, actor_id=None,
                         subject_id=None, subject_type=None, payload=None):
    """
    Write an event directly via SQL rather than db.events.record_event --
    db/events.py imports get_current_turn from this module, so calling it
    from here would create a circular import (see _handle_colonize).
    """
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (turn,))
    seq = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,payload)
        VALUES (NULL,?,?,?,?,?,?,?)
    """, (turn, seq, event_type, actor_id, subject_id, subject_type, json.dumps(payload or {})))

def _apply_org_upkeep(cur, consumption: dict):
    """
    Per-organization upkeep, once per turn (not per pod) -- every ship/colony
    costs ORG_UPKEEP_COST to keep running at all, on top of whatever its
    individual pods cost. Applies regardless of transit state, and is not
    discounted or surcharged by org type: the colony advantage is purely on
    the output side (see COLONY_PRODUCTION_MULTIPLIER).
    Prorated the same way as pod recipes: not enough on hand means a partial
    (not all-or-nothing) draw of whatever's actually available.
    Runs before the per-pod production pass, so upkeep gets first claim on
    an org's stock for the turn.
    `consumption` is a {player_id: {resource: amount}} accumulator (see
    end_of_turn()'s turn.snapshot ledger) -- upkeep drains are tallied into
    it alongside pod recipe costs, so the ledger's derived waste figure
    accounts for every resource sink in the turn, not just production inputs.
    """
    for org in cur.execute("SELECT id, player_id FROM organizations").fetchall():
        org_id = org["id"]
        ratio = 1.0
        for resource, required in ORG_UPKEEP_COST.items():
            if required <= 0:
                continue
            available = _available_org_resource(cur, org_id, resource)
            ratio = min(ratio, available / required)
        ratio = max(0.0, min(1.0, ratio))
        if ratio > 0:
            for resource, required in ORG_UPKEEP_COST.items():
                amount = required * ratio
                _drain_org_resource(cur, org_id, resource, amount)
                consumption[org["player_id"]][resource] += amount

def end_of_turn():
    if is_game_over():
        print("Turn limit reached — game over."); return
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM games WHERE id=1")
    if cur.fetchone()[0] == 0:
        # No scenario selected yet (xsettlers_mcp/game_select.py's select_scenario populates
        # this on bootstrap). Nothing to process -- don't burn turns on an empty game.
        conn.close(); return
    conn.close()

    # 0. NPC decisions -- each is_npc=1 player with a profile acts before
    #    this turn resolves, by calling the same confirm_move/set_pod_task/
    #    set_mission tool functions a human player would (see engine/npc.py).
    #    Each of those calls opens and commits its own connection, so this
    #    runs to completion with nothing left open before the turn's own
    #    conn/cur (below) starts -- no shared transaction, no lock contention.
    #    Imported here rather than at module level to avoid a circular import:
    #    engine/npc.py's use of navigation_tools.confirm_move needs
    #    get_current_turn from this module, which isn't defined yet while
    #    this module is still executing its own top-level imports.
    from engine.npc import run_npc_decisions
    run_npc_decisions()

    conn = get_connection(); cur = conn.cursor()

    # 1. Reset declarations
    cur.execute("UPDATE players SET end_turn_declared=0")

    # 2. Resolve arrivals
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()[0]
    cur.execute("SELECT org_id,dest_x,dest_y,dest_z FROM arrival_queue WHERE arrival_turn<=?",
                (current_turn,))
    for arrival in cur.fetchall():
        org = cur.execute("SELECT player_id FROM organizations WHERE id=?",
                          (arrival["org_id"],)).fetchone()
        if org:
            dest_sector_id = reveal_sector(cur, org["player_id"],
                arrival["dest_x"], arrival["dest_y"], arrival["dest_z"])
            cur.execute("""UPDATE organizations SET sector_id=?,mission='idle',mission_params=NULL,
                           is_mobile=1 WHERE id=?""", (dest_sector_id,arrival["org_id"]))
    cur.execute("DELETE FROM arrival_queue WHERE arrival_turn<=?", (current_turn,))

    # 3. Org upkeep, then pod production (input-costed, see engine/production.py's
    #    POD_CONSUMPTION_RECIPE), then scan resolution.
    #    Produce tasks run for all pods regardless of transit state, but
    #    produce_energy specifically can't harvest anything while in transit
    #    (see below). Scan resolution runs only for stationary orgs (transit
    #    suppresses scan).
    #
    #    before_holdings/production/consumption feed the turn.snapshot ledger
    #    (see _snapshot_holdings, step 6 below) -- captured/accumulated here,
    #    inline with work this pass is already doing, so the ledger costs no
    #    extra queries beyond the one before/after holdings snapshot each.
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
        #    Each producing task (plus scan) costs some other resource(s)
        #    to run (see POD_CONSUMPTION_RECIPE) -- drawn
        #    from the org's own pooled stock of that resource (see
        #    _available_org_resource/_drain_org_resource above). idle costs
        #    nothing. Output is prorated to whatever fraction of the required
        #    input is actually available: e.g. only half the energy needed on
        #    hand gives half the normal output, rather than an all-or-nothing
        #    gate. produce_energy is additionally capped by the sector's own
        #    remaining pool (depleted as it's drawn from, floored at 0, no
        #    regeneration yet) -- a ship in transit is parked at the sentinel
        #    sector (id=-1, permanently 0 capacity), so this alone drives
        #    energy production to 0 while traveling, with no special-case
        #    branch needed. Other resources aren't sector-sourced at all.
        #    Known gap, deferred: when multiple players' pods share one
        #    sector, whoever's pod processes first (by pod id) gets first
        #    claim on what's left that turn -- no fair-split model yet.
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

            ratio = 1.0
            for resource, required in recipe.items():
                if required <= 0:
                    continue
                ratio = min(ratio, _available_org_resource(cur, org_id, resource) / required)

            for resource, base_amount in base_production.items():
                if resource not in RESOURCE_CAPACITY_COLUMN or base_amount <= 0:
                    continue
                col = RESOURCE_CAPACITY_COLUMN[resource]
                sector = cur.execute(
                    f"SELECT {col} AS remaining FROM sectors WHERE id=?",
                    (pod["org_sector_id"],)).fetchone()
                available_sector = sector["remaining"] if sector else 0.0
                ratio = min(ratio, available_sector / base_amount)

            ratio = max(0.0, min(1.0, ratio))

            if ratio > 0:
                for resource, required in recipe.items():
                    amount = required * ratio
                    _drain_org_resource(cur, org_id, resource, amount)
                    consumption[player_id][resource] += amount

                for resource, base_amount in base_production.items():
                    amount = base_amount * ratio
                    if resource in RESOURCE_CAPACITY_COLUMN and amount > 0:
                        col = RESOURCE_CAPACITY_COLUMN[resource]
                        cur.execute(f"UPDATE sectors SET {col}=MAX(0,{col}-?) WHERE id=?",
                                    (amount, pod["org_sector_id"]))
                    # Fills this pod's own free space first, then spills into
                    # other org pods with room, then is lost if the whole org
                    # is full (see _store_org_resource) -- production tallied
                    # here regardless of whether it actually fit; the gap
                    # between this and what _player_holdings later shows
                    # stored is exactly the turn.snapshot ledger's waste figure.
                    _store_org_resource(cur, org_id, pod["id"], resource, amount)
                    if amount > 0:
                        production[player_id][resource] += amount

        # 3c. Scan: costs food (see POD_CONSUMPTION_RECIPE) but produces no
        #     output -- stationary orgs only (transit suppresses scan).
        elif task == "scan":
            recipe = get_consumption_recipe(task)
            org_id = pod["org_id"]
            ratio = 1.0
            for resource, required in recipe.items():
                if required <= 0:
                    continue
                ratio = min(ratio, _available_org_resource(cur, org_id, resource) / required)
            ratio = max(0.0, min(1.0, ratio))
            if ratio > 0:
                for resource, required in recipe.items():
                    amount = required * ratio
                    _drain_org_resource(cur, org_id, resource, amount)
                    consumption[player_id][resource] += amount

            org = cur.execute(
                "SELECT o.sector_id,o.player_id,s.coord_x,s.coord_y,s.coord_z FROM organizations o "
                "JOIN sectors s ON s.id=o.sector_id WHERE o.id=?", (pod["org_id"],)).fetchone()
            if ratio > 0 and org and org["sector_id"] != -1:
                # Aim is an OFFSET from wherever the org currently is, so it
                # survives the ship moving (see sector_tools.SCAN_BEARINGS).
                # Range was validated when the aim was set and cannot drift,
                # but it is re-checked here because get_scan_range() will
                # eventually vary per org (sensor pods).
                params = json.loads(pod["task_params"] or "{}")
                dx, dy, dz = (params.get("offset_x"), params.get("offset_y"),
                              params.get("offset_z"))
                if dx is not None and dy is not None and dz is not None:
                    tx, ty, tz = (org["coord_x"] + dx, org["coord_y"] + dy, org["coord_z"] + dz)
                    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
                    scan_range = get_scan_range(pod["org_id"])
                    if distance <= scan_range:
                        reveal_sector(cur, org["player_id"], tx, ty, tz)
                        # TODO: emit pod.scanned event; detect rivals
                    else:
                        _record_event_direct(cur, current_turn, "alert.scan_out_of_range",
                            actor_id=org["player_id"], subject_id=pod["id"], subject_type="pod",
                            payload={"pod_id": pod["id"], "org_id": pod["org_id"],
                                     "target_x": tx, "target_y": ty, "target_z": tz,
                                     "distance": distance, "range": scan_range})

    # 3d. Innate organization scan — every org can scan one sector per turn on
    #     its own account (a ship's bridge, a colony's headquarters), without
    #     dedicating a pod to it. Deliberately identical in cost and rules to
    #     carrying one scan pod: same food recipe, same range, same suppression
    #     while in transit, same out-of-range alert. An org that ALSO has scan
    #     pods simply gets both, each paying its own way.
    #
    #     Runs after pod scans so both are resolved against the same
    #     pre-existing state, and the aim persists across turns -- re-scanning
    #     is idempotent (reveal_sector doesn't re-randomize) and refreshes
    #     confidence, so holding a target is a legitimate way to keep a sector
    #     from blinking out.
    scan_recipe = get_consumption_recipe("scan")
    cur.execute("""SELECT o.id, o.player_id, o.sector_id,
                          o.scan_offset_x AS dx, o.scan_offset_y AS dy, o.scan_offset_z AS dz,
                          s.coord_x, s.coord_y, s.coord_z
        FROM organizations o LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.scan_offset_x IS NOT NULL
          AND o.scan_offset_y IS NOT NULL
          AND o.scan_offset_z IS NOT NULL""")
    for org in cur.fetchall():
        ratio = 1.0
        for resource, required in scan_recipe.items():
            if required <= 0:
                continue
            ratio = min(ratio, _available_org_resource(cur, org["id"], resource) / required)
        ratio = max(0.0, min(1.0, ratio))
        if ratio > 0:
            for resource, required in scan_recipe.items():
                amount = required * ratio
                _drain_org_resource(cur, org["id"], resource, amount)
                consumption[org["player_id"]][resource] += amount
        if ratio <= 0 or org["sector_id"] == -1:
            continue          # starved, or in transit: cost paid, no reveal
        tx, ty, tz = (org["coord_x"] + org["dx"], org["coord_y"] + org["dy"],
                      org["coord_z"] + org["dz"])
        distance = math.sqrt(org["dx"]**2 + org["dy"]**2 + org["dz"]**2)
        scan_range = get_scan_range(org["id"])
        if distance <= scan_range:
            reveal_sector(cur, org["player_id"], tx, ty, tz)
        else:
            _record_event_direct(cur, current_turn, "alert.scan_out_of_range",
                actor_id=org["player_id"], subject_id=org["id"], subject_type="organization",
                payload={"org_id": org["id"], "target_x": tx, "target_y": ty,
                         "target_z": tz, "distance": distance, "range": scan_range})

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

    Completes this function's own long-standing TODO (2026-07-30): a
    turn.snapshot event is now written per player per turn -- previously
    holdings were only ever printed, never persisted, so reconstructing a
    game's history (e.g. "how much did each player waste, turn by turn")
    meant replaying the whole game from bootstrap in a scratch DB. Payload
    carries the after-state holdings, this turn's score (same score_weights
    formula as show_game_status/_calculate_final_scores), and derived waste.

    Waste is derived, not directly measured -- for each resource:
        wasted = produced - consumed - (after - before)
    i.e. whatever was produced and consumed this turn but doesn't show up in
    the actual before/after delta was lost to a full pod with nowhere to
    spill (see _store_org_resource). `production`/`consumption` are
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
        score = (after["energy"] * weights.get("energy", 0) + after["food"] * weights.get("food", 0)
                 + after["goods"] * weights.get("goods", 0))
        _record_event_direct(cur, turn, "turn.snapshot", actor_id=pid, subject_id=pid,
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
    applied to each player's total stored energy/food/goods, highest wins --
    same formula xsettlers_mcp/tools/organization_tools.py's show_game_status
    uses, so the winner here always matches what was checkable mid-game via
    that tool.

    The result is PERSISTED as a `game.final_scores` event, not merely printed.
    A game whose outcome exists only in a server log is a game nobody can be
    told they won: players need the result back, and replay/audit needs the
    scoreboard as it stood at the final whistle rather than something
    recomputed later against state that may have moved on. The payload carries
    the full breakdown (rank, score, per-resource totals, and the weights used
    to derive it) so the scoreboard is self-explaining without re-reading
    config.

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
    cur.execute("""
        SELECT p.id AS player_id, p.display_name,
               SUM(pods.energy_stored) AS energy,
               SUM(pods.food_stored) AS food,
               SUM(pods.goods_stored) AS goods,
               SUM(pods.storage_capacity) AS capacity
        FROM players p
        LEFT JOIN organizations o ON o.player_id = p.id
        LEFT JOIN pods ON pods.org_id = o.id
        GROUP BY p.id
    """)
    standings = []
    for row in cur.fetchall():
        energy, food, goods = row["energy"] or 0, row["food"] or 0, row["goods"] or 0
        score = (energy * weights.get("energy", 0) + food * weights.get("food", 0)
                 + goods * weights.get("goods", 0))
        standings.append({"player_id": row["player_id"], "display_name": row["display_name"],
                          "score": score, "energy": energy, "food": food, "goods": goods,
                          "total": energy + food + goods, "capacity": row["capacity"] or 0})
    standings.sort(key=lambda s: s["score"], reverse=True)
    for rank, s in enumerate(standings, start=1):
        s["rank"] = rank

    final_turn = cur.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()["current_turn"]
    _record_event_direct(cur, final_turn, FINAL_SCORES_EVENT,
                         payload={"final_turn": final_turn, "turn_limit": TURN_LIMIT,
                                  "score_weights": dict(weights),
                                  "winner": standings[0]["display_name"] if standings else None,
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
    conn = get_connection()
    row = conn.execute("SELECT payload FROM events WHERE event_type=? ORDER BY id LIMIT 1",
                       (FINAL_SCORES_EVENT,)).fetchone()
    conn.close()
    return json.loads(row["payload"]) if row else None
    return standings

def _handle_colonize(cur, org, current_turn):
    """Resolve a matured colonize_complete event: flip org_type to 'colony' and log ship.colonized."""
    if org["org_type"] != "ship":
        return
    _record_event_direct(cur, current_turn, "ship.colonized", subject_id=org["id"],
        subject_type="organization", payload={"org_id": org["id"], "sector_id": org["sector_id"]})
    cur.execute("""UPDATE organizations
        SET org_type='colony',is_mobile=0,mission='idle',mission_params=NULL WHERE id=?""",
        (org["id"],))

def _handle_defend(cur, org, params): pass   # stub
def _handle_attack(cur, org, params): pass   # stub

def check_consensus_acceleration():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM players WHERE end_turn_declared=0")
    undeclared = cur.fetchone()[0]; conn.close()
    if undeclared == 0:
        print("All players declared end of turn — accelerating clock.")
        end_of_turn(); return True
    return False
