# Step 1 — Operator Extraction Rules

These rules are injected verbatim into the Step 1 LLM prompts (scenario
refinement + requirement extraction). Edit freely to tune precision WITHOUT
changing code. Keep each rule short and imperative. Leave this file empty to
disable. Rules must refine precision only — they may NOT invent scenarios,
metrics, or values the evidence does not support; the controlled data package
always wins on conflict.

Override per run with the env var `QUALITROL_STEP1_RULES_FILE`.

## Scenario disambiguation

- Treat generic instrumentation words — "relay", "alarm", "output", "sensor",
  "monitor", "ethernet", "display" — as WEAK. Do not put a scenario in scope on
  such a word alone; require an asset- or function-specific term as well.
- `TR_AUX_001` (Transformer auxiliary protection and indication) is in scope
  ONLY when there is explicit evidence of an auxiliary protection/accessory
  device — Buchholz / sudden-pressure / pressure-relief device, oil-level or
  oil-flow indicator, or dedicated alarm/trip CONTACTS. A temperature monitor's
  "relay alarm outputs" is an OUTPUT of `TR_TEMP_001`, not `TR_AUX_001`.
- `DRY_TR_TEMP_001` applies only to dry-type / cast-resin transformers. If the
  text says "top oil" / "oil temperature", the asset is oil-filled — drop it.
- `MTR_TEMP_001` / `MTR_PD_001` require an actual motor as the asset. Winding or
  bearing temperature on a transformer must NOT map to a motor scenario.
- `SUB_SOFT_001` requires evidence of a dedicated monitoring/asset-management
  SOFTWARE PLATFORM (dashboards, historian, fleet view). Protocol support
  (Modbus / DNP3 / IEC 61850) alone is communication scope, not software.
- Communication protocols belong to `COMM_SCADA_001`; do not let them
  corroborate unrelated hardware scenarios.
- SCOPE-EXCLUSION LANGUAGE wins: if the text says an item is "not part of the
  scope", "out of scope", "optional", "future", "provision", a "capability to
  expand", or is supplied by others, that item is NOT in scope now. Do not put
  its scenario in scope on such evidence (e.g. a PD spec noting it "can expand
  into a Breaker Condition Monitoring system … not part of the scope of this
  description" does NOT put `BRK_HEALTH_001` in scope).
- `BRK_HEALTH_001` (circuit breaker health monitoring) requires evidence that
  the customer wants to MONITOR the breaker — trip/close-coil current, operating
  / travel time, operation counter as a monitored signal, "breaker monitor" /
  "CBM". Circuit breakers, disconnectors, earthing switches shown as GIS /
  switchgear components (the plant being monitored) do NOT imply it.
- `COMM_SCADA_001` and gateway/software families (`PF_GATEWAY`, `PF_SOFTWARE`)
  are in scope only when a SEPARATE integration deliverable is required (a
  standalone SCADA gateway, protocol converter, or asset platform). An
  "IEC 61850 output" / "SCADA output" that is a bundled feature of a PD or SF6
  monitoring system is NOT a separate communication product line.

## Requirement extraction

- Prefer the customer's explicitly stated quantity over any inferred count, and
  record the unit exactly as written (e.g. "3 units", "6 probes").
- When a count is stated per-asset ("one per transformer"), capture both the
  per-asset basis and the resulting total if the asset count is also stated.
- Do not normalise away meaningful qualifiers such as "online", "continuous",
  "fiber-optic", "Class A"; keep them in the value or evidence.
- If a parameter is named but its value is absent, omit the value (do not guess)
  so the rules engine raises it as a clarification question.
