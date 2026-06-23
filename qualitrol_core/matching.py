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


def snippet(text: str, idx: int, width: int = 90) -> str:
    """Return a readable snippet of ``text`` centered on ``idx``."""
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


def find_class_a(text_lower: str) -> bool:
    return bool(re.search(r"class\s*a\b", text_lower))


def count_occurrences(text_lower: str, term: str) -> int:
    term = term.strip().lower()
    if not term:
        return 0
    if _is_short_alnum(term):
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text_lower))
    return text_lower.count(term)
