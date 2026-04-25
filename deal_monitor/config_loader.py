"""
Config loader: reads monitoring rules from a published Google Sheet (CSV).

The sheet must be published via:
  File -> Share -> Publish to web -> Comma-separated values (.csv)

The published URL is provided to the script via the SHEET_CSV_URL
environment variable. Publishing is read-only and does not expose the
sheet's edit access -- only the data in the published tab is visible.
"""

# --- Standard library imports -------------------------------------------------
import csv
import io
import logging
from dataclasses import dataclass
from typing import List, Optional

# --- Third-party imports ------------------------------------------------------
import requests

# Module-level logger so each module identifies itself in log lines.
logger = logging.getLogger(__name__)


# =============================================================================
# Data model
# =============================================================================

@dataclass
class Rule:
    """
    One row from the rules sheet, after parsing and validation.

    Attributes:
        enabled: If False, the rule is skipped entirely. Lets the user pause
            a rule from the sheet without deleting it.
        name: Human-readable name. Also used (after slugification) as the
            stable rule_id key in state.json.
        rule_type: 'url_price' or 'rss_keyword' -- dispatched in main.py.
        url: Page URL (url_price) or RSS feed URL (rss_keyword).
        selector_or_keywords: For url_price, a CSS selector for the price
            element. For rss_keyword, a comma-separated list of keywords
            (case-insensitive substring match against the item title).
        threshold_price: For url_price only. Alert when scraped price is
            at or below this number. None means "no threshold" -> no alerts.
        cooldown_hours: Minimum hours between alerts for the same rule.
            0 disables the cooldown (always alert when conditions match).
        notes: Free text. Ignored by the script -- exists for the user.
    """
    enabled: bool
    name: str
    rule_type: str
    url: str
    selector_or_keywords: str
    threshold_price: Optional[float]
    cooldown_hours: float
    notes: str

    @property
    def rule_id(self) -> str:
        """
        Stable identifier derived from `name`. Used as the key in state.json.

        We slugify by replacing any non-alphanumeric character with an
        underscore and lower-casing. This is stable as long as the user
        doesn't rename the rule. Renaming creates a "new" rule from the
        script's point of view (its prior cooldown/seen-ID state won't
        carry over) -- this is intentional, since renames usually mean
        the user wants a fresh start anyway.
        """
        slug = "".join(c if c.isalnum() else "_" for c in self.name.lower())
        # Collapse runs of underscores and trim edges so 'A & B' doesn't
        # become 'a___b' (visually noisy in state.json).
        while "__" in slug:
            slug = slug.replace("__", "_")
        return slug.strip("_")


# =============================================================================
# Cell-level parsing helpers
# =============================================================================
#
# Every CSV cell arrives as a string. These helpers convert to the right
# Python types with explicit handling of empty/malformed values so we never
# rely on implicit coercion (e.g. bool("FALSE") is True in Python -- a
# classic foot-gun we want to avoid).

def _parse_bool(value: Optional[str]) -> bool:
    """
    Convert a CSV string cell to a boolean.

    Recognised "true" values (case-insensitive): 'true', '1', 'yes', 'y'.
    Everything else (including empty) is False. We do NOT use bool(value)
    because bool('FALSE') is True in Python, which would silently flip
    the meaning of disabled rows.
    """
    if value is None:
        return False
    cleaned = value.strip().lower()
    return cleaned in ("true", "1", "yes", "y")


def _parse_float(value: Optional[str]) -> Optional[float]:
    """
    Convert a CSV string cell to a float, or None for empty / unparseable.

    Strips currency symbols and thousands separators so '$1,234.50' becomes
    1234.5. A bad value logs a warning and returns None rather than raising,
    so one malformed row can't take down the whole run.
    """
    if value is None:
        return None
    # Strip whitespace, then dollar signs, then thousands separators.
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        logger.warning("Could not parse '%s' as a number; treating as empty.", value)
        return None


# =============================================================================
# Public entry point
# =============================================================================

def load_rules(csv_url: str) -> List[Rule]:
    """
    Fetch the published Google Sheet CSV and return a list of enabled rules.

    Disabled rows and rows missing required fields are filtered out, so the
    caller only ever sees rules that are safe to execute.

    Raises:
        requests.RequestException: if the sheet cannot be fetched. Letting
            this bubble up is intentional -- if we can't read the sheet,
            the run cannot proceed.
    """
    logger.info("Fetching rules sheet from %s", csv_url)
    response = requests.get(csv_url, timeout=30)
    response.raise_for_status()

    # csv.DictReader needs a text-mode iterable. response.text is decoded
    # using the response's declared charset, which Google Sheets serves
    # as UTF-8.
    reader = csv.DictReader(io.StringIO(response.text))

    rules: List[Rule] = []

    # enumerate starts at 2 so error messages reference the actual sheet
    # row (row 1 is the header that DictReader has already consumed).
    for row_num, row in enumerate(reader, start=2):
        try:
            # Parse cooldown once and bind to a local so we don't pay the
            # parse cost twice. Explicit None-check below preserves a 0
            # value (which legitimately means "always alert").
            cooldown_parsed = _parse_float(row.get("cooldown_hours", ""))
            cooldown_hours = cooldown_parsed if cooldown_parsed is not None else 12.0

            rule = Rule(
                enabled=_parse_bool(row.get("enabled", "")),
                name=(row.get("name") or "").strip(),
                rule_type=(row.get("type") or "").strip().lower(),
                url=(row.get("url") or "").strip(),
                selector_or_keywords=(row.get("selector_or_keywords") or "").strip(),
                threshold_price=_parse_float(row.get("threshold_price", "")),
                cooldown_hours=cooldown_hours,
                notes=(row.get("notes") or "").strip(),
            )
        except Exception as exc:
            # Catching broadly is intentional: a single bad row should
            # never abort the run. The row is logged and skipped.
            logger.error("Skipping row %d due to parse error: %s", row_num, exc)
            continue

        # --- Filter stage -----------------------------------------------------
        # Disabled rows: silent skip. The user expects them to be ignored.
        if not rule.enabled:
            continue

        # Required-field check: log a warning so the user can fix the sheet,
        # but don't crash.
        if not rule.name or not rule.url or not rule.rule_type:
            logger.warning(
                "Skipping row %d ('%s'): missing required field(s).",
                row_num, rule.name or "<unnamed>",
            )
            continue

        rules.append(rule)

    logger.info("Loaded %d enabled rule(s).", len(rules))
    return rules
