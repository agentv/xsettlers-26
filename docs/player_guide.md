# XSettlers — Player Guide

This is the rulebook. If you just want to know how to play — what your ships do,
what your pods do, and how you win — start here. For the underlying data model
and tool APIs, see [Product Requirements](product_requirements.md); this doc
never contradicts that one, it just explains the same rules in plain language.

---

## The Setting

Mankind has at last inherited the stars.

With the invention of reliable faster-than-light travel, humanity has broken
free of its home system and begun the long work of spreading across the
galaxy. Scout ships roam the spaceways searching for habitable worlds. Colony
ships follow, carrying the people, equipment, and resources needed to claim
and shape whatever they find.

You command one of these fledgling societies. You control a fleet of ships
that move through a grid of space sectors, harvesting energy and turning it
into food and goods. Ships can be converted into permanent colonies —
trading mobility for a fixed foothold.

You are not alone. Rival civilizations are out there too, expanding from
their own home worlds, scouting the same promising sectors, racing toward the
same rich territories.

**The player holding the most valuable stockpile at the end of the game
wins** — and value is not the same as volume. See [Winning](#winning).

---

## The Basics

The game is played turn by turn. You issue commands whenever you're ready —
there's no real-time pressure. The turn resolves on a fixed timer, or early
the moment every player has declared they're done: ships that were traveling
arrive, pods produce, scans reveal what they found, and the universe advances
by one tick.

You play through an **MCP client** — Slack is the intended home, but the
server doesn't care which client you use.

Everything you own falls into one of two kinds of **organizations**:

- **Ships** — mobile. They move across the map and carry pods that keep
  producing the whole time.
- **Colonies** — stationary. Once a ship converts into a colony it can never
  move again, but it becomes a fixed, permanent base of operations.

Every organization — ship or colony — carries **pods**, and every
organization also has **sensors of its own** (see
[Scanning & Discovery](#scanning--discovery)).

### Names and call signs

Every unit is launched with a **given name** — `S1`, `S2`, `C1` and so on.
That name is fixed for the whole game. It's short on purpose, so you can say
it out loud, and it stays put so you always have a stable way to refer to a
unit.

On top of that, you can give any ship, colony, or task force a **call sign** —
your own name for it, up to 24 characters. Call `S3` "Vanguard" and every
report starts calling it Vanguard. Change it whenever you like; the given name
underneath never moves, so nothing gets lost when you do.

A call sign is purely for you and your reports — nothing in the game's rules
reads it. Keep them unique within your own fleet, and don't hand one unit
another's given name (calling `S1` "S2" while a real `S2` is flying around
helps nobody).

---

## Pods: What Actually Produces

A pod's job is its **task**, which you can reassign at any time. (Note the
vocabulary: *pods* have **tasks**, *organizations* have **missions**. They are
different things and the game uses different words for them deliberately.)

| Task | What it does |
|---|---|
| `produce_energy` | Harvests energy from the sector the organization is sitting in. This is the only thing drawn from the map itself — and the sector runs down as you take it. |
| `produce_food` | Manufactures food. Costs energy and goods to run. |
| `produce_goods` | Manufactures goods. Costs energy and food to run. Slower than the other two, and the most valuable thing you can make. |
| `scan` | Looks at a nearby sector and reports what's there. Costs energy and food — a scanner that runs out of energy goes blind. |
| `idle` | Does nothing, and costs nothing. |

Three things worth understanding early, because they shape everything:

**Sectors are not all the same.** Every sector you discover holds somewhere
between 500 and 1000 energy, decided the moment it is first found and fixed
from then on. You cannot know which you have until you look — but nowhere is
barren, so a poor find costs you upside, not survival. And what you see is
what is genuinely there: if a rival got somewhere before you, you inherit
whatever they have already drained out of it.

**Your home sector is the exception.** It is deep enough that you will never
exhaust it, no matter how long the game runs or how much you park there. Home
is a refuge, not a prize — the ground you can always fall back on. Everywhere
else is finite, which is what makes where you go a decision.

**Only energy comes from the map.** Food and goods are manufactured out of
what you already hold. Energy is therefore the input to your whole economy —
and the sector you're standing on depletes as you draw on it. Sit still long
enough and the ground runs dry.

**Storage is shared and finite.** Each pod has a capacity, and production
that has nowhere to go is simply lost. A full fleet produces nothing of
value, so watch how full you are, not just what you're making.

Pods keep running their task every turn, whether their ship is sitting still
or mid-journey — with two exceptions while traveling: energy can't be
harvested (there's no sector to harvest from), and scans don't report back.

---

## Movement

When you send a ship somewhere it doesn't teleport — it takes a number of
turns proportional to the distance. While it's en route:

- The ship is **in transit**, sitting in no normal sector; you'll see it
  flagged as such.
- It **can't be given a new mission**, and its pods can't be retasked, until
  it arrives — or you cancel the move. Set anything you want it doing on
  arrival *before* you send it.
- Its food and goods pods **keep working** the entire time, spending down the
  energy it's carrying with no way to replace it. A long voyage can arrive
  with empty tanks and no way to restart its own economy.

Distance is measured in a straight line, and travel time rounds up. **A ship
covers 2 sectors per turn**, so every sector touching the one you're in —
diagonals included — is a single turn away. Two sectors out diagonally is a
distance of 2.83 and takes two turns, while two sectors out straight is
exactly one. Straight lines are still better value over distance; it's the
first hop in any direction that's free of that penalty.

You don't set a ship's speed — it's a property of the hull, the same way its
storage is. You name where you want it to go and the ship takes as long as it
takes.

You can preview a move before committing to see how long it takes. Once
committed you can still cancel while it's in progress — the ship rubber-bands
back to where it started, with no credit for the distance covered.

---

## Colonizing

Any ship can be ordered to colonize the sector it's sitting in. Once that
completes the ship becomes a permanent colony there: it loses the ability to
move, but everything it produces goes up by **half again**. Every pod aboard
a colony works at 1.5× a ship's rate, for exactly the same running costs.

That is worth more than it sounds. Your costs don't change, so the whole
bonus lands on the margin — an organization barely breaking even as a ship
can be comfortably profitable as a colony.

**Colonizing costs 30 energy**, taken from the ship's holds the moment you
give the order. A ship without it is simply refused — this is the one cost
in the game with no partial credit, because there's no such thing as half a
colony. The ship is then locked for a short transition window before the
conversion finishes, and it works at the old ship rate until it does.

Choose the ground carefully, twice over. A colony can never relocate, so it
lives or dies on the sector you left it in — and it draws that sector down
1.5× as fast as a ship would. The bonus is real, but it spends the ground
underneath it quicker.

---

## Scanning & Discovery

Space is unknown at the start. You only know what you have actually looked at.

**Every organization can scan one sector per turn on its own account** — a
ship's bridge, a colony's headquarters — without dedicating a pod to it. Pods
can *additionally* be put on the `scan` task for more coverage. The rules are
identical either way; the only difference is what carries the equipment. Each
scan costs energy and food, so scanning competes directly with production for
the same stock.

You aim a scan by **bearing** — a direction and distance relative to the
scanner, not a fixed map coordinate:

```
        N2
    NW  N  NE
W2  W   ·   E   E2
    SW  S  SE
        S2
```

Those twelve bearings are exactly what a scanner can reach. `N`/`E`/`S`/`W`
are one sector away, the diagonals about 1.41, and `N2`/`E2`/`S2`/`W2` are two.
Anything further is out of range and will be refused when you set it.

Because a bearing is relative, **it travels with the ship**. Point a scout
north-east once and it keeps scanning north-east wherever it goes, with no
re-aiming after a move. A scan reveals only the sector you aimed at — range
governs how far you can reach, not how much you see.

Ships in transit can be aimed, but won't report until they land.

**Knowledge fades.** A sector you aren't occupying loses confidence every
turn, and after five turns without a fresh look it **blinks off your map
entirely** — indistinguishable from somewhere you've never been. Revisiting
territory matters, not just discovering it once. Holding a scan bearing on a
sector keeps it visible indefinitely.

---

## Winning

The game runs for a fixed number of turns. At the end of the last turn,
everything in storage across every ship and colony you own is scored.

**Value is not volume.** Each resource is weighted:

| Resource | Points per unit |
|---|---|
| Goods | **2** |
| Food | **1** |
| Energy | **0** |

Energy scores nothing. It is a *means* — the input that makes food and goods
possible — not an asset. A hold full of energy at the final whistle is worth
precisely nothing, so the endgame question is how much of it you managed to
convert before time ran out.

Goods are worth double food and are the slowest thing to produce. That
tension is the game: the most valuable resource is the one you can make least
of, and making it costs you both of the others.

**Highest score wins.** There's no bonus for territory, for the number of
colonies you hold, or for sectors explored — only for what's in the hold when
the music stops.

---

## Quick Reference

| You want to... | Command |
|---|---|
| See which games you can join | `list_scenarios` |
| Start or join one | `select_scenario` |
| See how long a move would take, without committing | `preview_move` |
| Actually move a ship | `confirm_move` |
| Change your mind mid-flight | `cancel_move` |
| Give an organization a mission (move/colonize/defend/attack/idle) | `set_mission` |
| Change what a pod is doing | `set_pod_task` |
| Aim an organization's own sensors | `set_org_scan_bearing` |
| Aim a scan pod | `set_pod_scan_bearing` |
| Give a ship or colony a call sign | `set_call_sign` |
| Give a task force a call sign | `set_task_force_call_sign` |
| Check on one ship or colony | `show_organization` |
| Review your whole fleet | `show_civilization_status` |
| Look at the space around something | `show_sector_neighborhood` |
| See what the sectors nearby are worth | `show_neighborhood_resources` |
| See the standings | `show_game_status` |
| Signal you're done for this turn | `declare_end_turn` |
| Take that back | `rescind_end_turn` |

Every command takes your `player_token` as its first argument. Full technical
detail lives in [Product Requirements](product_requirements.md).

---

## Scenarios

A scenario decides who plays, where they start, what they start with, and how
rich they start. Three ship today; `list_scenarios` shows the ones you're
seated in.

### Solo (`game_solo`)

One player, no rivals. Eight ships plus an established colony at
`(10,10,0)`, everything starting at 30% of storage capacity — so production
actually matters from turn 1. Because you're the only player, declaring end of
turn advances the clock immediately instead of waiting out the timer, which
makes this much the best scenario for learning the game or testing a client.

### Diaspora (`game0`)

Two players, 8 ships each, **no starting colony** — everyone begins mobile and
picks their own moment to plant roots. Home sectors are 25 sectors apart.

### Outbreak (`game1`)

One foothold, and a fleet to expand from it.

- **Players:** 2.
- **Starting colony:** each player begins with a colony already established at
  their home sector — a fixed foothold from turn 1.
- **Starting fleet:** 8 ships per player, all identical, spawned alongside the
  home colony.
- **Pods:** every organization — each ship and the home colony alike — carries
  the same 6-pod loadout: 2 energy, 2 goods, 2 food, all producing from turn 1.
- **Starting positions:** home sectors about 12.7 sectors apart — far enough
  for a real exploration phase, close enough that contact is likely mid-game.
- **Length:** 20 turns, then scores are tallied.

All three scenarios currently start holds at 30% of capacity. That is a
scenario setting rather than a rule of the game — a future variant is free to
start you rich — but starting lean is what makes production matter from turn 1
instead of being wasted against a hold that is already full.

> *Naming note: Outbreak was originally sketched under the name "Diaspora"
> before either scenario file existed. That name now belongs to `game0`.*
