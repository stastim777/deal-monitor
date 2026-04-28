"""
Rule checkers: the actual logic for evaluating each rule type.

Every checker returns Alert objects so the main loop can hand them to the
notifier in one batch. Checkers must NEVER raise on network errors -- they
log and return an empty list, so one broken site can't take down the run.
"""

# --- Standard library imports -------------------------------------------------
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

# --- Third-party imports ------------------------------------------------------
import requests
from bs4 import BeautifulSoup

# --- Local imports ------------------------------------------------------------
from .config_loader import Rule

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Polite, identifiable User-Agent. Some sites return 403 to the default
# 'python-requests/x.y.z' UA, and a real-looking UA reduces friction.
# The URL is informational; replace with your fork URL after deploying.
USER_AGENT = (
    "deal-monitor/1.0 (by /u/brattok7; "
    "personal price tracker; +https://github.com/stastim777/deal-monitor)"
)
# Default request timeout in seconds. Long enough for slow sites, short
# enough that one stuck request can't block the rest of the run.
HTTP_TIMEOUT_SECONDS = 30


# =============================================================================
# Data model
# =============================================================================

@dataclass
class Alert:
    """
    A single notification to be sent.

    rule_name and url are pass-through metadata; title and message are
    what the user actually sees on their phone.
    """
    rule_name: str
    title: str
    message: str
    url: str
    # Only set for url_price alerts; None for rss_keyword.
    current_price: Optional[float] = None


# =============================================================================
# HTTP helper
# =============================================================================

