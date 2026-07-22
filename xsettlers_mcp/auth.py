import hmac
from config.loader import load_config

def authenticate(player_token: str) -> dict:
    """
    Resolve a player's opaque token against the game's fixed roster
    (config/game_config.yaml's players list). Client-agnostic by design --
    any caller (Slack, another LLM agent, curl) that knows a valid player's
    token authenticates the same way; nothing here is Slack-specific.

    player_token is each player's actual credential (not a public-looking
    ID), so comparison is constant-time (hmac.compare_digest) rather than
    plain == -- avoids leaking match-length information via timing.

    Deliberately does NOT depend on the DB or on a game having been
    bootstrapped yet -- the roster is config-driven and exists before any
    scenario is chosen. This is what makes it safe to call from
    game_select.select_scenario() before bootstrap has run.
    """
    cfg = load_config()
    for p in cfg.players:
        if hmac.compare_digest(p.player_token, player_token):
            return {"ok": True, "player_token": player_token,
                    "email": p.email, "display_name": p.display_name}
    return {"ok": False, "error": "Unrecognized player — not part of this game's roster"}
