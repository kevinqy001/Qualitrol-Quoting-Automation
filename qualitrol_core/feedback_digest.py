"""Read-only aggregation of user feedback into a Markdown digest.

Reads the per-case feedback files and the append-only global logs under
``OUTPUT_DIR/_feedback`` and renders a human-readable report an engineer can use
to tune ``Step 1 _ Extract Info/extraction_rules.md`` and the BOQ logic.

This is step 1 of the feedback -> optimization loop: *surface* the signal. It is
strictly READ-ONLY — it never mutates any feedback file, case data, or log.
Turning these signals into rule edits stays a deliberate, human-reviewed action
(rules-first, offline-safe: this module needs no LLM and no network).

Feedback sources (see webapp/server.py for the writers):
  * per-case  outputs/<id>/spec_feedback.json          (spec review 👍/👎 + comment)
  * per-case  outputs/<id>/requirements_feedback.json  (BOQ line 👍/👎 + comment)
  * per-case  outputs/<id>/feedback.json               (overall BOQ 👍/👎 + comment)
  * global    outputs/_feedback/boq_regeneration_log.jsonl  (LLM re-pick decisions)
  * global    outputs/_feedback/*_log.jsonl                 (append-only volume)
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

_GLOBAL_LOGS = (
    "spec_feedback_log.jsonl",
    "requirements_feedback_log.jsonl",
    "feedback_log.jsonl",
    "boq_regeneration_log.jsonl",
)


def _feedback_dir(output_dir: Optional[Path] = None) -> Path:
    base = Path(output_dir) if output_dir else config.OUTPUT_DIR
    return base / "_feedback"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def collect(output_dir: Optional[Path] = None) -> dict:
    """Aggregate every feedback signal into a plain (JSON-serialisable) dict.

    Merges the append-only global logs (history — captures downvotes that were
    later *cleared* after a BOQ regeneration) with the per-case files (current
    UI state, authoritative for keys still present). ``current overrides
    history`` per key, so a rating the user changed reflects its latest value
    while corrections consumed by a regeneration are still surfaced.
    """
    base = Path(output_dir) if output_dir else config.OUTPUT_DIR
    fdir = _feedback_dir(output_dir)

    # (caseId, key) -> merged latest state. Seed from history, overlay current.
    line_map: dict[tuple, dict] = {}
    spec_map: dict[tuple, dict] = {}
    overall_map: dict[str, dict] = {}

    for row in _read_jsonl(fdir / "requirements_feedback_log.jsonl"):
        cid = str(row.get("caseId", ""))
        rid = str(row.get("requirement_id") or row.get("requirementId") or "")
        line_map[(cid, rid)] = {
            "caseId": cid, "requirement": (row.get("requirement") or "").strip(),
            "scenarioId": (row.get("scenario_id") or "").strip(),
            "feedback": (row.get("feedback") or "").strip(),
            "comments": (row.get("comments") or "").strip(),
        }
    for row in _read_jsonl(fdir / "spec_feedback_log.jsonl"):
        cid = str(row.get("caseId", "")); rid = str(row.get("id") or "")
        spec_map[(cid, rid)] = {
            "caseId": cid, "location": (row.get("location") or "").strip(),
            "feedback": (row.get("feedback") or "").strip(),
            "comments": (row.get("comments") or "").strip(),
        }
    for row in _read_jsonl(fdir / "feedback_log.jsonl"):
        cid = str(row.get("caseId", ""))
        overall_map[cid] = {
            "caseId": cid, "feedback": (row.get("overallFeedback") or "").strip(),
            "comments": (row.get("comments") or "").strip(),
        }

    case_ids: set[str] = set()
    if base.exists():
        for case_dir in sorted(
            p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")
        ):
            cid = case_dir.name
            reqs = _read_json(case_dir / "requirements_feedback.json").get("items", {}) or {}
            spec = _read_json(case_dir / "spec_feedback.json").get("items", {}) or {}
            overall = _read_json(case_dir / "feedback.json")
            for rid, it in (reqs.items() if isinstance(reqs, dict) else []):
                line_map[(cid, str(rid))] = {
                    "caseId": cid, "requirement": (it.get("requirement") or "").strip(),
                    "scenarioId": (it.get("scenario_id") or "").strip(),
                    "feedback": (it.get("feedback") or "").strip(),
                    "comments": (it.get("comments") or "").strip(),
                }
            for rid, it in (spec.items() if isinstance(spec, dict) else []):
                spec_map[(cid, str(rid))] = {
                    "caseId": cid, "location": (it.get("location") or "").strip(),
                    "feedback": (it.get("feedback") or "").strip(),
                    "comments": (it.get("comments") or "").strip(),
                }
            if overall:
                overall_map[cid] = {
                    "caseId": cid,
                    "feedback": (overall.get("overallFeedback") or "").strip(),
                    "comments": (overall.get("comments") or "").strip(),
                }
            case_ids.add(cid)

    spec_neg = [v for v in spec_map.values() if v["feedback"] == "Negative"]
    line_neg = [v for v in line_map.values() if v["feedback"] == "Negative"]
    overall_neg = [v for v in overall_map.values() if v["feedback"] == "Negative"]
    spec_pos = sum(1 for v in spec_map.values() if v["feedback"] == "Positive")
    spec_negc = len(spec_neg)
    line_pos = sum(1 for v in line_map.values() if v["feedback"] == "Positive")
    line_negc = len(line_neg)
    ov_pos = sum(1 for v in overall_map.values() if v["feedback"] == "Positive")
    ov_neg = len(overall_neg)
    by_scenario_neg = Counter(v["scenarioId"] for v in line_neg if v["scenarioId"])
    by_requirement_neg = Counter(v["requirement"] for v in line_neg if v["requirement"])

    # Per-case rollup across the merged maps.
    case_ids |= {k[0] for k in spec_map} | {k[0] for k in line_map} | set(overall_map)
    cases: list[dict] = []
    for cid in sorted(case_ids):
        sp = sum(1 for (c, _), v in spec_map.items() if c == cid and v["feedback"] == "Positive")
        sn = sum(1 for (c, _), v in spec_map.items() if c == cid and v["feedback"] == "Negative")
        lp = sum(1 for (c, _), v in line_map.items() if c == cid and v["feedback"] == "Positive")
        ln = sum(1 for (c, _), v in line_map.items() if c == cid and v["feedback"] == "Negative")
        ov = (overall_map.get(cid, {}) or {}).get("feedback", "")
        if sp or sn or lp or ln or ov:
            cases.append({"caseId": cid, "specPos": sp, "specNeg": sn,
                          "linePos": lp, "lineNeg": ln, "overall": ov})

    # BOQ regeneration decisions (the LLM's per-line re-picks) — strong signal.
    actions: Counter = Counter()
    replacements: Counter = Counter()
    adjustments: list[dict] = []
    removals: list[dict] = []
    regen_rows = _read_jsonl(fdir / "boq_regeneration_log.jsonl")
    for row in regen_rows:
        for ch in row.get("changes") or []:
            action = (ch.get("action") or "").strip()
            if action:
                actions[action] += 1
            before = ch.get("before") or {}
            after = ch.get("after") or {}
            if action == "replace":
                replacements[
                    f"{before.get('product_model', '?')} -> {after.get('product_model', '?')}"
                ] += 1
            elif action == "adjust":
                adjustments.append({"caseId": row.get("caseId", ""),
                                    "before": before, "after": after,
                                    "rationale": (ch.get("rationale") or "").strip()})
            elif action == "remove":
                removals.append({"caseId": row.get("caseId", ""), "before": before,
                                 "rationale": (ch.get("rationale") or "").strip()})

    log_volumes = {name: len(_read_jsonl(fdir / name)) for name in _GLOBAL_LOGS}

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outputDir": str(base),
        "spec": {"positive": spec_pos, "negative": spec_negc, "negatives": spec_neg},
        "boqLines": {"positive": line_pos, "negative": line_negc, "negatives": line_neg},
        "boqOverall": {"positive": ov_pos, "negative": ov_neg, "negatives": overall_neg},
        "byScenarioNeg": dict(by_scenario_neg.most_common()),
        "byRequirementNeg": dict(by_requirement_neg.most_common(20)),
        "regen": {
            "total": len(regen_rows),
            "actions": dict(actions),
            "replacements": dict(replacements.most_common(20)),
            "adjustments": adjustments,
            "removals": removals,
        },
        "cases": cases,
        "logVolumes": log_volumes,
    }


def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(stats: dict) -> str:
    L: list[str] = []
    a = L.append

    a("# Feedback digest")
    a("")
    a(f"_Generated {stats['generatedAt']} · source `{stats['outputDir']}`_")
    a("")
    a("> Read-only aggregation of user feedback, to guide tuning of "
      "`Step 1 _ Extract Info/extraction_rules.md` and the BOQ logic. Comments may "
      "contain **customer-specific** text — distil any resulting rules to generic, "
      "non-identifying guidance before committing.")
    a("")

    # 1. Totals
    a("## 1. Totals")
    a("")
    a("| Channel | 👍 | 👎 |")
    a("|---|---:|---:|")
    a(f"| Spec review items | {stats['spec']['positive']} | {stats['spec']['negative']} |")
    a(f"| BOQ lines | {stats['boqLines']['positive']} | {stats['boqLines']['negative']} |")
    a(f"| BOQ overall | {stats['boqOverall']['positive']} | {stats['boqOverall']['negative']} |")
    a("")
    a(f"Cases with feedback: **{len(stats['cases'])}** · "
      f"BOQ regenerations logged: **{stats['regen']['total']}**")
    a("")

    # 2. Negative BOQ lines by scenario / requirement
    a("## 2. Where the BOQ is wrong most often")
    a("")
    by_scen = stats.get("byScenarioNeg") or {}
    if by_scen:
        a("**Negative BOQ lines by scenario** (rules/catalog candidates worth reviewing):")
        a("")
        a("| Scenario ID | 👎 lines |")
        a("|---|---:|")
        for sid, n in list(by_scen.items())[:15]:
            a(f"| {_md_escape(sid)} | {n} |")
        a("")
    by_req = stats.get("byRequirementNeg") or {}
    if by_req:
        a("**Most-downvoted BOQ line items:**")
        a("")
        for req, n in list(by_req.items())[:15]:
            a(f"- ({n}×) {_md_escape(req)}")
        a("")
    if not by_scen and not by_req:
        a("_No negative BOQ-line feedback recorded yet._")
        a("")

    # 3. BOQ regeneration patterns (the LLM's corrections = ground-truth signal)
    a("## 3. BOQ re-pick patterns (from user-triggered regenerations)")
    a("")
    regen = stats["regen"]
    if regen["total"]:
        acts = regen["actions"]
        a("Actions applied: " + ", ".join(f"**{k}** ×{v}" for k, v in acts.items()) + ".")
        a("")
        if regen["replacements"]:
            a("**Top product replacements** (wrong → chosen):")
            a("")
            for swap, n in regen["replacements"].items():
                a(f"- ({n}×) {_md_escape(swap)}")
            a("")
        if regen["adjustments"]:
            a("**Quantity adjustments:**")
            a("")
            for adj in regen["adjustments"][:15]:
                b = adj["before"].get("quantity"); af = adj["after"].get("quantity")
                a(f"- `{_md_escape(adj['caseId'])}` {b} → {af} — {_md_escape(adj['rationale'])}")
            a("")
        if regen["removals"]:
            a("**Removed lines:**")
            a("")
            for rm in regen["removals"][:15]:
                a(f"- `{_md_escape(rm['caseId'])}` {_md_escape(str(rm['before'].get('product_model', '')))} — {_md_escape(rm['rationale'])}")
            a("")
    else:
        a("_No BOQ regenerations recorded yet._")
        a("")

    # 4. Negative comments — the raw material for new rules
    a("## 4. Negative comments (candidate rules)")
    a("")
    line_comments = [c for c in stats["boqLines"]["negatives"] if c.get("comments")]
    spec_comments = [c for c in stats["spec"]["negatives"] if c.get("comments")]
    over_comments = [c for c in stats["boqOverall"]["negatives"] if c.get("comments")]
    if line_comments:
        a("**BOQ line comments:**")
        a("")
        for c in line_comments[:40]:
            ctx = c.get("requirement") or c.get("scenarioId") or ""
            a(f"- `{_md_escape(c['caseId'])}` [{_md_escape(ctx)}] {_md_escape(c['comments'])}")
        a("")
    if spec_comments:
        a("**Spec review comments:**")
        a("")
        for c in spec_comments[:40]:
            a(f"- `{_md_escape(c['caseId'])}` [{_md_escape(c.get('location', ''))}] {_md_escape(c['comments'])}")
        a("")
    if over_comments:
        a("**Overall BOQ comments:**")
        a("")
        for c in over_comments[:40]:
            a(f"- `{_md_escape(c['caseId'])}` {_md_escape(c['comments'])}")
        a("")
    if not (line_comments or spec_comments or over_comments):
        a("_No free-text negative comments recorded yet._")
        a("")

    # 5. Per-case table
    a("## 5. Per-case summary")
    a("")
    if stats["cases"]:
        a("| Case | Spec 👍/👎 | Line 👍/👎 | Overall |")
        a("|---|---|---|---|")
        for c in stats["cases"]:
            a(f"| {_md_escape(c['caseId'])} | {c['specPos']}/{c['specNeg']} | "
              f"{c['linePos']}/{c['lineNeg']} | {c['overall'] or '—'} |")
        a("")
    else:
        a("_No per-case feedback found._")
        a("")

    # 6. Log volumes
    a("## 6. Global log volumes (append-only)")
    a("")
    for name, n in stats["logVolumes"].items():
        a(f"- `{name}`: {n} entries")
    a("")

    return "\n".join(L)


def build_digest(output_dir: Optional[Path] = None) -> tuple[dict, str]:
    """Return ``(stats, markdown)`` for the current feedback corpus."""
    stats = collect(output_dir)
    return stats, render_markdown(stats)


def write_digest(markdown: str, output_dir: Optional[Path] = None) -> Path:
    """Write the Markdown digest to ``OUTPUT_DIR/_feedback/feedback_digest.md``."""
    fdir = _feedback_dir(output_dir)
    fdir.mkdir(parents=True, exist_ok=True)
    out = fdir / "feedback_digest.md"
    out.write_text(markdown, encoding="utf-8")
    return out
