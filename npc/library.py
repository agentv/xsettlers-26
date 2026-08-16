"""
The strategy library: config/npc_strategies/<name>.yaml, one document each
(see npc/strategy.py for the format).

A strategy's *name* is a stable contract with two consumers outside this
repo -- GameHouse validates an incoming roster's strategy_ref against this
library before a session starts (xsettlers_mcp/gamehouse.py), and
../xsettlers-designer's tournament runner plays whatever it lists. Adding a
strategy is adding a file here; nothing else has to be told.
"""
import os
import yaml

STRATEGY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "npc_strategies")


def load_strategies() -> dict:
    """{name: document} for every YAML file in config/npc_strategies/, keyed by
    filename stem. Read fresh rather than cached at import: a strategy is data,
    and a test or a tournament run that writes one should not have to care
    whether this module was imported first."""
    strategies = {}
    if not os.path.isdir(STRATEGY_DIR):
        return strategies
    for filename in sorted(os.listdir(STRATEGY_DIR)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(STRATEGY_DIR, filename)) as f:
            strategies[os.path.splitext(filename)[0]] = yaml.safe_load(f) or {}
    return strategies


def strategy_names() -> list:
    """Every strategy_ref that can be assigned. This is the list GameHouse
    validates a roster against and the field the tournament runner plays."""
    return sorted(load_strategies())


def get_strategy(name: str) -> dict | None:
    """One document by name, or None if the library has no such strategy."""
    return load_strategies().get(name)
