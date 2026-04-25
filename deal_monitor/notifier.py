"""
Notifier: sends alerts via ntfy.sh -- a free, no-account push service.

Setup on the receiving side:
  1. Install the 'ntfy' app on your phone (iOS or Android).
  2. Subscribe to a topic name only you know (e.g. 'stas-deals-7f3k2vQ').
  3. Put that topic name in the NTFY_TOPIC environment variable.

Security note: ntfy.sh topics have no authentication. Anyone who guesses
your topic name can send you alerts. Use an unguessable name -- treat it
like a password. Do NOT use names like 'deals' or 'prices'.
"""

# --- Standard library imports -------------------------------------------------
import logging
from typing import Iterable

# --- Third-party imports ------------------------------------------------------
import requests

# --- Local imports ------------------------------------------------------------
from .checkers import Alert

logger = logging.getLogger(__name__)

# The public ntfy.sh JSON publish endpoint. Self-hosted ntfy instances use
# the same protocol -- if you ever move off the public server, change the
# host portion here and add an env var for it.
NTFY_PUBLISH_URL = "https://ntfy.sh/"

# Per-alert request timeout in seconds. ntfy is fast; if it doesn't respond
# in 15 seconds something's wrong and we'd rather move on than block.
NTFY_TIMEOUT_SECONDS = 15


def send_alerts(alerts: Iterable[Alert], topic: str) -> None:
    """
    POST each alert to ntfy.sh as a JSON payload.

    A failed individual alert is logged but does not stop the rest from
    being sent -- one notification hiccup shouldn't lose an entire batch.

    We use the JSON publish endpoint rather than the simpler header-based
    POST because JSON handles unicode (titles with em-dashes, accented
    characters, etc.) cleanly. The header-based form requires ASCII-only
    titles and silently mangles other characters.
    """
    if not topic:
        # Treat empty topic as a config error rather than silently dropping.
        # The main loop already validates this, but defense in depth helps.
        logger.error("No NTFY_TOPIC configured; cannot send notifications.")
        return

    # Materialise once so we can log a count without consuming an iterator.
    alerts_list = list(alerts)
    if not alerts_list:
        return

    logger.info("Posting %d alert(s) to ntfy topic '%s'.", len(alerts_list), topic)

    for alert in alerts_list:
        # ntfy.sh JSON schema:
        # https://docs.ntfy.sh/publish/#publish-as-json
        payload = {
            "topic": topic,
            "title": alert.title,
            "message": alert.message,
            # 'click' is the URL the notification opens when tapped --
            # super handy for jumping straight to the deal page.
            "click": alert.url,
            # 'tags' renders as emoji on the receiving device. Purely
            # cosmetic but it makes the alert recognisable at a glance.
            "tags": ["tada"],
            # Default priority. Bump to 'high' for time-sensitive alerts
            # (configurable per rule in a future version).
            "priority": 3,
        }

        try:
            response = requests.post(
                NTFY_PUBLISH_URL,
                json=payload,
                timeout=NTFY_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error(
                "Failed to send alert '%s': %s", alert.title, exc,
            )
            # Don't break -- continue with the next alert.
            continue

        logger.info("Sent: %s", alert.title)