def _http_get(url: str) -> requests.Response:
    """
    Wrapper around requests.get with our standard timeout and UA.

    Raises requests.RequestException on any network or HTTP error, which
    callers must catch -- we don't want to suppress errors at this layer
    because the caller may want to decide what to do (e.g. RSS may want
    to retry while url_price just gives up).
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        # Some CDNs do content negotiation. Asking for English keeps prices
        # in dollars/local currency rather than a localised version.
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response


# =============================================================================
# Price extraction
# =============================================================================

# Regex matches the first price-looking number in arbitrary text. It accepts:
#   - optional leading $ (with optional whitespace)
#   - integer or decimal numbers
#   - thousands separators with commas (1,234.56)
# We intentionally keep the dollar sign optional because the selector might
# point at a <span> that contains "47.50" with the "$" in a sibling element.
_PRICE_REGEX = re.compile(
    r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
)


def _extract_price(text: str) -> Optional[float]:
    """
    Pull the first price-looking number out of a text snippet.

    Returns None if no number is found or the match cannot be coerced.
    Removes thousands separators before float conversion to avoid
    "could not convert string to float: '1,234.56'" errors.
    """
    if not text:
        return None
    match = _PRICE_REGEX.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        # Belt-and-suspenders: the regex should guarantee a parseable number,
        # but log just in case the regex is ever loosened.
        logger.warning("Regex matched '%s' but float conversion failed.", raw)
        return None


# =============================================================================
# url_price checker
# =============================================================================

def check_url_price(rule: Rule) -> List[Alert]:
    """
    Fetch the page, extract the price using the CSS selector, and alert
    if the price is at or below the threshold.

    Returns:
        A list of 0 or 1 Alert objects. (We return a list rather than
        Optional[Alert] to match the rss_keyword checker's signature,
        making the main loop simpler.)
    """
    # Threshold is required for this rule type; without it we can't decide
    # whether to alert.
    if rule.threshold_price is None:
        logger.warning("Rule '%s' has no threshold_price; skipping.", rule.name)
        return []

    # --- Fetch ---------------------------------------------------------------
    try:
        response = _http_get(rule.url)
    except requests.RequestException as exc:
        logger.error(
            "Failed to fetch %s for rule '%s': %s",
            rule.url, rule.name, exc,
        )
        return []

    # --- Parse ---------------------------------------------------------------
    # html.parser is built-in (no extra dependency) and handles malformed
    # HTML gracefully. lxml would be faster but requires a system library.
    soup = BeautifulSoup(response.text, "html.parser")

    element = soup.select_one(rule.selector_or_keywords)
    if element is None:
        logger.warning(
            "Selector '%s' matched nothing on %s (rule '%s').",
            rule.selector_or_keywords, rule.url, rule.name,
        )
        return []

    # get_text() returns the concatenated visible text with tags stripped.
    # We then extract the first number, which handles cases like
    # '<span>$<sup>47</sup>.<sub>50</sub></span>' that simpler approaches miss.
    text = element.get_text(separator=" ", strip=True)
    price = _extract_price(text)

    if price is None:
        logger.warning(
            "Could not parse a price from text '%s' (rule '%s').",
            text[:80], rule.name,
        )
        return []

    # Use %.2f formatting so log lines are stable and don't show
    # floating-point cruft like '47.500000000000004'.
    logger.info(
        "Rule '%s': observed $%.2f (threshold $%.2f).",
        rule.name, price, rule.threshold_price,
    )

    # The actual decision: at-or-below the threshold -> alert.
    if price <= rule.threshold_price:
        # Build the message with explicit format specifiers; never let
        # Python decide how to render a float in user-visible text.
        message = (
            f"${price:.2f} is at or below your ${rule.threshold_price:.2f} "
            f"threshold."
        )
        if rule.notes:
            message = f"{message}\n\n{rule.notes}"
        return [Alert(
            rule_name=rule.name,
            title=f"Deal: {rule.name}",
            message=message,
            url=rule.url,
            current_price=price,
        )]

    return []


# =============================================================================
# rss_keyword checker
# =============================================================================

def _local_tag(element: ET.Element) -> str:
    """
    Return the local (un-namespaced) tag name of an XML element.

    RSS uses bare tags like 'title'; Atom uses '{http://www.w3.org/2005/Atom}title'.
    Using the local name lets us treat both feed types uniformly.
    """
    tag = element.tag
    # rsplit on '}' returns the original string when no '}' is present, so
    # this is safe for non-namespaced elements too.
    return tag.rsplit("}", 1)[-1]


def _first_child_text(item: ET.Element, tag_names: List[str]) -> Optional[str]:
    """
    Return the text of the first direct child whose local tag name matches
    any of `tag_names`.

    Special case: Atom <link> elements carry the URL in the 'href' attribute
    rather than as text content. We fall back to the attribute when text
    is empty.
    """
    for child in item:
        local = _local_tag(child)
        if local not in tag_names:
            continue

        text = (child.text or "").strip()
        if text:
            return text

        # Atom <link href="..." /> case.
        if local == "link":
            href = child.attrib.get("href", "").strip()
            if href:
                return href
        # If we matched the tag but found no text and no href, keep scanning
        # in case there's a duplicate tag with content (rare but possible).
    return None


def check_rss_keyword(
    rule: Rule,
    previously_seen: Set[str],
) -> Tuple[List[Alert], Set[str]]:
    """
    Parse an RSS or Atom feed and alert on items whose titles match any
    of the rule's keywords.

    Args:
        rule: The rule to evaluate.
        previously_seen: GUIDs of items we've already alerted on.

    Returns:
        A tuple of (alerts, current_ids). current_ids is the set of GUIDs
        currently visible in the feed -- the caller persists this so on
        the next run we know which items are new.
    """
    # Empty result for the "no keywords" case -- caller should have caught
    # this at config time, but be defensive.
    keywords = [
        kw.strip().lower()
        for kw in rule.selector_or_keywords.split(",")
        if kw.strip()
    ]
    if not keywords:
        logger.warning("Rule '%s' has no keywords; skipping.", rule.name)
        return [], set()

    # --- Fetch ---------------------------------------------------------------
    try:
        response = _http_get(rule.url)
    except requests.RequestException as exc:
        logger.error(
            "Failed to fetch %s for rule '%s': %s",
            rule.url, rule.name, exc,
        )
        # Returning an empty current_ids would cause the caller to "forget"
        # everything it had previously seen, which would re-alert on the
        # next successful run. Returning the prior set preserves continuity.
        return [], previously_seen

    # --- Parse ---------------------------------------------------------------
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        logger.error("Could not parse XML for rule '%s': %s", rule.name, exc)
        return [], previously_seen

    # Find feed items. RSS uses <item>, Atom uses <entry>. The find()
    # call below uses '*' for the namespace to match either.
    items: List[ET.Element] = []
    for el in root.iter():
        local = _local_tag(el)
        if local in ("item", "entry"):
            items.append(el)

    if not items:
        logger.warning("No items/entries found in feed for rule '%s'.", rule.name)
        return [], previously_seen

    alerts: List[Alert] = []
    current_ids: Set[str] = set()

    for item in items:
        title = _first_child_text(item, ["title"])
        link = _first_child_text(item, ["link"]) or rule.url
        # GUID/ID is the canonical de-dup key; fall back to link, then title,
        # to give us *some* dedupe ability for malformed feeds.
        guid = _first_child_text(item, ["guid", "id"]) or link or title

        # Skip items missing a title or any kind of identifier; we can't
        # do anything useful with them.
        if not title or not guid:
            continue

        current_ids.add(guid)

        # If we've already alerted on this item in a previous run, skip.
        if guid in previously_seen:
            continue

        # Case-insensitive substring match. We log the matched keyword so
        # the user can see *why* an alert fired, which helps tuning.
        title_lower = title.lower()
        matched_kw = next((kw for kw in keywords if kw in title_lower), None)
        if matched_kw is None:
            continue

        logger.info(
            "Rule '%s': matched keyword '%s' in title '%s'.",
            rule.name, matched_kw, title[:80],
        )

        alerts.append(Alert(
            rule_name=rule.name,
            title=f"Deal: {rule.name}",
            # Prefix the matched keyword in brackets so the user can scan
            # the notification and see what triggered it at a glance.
            message=f"[{matched_kw}] {title}",
            url=link,
            current_price=None,
        ))

    return alerts, current_ids
