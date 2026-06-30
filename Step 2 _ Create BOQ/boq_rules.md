# Step 2 — Operator BOQ Rules

These rules are injected verbatim into the Step 2 LLM prompts (product-match
explanation + extra clarification questions). Edit freely to tune behaviour
WITHOUT changing code. Keep each rule short and imperative. Leave this file empty
to disable. Rules must refine precision/usefulness only — they may NOT invent
product capabilities, prices, or scope the evidence and data package do not
support; the controlled data package always wins on conflict.

Override per run with the env var `QUALITROL_STEP2_RULES_FILE`.

## Match recommendation style

- Lead every `recommendation` with one of: `Recommended`, `Recommended with
  validation`, or `Needs Review`, then a one-line reason.
- When product model or capability values are `TBD` in the data package, the
  `gap_or_risk` MUST say so explicitly and assign validation to the product team.
- Never present a family as quote-ready while a High-severity compatibility
  guardrail (e.g. CR_013 low confidence, CR_004/CR_005/CR_010 missing layout or
  counts) is open for its scenario.
- Sanity-check derived quantities: if sensor/channel counts per asset look
  unusually high or low (e.g. far above a typical monitor's per-unit channel
  limit), call it out as a risk to confirm — do not silently accept it.
- Do not recommend cross-family substitutes (e.g. a GIS PD monitor for a
  transformer PD need); flag the gap and suggest the correct family instead.

## Clarification questions

- Ask only what genuinely blocks finalizing the BOQ; never repeat a question
  already in the existing list (even if reworded).
- Prefer one precise, answerable question over several vague ones; cap at 4.
- Make quantity questions reference the concrete count basis (e.g. "GIS bay
  count", "sensors per bay", "transformer count") rather than generic wording.
- Always set a sensible `owner`: customer-facing scope/quantity → `Sales /
  Customer`; internal capability/model validation → `Sales / Product Engineer`.
- Set `priority` High only when the answer changes scope, quantity, or product
  selection; otherwise Medium or Low.
- Keep questions vendor-neutral and free of pricing; pricing is out of scope for
  this step.
