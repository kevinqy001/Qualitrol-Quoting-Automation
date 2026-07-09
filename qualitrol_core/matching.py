"""Deterministic text-matching utilities (the rules-first engine).

These helpers back the offline extractor: locating controlled synonym terms and
scenario keywords in customer text, pulling numeric values + units near a term,
and converting mapping priority into a confidence score. They make Step 1
explainable and reproducible without an LLM; the LLM (when enabled) is layered
on top for harder, semantic cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PRIORITY_CONFIDENCE = {"high": 0.85, "medium": 0.65, "low": 0.5}

# Number optionally followed by a unit token commonly seen in specs.
_VALUE_UNIT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?(?:\s*[-/]\s*\d+(?:[.,]\d+)?)?)\s*"
    r"(?P<unit>kV|kA|MVA|MVAR|MW|kW|VAC|VDC|Hz|ms|ppm|%RH|%|pC|mV|dB|"
    r"samples/cycle|fps|bays?|channels?|panels?|breakers?|transformers?|"
    r"units?|sensors?|nos?\.?|sets?)?",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _is_short_alnum(term: str) -> bool:
    # Short tokens (PMU, DFR, H2, PD) need word-boundary matching to avoid
    # spurious substring hits; longer phrases are matched as substrings.
    return len(term) <= 5 and term.replace(" ", "").isalnum()


def find_term(text_lower: str, term: str) -> int:
    """Return the index of ``term`` in ``text_lower`` or -1 if absent."""
    term = term.strip().lower()
    if not term:
        return -1
    if _is_short_alnum(term):
        match = re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text_lower)
        return match.start() if match else -1
    return text_lower.find(term)


def snippet(text: str, idx: int, width: int = 230) -> str:
    """Return a readable snippet of ``text`` centered on ``idx``.

    ``width`` is the number of characters kept on each side of the match, so the
    default returns roughly a short paragraph of context (~460 chars) — enough
    to read the surrounding requirement rather than a single truncated phrase.
    """
    if idx < 0:
        return normalize(text)[: width * 2]
    start = max(0, idx - width)
    end = min(len(text), idx + width)
    fragment = text[start:end].replace("\n", " ")
    fragment = normalize(fragment)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def priority_to_confidence(priority: str) -> float:
    return _PRIORITY_CONFIDENCE.get((priority or "").strip().lower(), 0.6)


@dataclass
class ValueHit:
    raw: str
    number: str
    unit: str
    position: int


def find_values_near(text: str, keyword_idx: int, window: int = 60) -> list[ValueHit]:
    """Find number+unit tokens within ``window`` chars of a keyword index."""
    if keyword_idx < 0:
        return []
    start = max(0, keyword_idx - window)
    end = min(len(text), keyword_idx + window)
    region = text[start:end]
    hits: list[ValueHit] = []
    for m in _VALUE_UNIT_RE.finditer(region):
        num = m.group("num")
        if not num:
            continue
        unit = (m.group("unit") or "").strip()
        # Ignore bare single digits with no unit (too noisy) unless near keyword.
        if not unit and len(num) <= 1:
            continue
        hits.append(
            ValueHit(
                raw=normalize(m.group(0)),
                number=num,
                unit=unit,
                position=start + m.start(),
            )
        )
    return hits


_COUNT_UNIT_TOKENS = {
    "bay", "bays", "channel", "channels", "panel", "panels", "breaker",
    "breakers", "transformer", "transformers", "unit", "units", "sensor",
    "sensors", "no", "nos", "set", "sets", "count", "",
}


def alpha_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def unit_compatible(hit_unit: str, expected: set[str], is_count: bool) -> bool:
    """Whether a value's unit is acceptable for a metric's expected unit(s)."""
    hit = (hit_unit or "").lower().rstrip(".")
    if is_count:
        return hit in _COUNT_UNIT_TOKENS
    if not expected:
        return bool(hit)  # generic numeric metric: require *some* unit
    return hit in expected


# Generic, cross-scenario evidence keywords that are too weak to raise a
# scenario match on their own: they appear across many unrelated specs and, in
# isolation, generate false positives. They only count when a more specific
# term for the SAME scenario co-occurs in the same text segment. Example: a
# temperature monitor's "Relay alarm outputs" line should not, by the lone word
# "relay", trigger the transformer auxiliary-protection scenario (TR_AUX_001),
# whose real signals are Buchholz / pressure relief / oil level, etc.
AMBIGUOUS_SCENARIO_KEYWORDS = frozenset(
    {
        "relay", "alarm", "alarm contacts", "sensor", "sensors", "monitor",
        "monitoring", "ethernet", "serial", "current", "voltage", "speed",
        "integration", "protocol", "gateway", "api", "display", "output",
        "outputs", "reliability",
    }
)


def is_ambiguous_keyword(term: str) -> bool:
    return (term or "").strip().lower() in AMBIGUOUS_SCENARIO_KEYWORDS


def has_corroborating_term(
    text_lower: str, ambiguous_term: str, strong_terms
) -> bool:
    """True if a scenario-specific term (other than ``ambiguous_term``) is in
    the same segment, corroborating an otherwise-weak ambiguous keyword hit."""
    target = (ambiguous_term or "").strip().lower()
    for term in strong_terms:
        if term and term != target and find_term(text_lower, term) >= 0:
            return True
    return False


# Phrases that explicitly place a nearby term OUT of the supply/quotation
# scope. A scenario keyword that sits next to one of these is describing
# something the customer says is *not* being bought now (a possible future
# expansion, an optional capability, or another party's supply), so it must
# not raise a scenario on its own. Kept deliberately conservative — only
# unambiguous "out of scope / not supplied" language, NOT "optional"/"future"
# alone (those are handled as asset scope-status in Step 2) — so genuinely
# in-scope scenarios that merely appear near the word "future" survive.
EXCLUSION_CUES = (
    "not part of the scope",
    "not part of this scope",
    "not part of the current",
    "not part of this contract",
    "not part of this technical",
    "not part of the supply",
    "not part of scope of supply",
    "not within the scope",
    "not in the scope",
    "not in scope",
    "out of scope",
    "outside the scope",
    "outside of scope",
    "not included in the scope",
    "not included in scope",
    "excluded from the scope",
    "excluded from supply",
    "excluded from this",
    "is excluded",
    "are excluded",
    "shall be excluded",
    "additional condition monitoring system is not",
)


def in_exclusion_context(
    text: str, idx: int, behind: int = 160, ahead: int = 240
) -> bool:
    """True when the term at ``idx`` sits inside an explicit out-of-scope phrase.

    Scans ``behind`` chars before and ``ahead`` chars after the match for any
    of ``EXCLUSION_CUES``. The look-ahead is wider because specs usually state
    the exclusion AFTER naming the item ("…Breaker Condition Monitoring system.
    The additional condition monitoring system is not part of the scope of the
    current technical description."). Cues are all unambiguous "not supplied /
    out of scope" phrases, so suppressing the one evidence hit next to them is
    safe — a genuinely in-scope scenario keeps its other (non-excluded) hits.
    """
    if idx < 0:
        return False
    start = max(0, idx - behind)
    end = min(len(text), idx + ahead)
    region = text[start:end].lower()
    return any(cue in region for cue in EXCLUSION_CUES)


def find_class_a(text_lower: str) -> bool:
    return bool(re.search(r"class\s*a\b", text_lower))


def count_occurrences(text_lower: str, term: str) -> int:
    term = term.strip().lower()
    if not term:
        return 0
    if _is_short_alnum(term):
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text_lower))
    return text_lower.count(term)


# --------------------------------------------------------------------------- #
# Parameter-level value comparison (Step 2 product-parameter matching)
# --------------------------------------------------------------------------- #
_SPEC_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_UPPER_BOUND_HINTS = ("up to", "upto", "max", "maximum", "≤", "<=", "<")
_LOWER_BOUND_HINTS = ("at least", "min", "minimum", "≥", ">=", ">")


def parse_spec_numbers(text: str) -> list[float]:
    """Extract all numbers from a free-text spec value (comma-grouping aware)."""
    out: list[float] = []
    for tok in _SPEC_NUM_RE.findall(text or ""):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def spec_bounds(
    min_value: float | None, max_value: float | None, supported_value: str
) -> tuple[float | None, float | None]:
    """Best-effort numeric ``(lo, hi)`` for a product parameter.

    Uses explicit ``min_value`` / ``max_value`` when present, otherwise parses
    the free-text ``supported_value`` (e.g. "1 to 6 channels" -> (1, 6),
    "up to 35 kV" -> (None, 35), "300 to 2000 MHz" -> (300, 2000)).
    """
    lo, hi = min_value, max_value
    if lo is None and hi is None and supported_value:
        sv = supported_value.lower()
        nums = parse_spec_numbers(sv)
        if not nums:
            return None, None
        if any(k in sv for k in _UPPER_BOUND_HINTS):
            hi = max(nums)
        elif any(k in sv for k in _LOWER_BOUND_HINTS):
            lo = min(nums)
        elif len(nums) >= 2:
            lo, hi = min(nums), max(nums)
        else:
            hi = nums[0]  # a lone number reads as a capacity/ceiling
    return lo, hi


def match_parameter_value(
    req_value: str,
    min_value: float | None,
    max_value: float | None,
    supported_value: str,
) -> str:
    """Compare a required value to a product parameter's capability.

    Returns ``"pass"`` / ``"fail"`` / ``"unknown"``.

    Numeric requirements are checked against the parameter's ``[lo, hi]`` bounds
    (a demand within a capacity/range passes; out-of-range is a real ``"fail"``).
    Text requirements can only confirm a ``"pass"`` via token containment — a
    non-match returns ``"unknown"``, never ``"fail"``, because free-text specs
    use synonyms (e.g. required "SCADA" vs supported "DNP3, Modbus") and a
    literal mismatch is not a genuine conflict. ``"unknown"`` means the value is
    missing or not confidently comparable.
    """
    val = (req_value or "").strip()
    if not val:
        return "unknown"
    nums = parse_spec_numbers(val)
    is_numeric = bool(nums) and not re.search(r"[a-zA-Z]{3,}", val)
    if is_numeric:
        lo, hi = spec_bounds(min_value, max_value, supported_value)
        v = nums[0]
        if lo is not None and hi is not None:
            return "pass" if lo <= v <= hi else "fail"
        if hi is not None:
            return "pass" if v <= hi else "fail"
        if lo is not None:
            return "pass" if v >= lo else "fail"
        return "unknown"
    supported = (supported_value or "").lower()
    if not supported:
        return "unknown"
    needle = val.lower()
    if needle in supported or any(tok in supported for tok in needle.split() if len(tok) > 1):
        return "pass"
    return "unknown"
