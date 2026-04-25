"""
State persistence: read/write a JSON file that survives between runs via
git commits in the GitHub Actions workflow.

The state file tracks two things:
  - For url_price rules: the last alert timestamp, so we can enforce
    cooldown windows and avoid alert spam when a price stays low.
  - For rss_keyword rules: the set of feed item GUIDs we've already
    alerted on, so we don't re-alert on the same item.

State file layout (state.json):
{
  "<rule_id>": {
    "last_alert_at": "2026-04-25T13:51:00+00:00",  # ISO 8601
    "last_alert_price": 47.50,                      # url_price only
    "seen_ids": ["guid-1", "guid-2", ...]           # rss_keyword only
  },
  ...
}
"""

# --- Standard library imports -------------------------------------------------
import json
import logging
import os
from datetime import datetime
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Filename is fixed and lives at the repo root so GitHub Actions can commit it.
STATE_FILE = "state.json"


# =============================================================================
# Load / save
# =============================================================================

def load_state() -> dict:
    """
    Load the state file. Missing or corrupt files yield an empty dict so
    the first-ever run "just works" without a manually-created file.
    """
    if not os.path.exists(STATE_FILE):
        logger.info("No %s found -- starting with empty state.", STATE_FILE)
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "Could not read %s (%s). Continuing with empty state.",
            STATE_FILE, exc,
        )
        return {}

    # Defensive type check: if someone hand-edits the file and breaks it,
    # we want a clean failure mode rather than a TypeError downstream.
    if not isinstance(data, dict):
        logger.error("%s is not a JSON object; starting fresh.", STATE_FILE)
        return {}

    return data


def save_state(state: dict) -> None:
    """
    Atomically write the state file: write to a temp file, then rename.

    Atomic rename means an interrupted run (e.g. cancelled workflow) can
    never leave a half-written state file. On POSIX, os.replace is atomic.
    """
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        # sort_keys=True produces stable diffs in git, which makes the
        # commit history actually readable.
        # indent=2 makes the file human-editable in case the user wants
        # to manually clear cooldowns or seen-IDs.
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# =============================================================================
# Per-rule helpers
# =============================================================================

def get_rule_state(state: dict, rule_id: str) -> dict:
    """
    Get (and create if missing) the per-rule state dict.

    Mutating the returned dict mutates `state` -- this is intentional and
    relied upon by the main loop, which does all its updates in-place
    before a single save_state() at the end.
    """
    return state.setdefault(rule_id, {})


def is_in_cooldown(
    rule_state: dict,
    cooldown_hours: float,
    now: datetime,
) -> bool:
    """
    True if we've alerted on this rule recently enough to suppress another.

    Returns False when:
      - cooldown_hours is 0 or negative (cooldown disabled)
      - the rule has never alerted
      - the last_alert_at timestamp is malformed
    """
    if cooldown_hours <= 0:
        return False

    last = rule_state.get("last_alert_at")
    if not last:
        return False

    # fromisoformat handles the timezone offset we wrote out.
    try:
        last_dt = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        # Corrupt timestamp -- treat as "no prior alert" so a bad value
        # doesn't permanently block alerts.
        logger.warning(
            "Could not parse last_alert_at='%s'; ignoring cooldown.", last,
        )
        return False

    elapsed_seconds = (now - last_dt).total_seconds()
    cooldown_seconds = cooldown_hours * 3600.0
    return elapsed_seconds < cooldown_seconds


def mark_alerted(
    rule_state: dict,
    now: datetime,
    current_price: Optional[float] = None,
) -> None:
    """
    Stamp the rule state with the current timestamp (and price, if given)
    immediately after a successful alert.
    """
    # isoformat() preserves the UTC offset on a tz-aware datetime, which
    # is what fromisoformat() above expects on read-back.
    rule_state["last_alert_at"] = now.isoformat()
    if current_price is not None:
        # Round to cents to keep state file diffs clean.
        rule_state["last_alert_price"] = round(current_price, 2)


# =============================================================================
# RSS-specific helpers
# =============================================================================

def get_seen_ids(rule_state: dict) -> Set[str]:
    """
    Return the set of feed item GUIDs we've already alerted on.

    Stored as a list in JSON (sets aren't JSON-serializable), converted
    to a set here for O(1) membership checks.
    """
    raw = rule_state.get("seen_ids", [])
    if not isinstance(raw, list):
        # Defensive: corrupt state -> empty set rather than crash.
        logger.warning("seen_ids is not a list; resetting.")
        return set()
    return set(raw)


def save_seen_ids(rule_state: dict, ids: Set[str]) -> None:
    """
    Persist the seen-GUIDs set back to state.

    We write a sorted list rather than the set's iteration order so git
    diffs are stable and meaningful (you can see which IDs were added
    versus just rearranged).

    The set is naturally bounded by the size of the feed (we replace the
    saved IDs with current_ids each run), so no explicit cap is needed.
    """
    rule_state["seen_ids"] = sorted(ids)
