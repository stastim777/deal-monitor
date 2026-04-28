"""
Notifier: sends alerts via Pushover (https://pushover.net).

Pushover is a one-time-purchase push notification service ($5 per platform)
that delivers reliably to iOS via Apple Push Notification Service (APNs).
Unlike free ntfy.sh on iOS -- which can only poll when the app is opened --
Pushover messages arrive as proper background push notifications.

Setup on the receiving side:
  1. Install the 'Pushover' app on your phone (iOS App Store).
  2. Create a free account at https://pushover.net and sign in on the app.
  3. Copy your "User Key" from the Pushover dashboard.
  4. On the dashboard, create an "Application" (any name, e.g.
     'Deal Monitor'). Copy the resulting "API Token/Key".
  5. Set both as environment variables:
       PUSHOVER_USER_KEY  = <your user key>
       PUSHOVER_APP_TOKEN = <your application api token>

Security note: User keys and app tokens are private credentials. Treat them
like passwords. Use GitHub Actions Secrets (not plain env vars in the
workflow file) so they never appear in commit history or logs.
"""

# --- Standard library imports -------------------------------------------------
import logging
from typing import Iterable

# --- Third-party imports ------------------------------------------------------
import requests

# --- Local imports ------------------------------------------------------------
from .checkers import Alert

logger = logging.getLogger(__name__)

# Pushover's "Messages" API endpoint. Documented at:
#   https://pushover.net/api#messages
# This URL is stable and has been unchanged for years.
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Per-alert request timeout in seconds. Pushover normally responds in under
# a second; 15s gives slow networks plenty of headroom without letting one
# stuck request block the whole notification batch.
PUSHOVER_TIMEOUT_SECONDS = 15

# Pushover's hard cap on message body length. Anything longer is rejected
# with HTTP 400. We truncate proactively so we never see that error.
PUSHOVER_MESSAGE_MAX_CHARS = 1024

# Hard cap on title length per Pushover docs.
PUSHOVER_TITLE_MAX_CHARS = 250


def _truncate(text: str, max_chars: int) -> str:
    """
    Truncate a string to max_chars, appending an ellipsis if truncation
    actually occurred. Returns the original string when it already fits.

    We use a literal '...' (three ASCII periods) rather than the unicode
    ellipsis character so we never have to think about encoding edge cases
    when the string is later embedded in a form-encoded POST body.
    """
    if len(text) <= max_chars:
        return text
    # -3 leaves room for the '...' suffix while staying within the cap.
    return text[: max_chars - 3] + "..."


def send_alerts(alerts: Iterable[Alert], topic: str) -> None:
    """
    POST each alert to Pushover.

    The signature keeps the `topic` parameter name for backward
    compatibility with the existing main.py call site, but for Pushover
    we expect this string to be formatted as:
        "<USER_KEY>:<APP_TOKEN>"

    main.py builds this string from the two environment variables before
    calling us. Splitting credentials this way (vs. two separate args)
    keeps the notifier interface stable across notification backends, so
    swapping ntfy <-> Pushover <-> Slack later is a one-file change.

    A failed individual alert is logged but does not stop the rest from
    being sent -- one notification hiccup shouldn't lose an entire batch.
    """
    if not topic:
        # Treat empty credentials as a config error rather than silently
        # dropping. Defense in depth -- main.py already validates this.
        logger.error("No Pushover credentials configured; cannot send.")
        return

    # Split the combined credential string. We use rsplit with maxsplit=1
    # in case (extremely unlikely, but possible) the user key itself
    # contains a colon -- the app token never does.
    if ":" not in topic:
        logger.error(
            "Pushover credentials must be formatted as "
            "'USER_KEY:APP_TOKEN'; got malformed value."
        )
        return
    user_key, app_token = topic.rsplit(":", 1)
    user_key = user_key.strip()
    app_token = app_token.strip()

    if not user_key or not app_token:
        logger.error("Pushover user key or app token is empty after parsing.")
        return

    # Materialise once so we can log a count without consuming an iterator.
    alerts_list = list(alerts)
    if not alerts_list:
        return

    logger.info("Posting %d alert(s) to Pushover.", len(alerts_list))

    for alert in alerts_list:
        # Pushover's API takes form-encoded POST data, NOT JSON. Sending
        # JSON results in a HTTP 400 with a misleading error -- one of
        # the easier mistakes to make when porting from a JSON-based API.
        # See https://pushover.net/api#messages for the field reference.
        payload = {
            "token": app_token,
            "user": user_key,
            "title": _truncate(alert.title, PUSHOVER_TITLE_MAX_CHARS),
            "message": _truncate(alert.message, PUSHOVER_MESSAGE_MAX_CHARS),
            # 'url' makes the notification tappable -- opens the deal page.
            "url": alert.url,
            # 'url_title' is the label shown for the link in the notification.
            # Keeping it short ('View deal') reads cleanly in the iOS banner.
            "url_title": "View deal",
            # Priority scale: -2 (no notification) ... 0 (default) ... 2 (emergency).
            # 0 is right for deal alerts -- they're informational, not critical.
            "priority": 0,
        }

        try:
            response = requests.post(
                PUSHOVER_API_URL,
                # `data=` for form-encoded body. Do NOT use `json=` here.
                data=payload,
                timeout=PUSHOVER_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # Pushover returns useful error details in the response body
            # when status is non-2xx. Log them when available so the user
            # can diagnose without having to re-run with --debug.
            body = ""
            if exc.response is not None:
                # response.text can be huge if a proxy intercepted; cap it.
                body = exc.response.text[:500]
            logger.error(
                "Failed to send alert '%s': %s | response body: %s",
                alert.title, exc, body,
            )
            # Don't break -- continue with the next alert.
            continue

        logger.info("Sent: %s", alert.title)
