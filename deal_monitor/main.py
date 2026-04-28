"""
Entry point for the deal monitor.

Run as:  python -m deal_monitor.main

Required environment variables:
    SHEET_CSV_URL       The URL to your published Google Sheet (CSV format).
    PUSHOVER_USER_KEY   Your Pushover user key (from pushover.net dashboard).
    PUSHOVER_APP_TOKEN  Your Pushover application API token (also from
                        pushover.net -- create an Application to get one).

Exit codes:
    0   Success (whether or not any alerts fired).
    1   Fatal error (missing config, sheet unreachable, etc.).
"""

# --- Standard library imports -------------------------------------------------
import logging
import os
import sys
from datetime import datetime, timezone
from typing import List

# --- Local imports ------------------------------------------------------------
from .checkers import Alert, check_rss_keyword, check_url_price
from .config_loader import load_rules
from .notifier import send_alerts
from .state import (
    get_rule_state,
    get_seen_ids,
    is_in_cooldown,
    load_state,
    mark_alerted,
    save_seen_ids,
    save_state,
)


# =============================================================================
# Logging setup
# =============================================================================

def configure_logging() -> None:
    """
    Configure root logging once at startup.

    Output goes to stdout (not stderr) so GitHub Actions' log viewer
    shows everything in chronological order. The format includes
    timestamps because Actions logs already have them, but local runs
    don't, and consistency between the two helps debugging.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


# =============================================================================
# Per-rule dispatch
# =============================================================================

def _process_url_price(rule, rule_state, now) -> List[Alert]:
    """
    Run a url_price rule and update its state in place.
    Returns the alerts (0 or 1) that should be sent.
    """
    alerts = check_url_price(rule)
    if alerts:
        # url_price always returns exactly 0 or 1 alerts, but loop for
        # forward compatibility in case we ever return more.
        for alert in alerts:
            mark_alerted(rule_state, now, current_price=alert.current_price)
    return alerts


def _process_rss_keyword(rule, rule_state, now, log) -> List[Alert]:
    """
    Run an rss_keyword rule and update its state in place.

    Special "first run" behaviour: if we've never seen this rule before
    (no stored seen_ids), we seed the state with all currently-visible
    items WITHOUT alerting. Otherwise, the user would get a flood of
    alerts the first time the rule runs (every matching historical item
    in the feed at once). Going forward, only NEW items can trigger.
    """
    previously_seen = get_seen_ids(rule_state)
    is_first_run = "seen_ids" not in rule_state

    alerts, current_ids = check_rss_keyword(rule, previously_seen)

    if is_first_run:
        log.info(
            "Rule '%s' first run: seeding %d existing item(s) "
            "without alerting.",
            rule.name, len(current_ids),
        )
        # Save the snapshot so the next run can detect "new since now".
        save_seen_ids(rule_state, current_ids)
        return []

    if alerts:
        mark_alerted(rule_state, now)

    # Always update the seen-ID snapshot, even when no alerts fired.
    # This keeps the snapshot tracking the current feed contents.
    save_seen_ids(rule_state, current_ids)

    return alerts


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """
    One full check pass. Returns an exit code suitable for sys.exit().
    """
    configure_logging()
    log = logging.getLogger("deal_monitor")

    # --- Read required env vars ----------------------------------------------
    # We do explicit empty-string checks because os.environ.get returns ''
    # for an unset GitHub Actions secret, not None -- and bool('') is False
    # but `not value` is clearer about intent.
    sheet_url = os.environ.get("SHEET_CSV_URL", "").strip()
    pushover_user = os.environ.get("PUSHOVER_USER_KEY", "").strip()
    pushover_token = os.environ.get("PUSHOVER_APP_TOKEN", "").strip()

    if not sheet_url:
        log.error("SHEET_CSV_URL environment variable is required.")
        return 1
    if not pushover_user:
        log.error("PUSHOVER_USER_KEY environment variable is required.")
        return 1
    if not pushover_token:
        log.error("PUSHOVER_APP_TOKEN environment variable is required.")
        return 1

    # The notifier expects a single combined credential string. Combining
    # here (rather than passing two separate args) keeps the notifier
    # interface stable across notification backends -- swapping providers
    # later means changing only notifier.py and these few lines.
    pushover_credentials = f"{pushover_user}:{pushover_token}"

    # --- Load rules ----------------------------------------------------------
    try:
        rules = load_rules(sheet_url)
    except Exception as exc:
        # Failing to load the sheet is fatal: no rules means no work to do
        # and we should exit non-zero so the workflow run shows as failed.
        log.error("Could not load rules sheet: %s", exc)
        return 1

    if not rules:
        log.info("No enabled rules in sheet -- nothing to do.")
        return 0

    # --- Run rules -----------------------------------------------------------
    state = load_state()
    # Use UTC throughout. Local time on a GitHub runner is unspecified and
    # can change without notice; UTC is stable and serialises cleanly.
    now = datetime.now(timezone.utc)
    all_alerts: List[Alert] = []

    for rule in rules:
        rule_state = get_rule_state(state, rule.rule_id)

        # Cooldown applies to ALL rule types -- we never want to spam,
        # regardless of why an alert is firing.
        if is_in_cooldown(rule_state, rule.cooldown_hours, now):
            log.info(
                "Rule '%s' is in cooldown (next alert in %.1fh max); skipping.",
                rule.name, rule.cooldown_hours,
            )
            continue

        # Dispatch on rule type.
        if rule.rule_type == "url_price":
            all_alerts.extend(_process_url_price(rule, rule_state, now))
        elif rule.rule_type == "rss_keyword":
            all_alerts.extend(_process_rss_keyword(rule, rule_state, now, log))
        else:
            # Unknown rule type -- log and continue. We don't fail because
            # the user might be in the middle of editing the sheet and we
            # don't want a typo to break the run.
            log.warning(
                "Unknown rule type '%s' for rule '%s'; skipping.",
                rule.rule_type, rule.name,
            )

    # --- Persist state BEFORE notifying --------------------------------------
    # Order matters: if we save state after notifying and the notify step
    # crashes, the next run would re-alert. Saving first means we only
    # double-notify in the much rarer case where notify succeeds but the
    # save fails.
    save_state(state)

    # --- Send alerts ---------------------------------------------------------
    if all_alerts:
        log.info("Sending %d alert(s).", len(all_alerts))
        send_alerts(all_alerts, pushover_credentials)
    else:
        log.info("No alerts to send this run.")

    return 0


# Entry guard so the module is also importable for testing.
if __name__ == "__main__":
    sys.exit(main())
