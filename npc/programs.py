"""
The library of named NPC programs: config/npc_programs/<name>.yaml, each one a
`program:` key holding the step list npc/script.py executes.

Named programs are what keeps a strategy's *name* a stable contract while its
implementation moves from Python into data. GameHouse validates an incoming
roster's strategy_name against the registry before a session starts
(xsettlers_mcp/gamehouse.py) and scripts/run_tournament.py enumerates it, so
'turtle' has to keep meaning turtle whether a function or a YAML file is behind
it. npc/strategies.py's STRATEGY_NAMES is that union.

Adding a scripted strategy is adding a file here -- no code change, the same
property config/game<N>.yaml already has for scenarios.
"""
import os
import yaml

PROGRAM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "npc_programs")

def load_programs() -> dict:
    """{name: program} for every YAML file in config/npc_programs/, keyed by
    filename stem. Read fresh rather than cached at import: a program is data,
    and a test or a tournament run that writes one should not have to care
    whether this module was imported first."""
    programs = {}
    if not os.path.isdir(PROGRAM_DIR):
        return programs
    for filename in sorted(os.listdir(PROGRAM_DIR)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(PROGRAM_DIR, filename)) as f:
            document = yaml.safe_load(f) or {}
        programs[os.path.splitext(filename)[0]] = document.get("program") or []
    return programs
