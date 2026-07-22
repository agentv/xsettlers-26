from config.loader import load_config

def authenticate(slack_user_id: str) -> dict:
    """
    Resolve a Slack identity against the game's fixed player roster
    (config/game_config.yaml's players list). Trusts the Slack-provided
    identity at face value for now; hardening deferred (see docs/TODO.md).

    Deliberately does NOT depend on the DB or on a game having been
    bootstrapped yet — the roster is config-driven and exists before any
    scenario is chosen. This is what makes it safe to call from
    game_select.select_scenario() before bootstrap has run.
    """
    cfg = load_config()
    for p in cfg.players:
        if p.slack_user_id == slack_user_id:
            return {"ok": True, "slack_user_id": slack_user_id,
                    "email": p.email, "display_name": p.display_name}
    return {"ok": False, "error": "Unrecognized player — not part of this game's roster"}
