# Spec: Durable File Persistence for Demo Deployment

## Background

The app is FastAPI storing all state as files under `outputs/`
(`qualitrol_core/config.py:56`). **The absence of a database is by design and
is the right long-term choice** — the artifacts are naturally file-shaped
(uploads, JSON results, generated `.docx`), and plain files keep the system
simple, portable, and easy to inspect. This spec is not a step toward adding a
database; it makes the current file design robust enough for the upcoming demo
deployment while keeping the design easy to operate and debug.

### Hosting context

The app is deployed to a shared Azure App Service environment: **multiple users
share one instance, and the platform runs multiple application processes**
(gunicorn `--workers 2` today, plus restarts and redeploys). That means the
same files can be read and written by different worker processes at the same
time, and in-process memory is not shared between them or preserved across a
restart. The file design was written as if there were a single process on local
disk; the shared, multi-process host is what turns its soft spots into real
failures — which is what the Problem section enumerates.

The demo bar this spec must clear: completed uploads, job results, generated
documents, and feedback survive redeploys and remain readable with **more than
one worker**. Data may be kept forever for now.

### One identifier: `project_id`

There is exactly one project identifier, generated once when analysis starts:

```text
project_id = WEB-<16 hex chars>   (webapp/server.py:546)
```

**Widen the ID before implementation.** Today it is `uuid.uuid4().hex[:8]`
(8 hex chars); change it to `uuid.uuid4().hex[:16]` minimum. This is a one-line
change and matters more now that the URL is the access handle — a wider ID
resists guessing/enumeration of other users' projects. It is also the on-disk
directory name (`OUTPUT_DIR/<project_id>/`). The codebase
currently exposes the same value under several alias names — `jobId`, `caseId`,
`caseReference` — and endpoint path params named `job_id` / `case_id`. **These
are all the same string.** This spec uses `project_id` throughout; the aliases
are not distinct concepts and should not be treated as such. New code and the
project URL use `project_id`; existing alias keys can stay for compatibility but
carry no separate meaning.

That server-generated ID is the user's project handle. After the ingest
endpoint returns it, the frontend updates the URL to:

```text
/?project=<project_id>
```

That URL is the resume/share/bookmark mechanism. The server reconstructs the
project from files under `OUTPUT_DIR/<project_id>/`, not from browser
`localStorage`.

## Problem

Four issues block the demo deployment:

1. **`outputs/` lives inside the deployed code dir.** On Azure App Service it
   sits under `/home/site/wwwroot`, commingled with code; survival across
   deploys is incidental, not guaranteed.
2. **`_generated_docs` is an in-memory dict** (`webapp/server.py:1116`). A doc
   generated on worker A is invisible to a download routed to worker B -> 404
   even though the file exists. Also emptied on every restart.
3. **Naive `write_text` for the cross-worker poll files** `_job.json` and
   `_result.json` (`webapp/server.py:478-484`). Any worker may serve a poll, so
   a partial write can be read as a half-written file.
4. **Project restore is browser-local.** The UI stores completed analyses in
   `localStorage`; refresh/share/bookmark is not tied to server state. A user
   with the project URL should be able to resume from disk without browser
   history.

## Spine

```text
upload -> per-project dir keyed by project_id -> poll files written atomically ->
project URL carries project_id -> any worker reads by deterministic path ->
durable on Azure /home
```

Every project owns an isolated subtree on `/home`. The cross-worker poll files
are written atomically so a poll never sees a half-written file. Any worker
locates a project view, job result, or generated doc by path alone — nothing a
project-load, poll, or download endpoint needs lives only in process memory.

## Invariants

Named once here; every fix inherits them.

- **Files are the store.** The file-based design is intentional and long-term;
  fixes stay within it. A database is out of scope, not a pending upgrade.
- **No cross-process locks.** `/home` is Azure Files (SMB); advisory locks
  (`flock`) are not reliable over it. (SQLite would hit the same wall:
  concurrent writes over a network share are unsafe.) For this demo, avoid
  locks and lean on project isolation, one-editor-per-project, and atomic
  publish.
- **Last writer wins within a project.** This is acceptable for the demo because
  only one user edits a project at a time. We are not solving collaborative
  editing or simultaneous regeneration of the same project.
- **Atomic publish.** A file is either its old contents or its new contents,
  never partial. Achieved with a unique temp file in the same directory plus
  `os.replace`.
- **No critical state in process memory.** Any worker reconstructs everything
  from disk.

## Fixes

Lead with the decision; rationale only where it prevents a mistake.

### 1. Configurable data dir on `/home`
`OUTPUT_DIR` reads from env `QUALITROL_DATA_DIR`, defaulting to today's
`REPO_ROOT / "outputs"` for local dev. Set `QUALITROL_DATA_DIR=/home/data` in
Azure App Settings. `/home` is a plan-wide persistent share, so this is durable
with zero infra work. *Addresses problem 1.*

### 2. Deterministic doc paths, delete the dict
Remove `_generated_docs`. The doc already lands on disk; make its filename
deterministic instead of indexing the path in memory:

```text
OUTPUT_DIR/_docgen/Qualitrol_Quotation_<doc_id>.docx
```

The download endpoint validates `doc_id` against a strict pattern (for example
`DOC-[A-F0-9]{6,32}`), derives the path from that ID, and returns the file if it
exists. Generated docs are write-once, read-only after publish.
*Addresses problem 2.*

### 3. Atomic writes for the poll files
`_job.json` and `_result.json` are the cross-worker poll files — the JSON most
important to protect from partial reads, since any worker may serve a poll.
Write them atomically: to a unique temp file in the same directory, then
`os.replace` onto the final path. Unique temp names, not a fixed `<name>.tmp`,
so two processes publishing the same target cannot collide. Add a generic helper

```text
io_utils.write_json_atomic(path, payload)
```

that writes arbitrary JSON (do not overfit it to job files), then call it from
the `_job.json` and `_result.json` writes. Keeping it general means the Deferred
artifact-atomic work reuses the same helper. *Addresses problem 3.*

### 4. Stale processing jobs
The background analysis task is still in-process. If a worker restarts while a
job is running, the job cannot be resumed by another worker. That is acceptable
for the demo only if the UI fails clearly instead of polling forever.

If `_job.json` says `processing` and `startedAt` is older than a fixed threshold
(for example 30 minutes), the poll endpoint should return an error/stale status
telling the user to rerun the analysis. Completed results remain durable;
in-flight work is not guaranteed through worker restart in this demo plan.

### 5. Server-backed project resume by URL
Add a server endpoint that reconstructs the current project state from disk:

```text
GET /api/v1/projects/<project_id>
```

The endpoint validates `project_id`, then reads deterministic files under
`OUTPUT_DIR/<project_id>/`. **No new resume marker is needed: `_job.json` is
already the authoritative state marker** (written synchronously before the
ingest handler returns), and `_result.json` is a complete snapshot of the
frontend payload. Resume is a switch on `_job.json`:

- missing → 404 (unknown project);
- `done` → return `_result.json` (the completed ingest payload);
- `processing`, `startedAt` fresh → a processing response carrying the ID under
  both canonical and compatibility keys so existing polling code keeps working:
  `{status: "processing", projectId, caseId, jobId}` (all the same value). The
  frontend resumes polling `/api/v1/ingest/result/<project_id>`;
- `processing`, `startedAt` past the stale threshold (fix 4) → stale response,
  user reruns;
- `error` → the recorded error response.

A run that died between writing `step2_create_boq.json` and `_result.json` is
just a crashed in-flight job: `_job.json` still says `processing`, so the stale
branch covers it. We do not rebuild a partial payload from step1/step2 —
salvaging in-flight work is out of scope (see Deferred).

On the frontend:

- after ingest starts and the backend returns the project ID, update the URL to
  `/?project=<project_id>` using `history.replaceState` or `history.pushState`;
- on page load, if `project` is present, call `/api/v1/projects/<project_id>`
  before loading sample data or showing any welcome/history UI;
- if the project loads, render it from the server response, not `localStorage`;
- if it is still processing, show the existing progress state and resume
  polling;
- if there is no `project` query param, show the existing upload workflow as a
  fresh analysis.

Scope for this fix: make URL restore the source of truth and stop relying on
`localStorage` for resume. The history UI does not have to be deleted now — it
just stops being a restore path. Full removal of the local-history flow is a
follow-up. No URL means the user reruns.

## Scope

- **Now:** fixes 1–5 above. Demo-safe with multiple workers, durable completed
  artifacts on `/home`, project URLs for restore, and no process-memory
  dependency for project-load/download/poll paths.
- **Gates:**
  - `project_id` is `hex[:16]` or wider.
  - `_job.json` and `_result.json` are written atomically via unique
    same-directory temp file + `os.replace`; no `Path.write_text` remains for
    either.
  - No module-level dict/set is the source of truth for anything a download or
    poll/project-load endpoint returns.
  - Doc download path is derived from validated `doc_id`, not `_generated_docs`.
  - Loading `/?project=<project_id>` reconstructs completed projects from disk
    without `localStorage`.
  - Two-worker run: a doc generated via one request downloads successfully from
    a later request after clearing process memory or restarting a worker.

## Non-goals

Not part of this design. Each is a conscious exclusion, acceptable under the
demo assumptions, not an oversight.

- **A database, distributed locking, or a job queue.** Files are the store;
  correctness comes from project isolation, one-editor-per-project, and atomic
  writes — not locks.
- **Atomic publish beyond the poll files.** BOQ/margin Excel and quotation Word
  are not hardened against torn reads; that would need a concurrent regenerate
  of the *same* project, which the one-editor assumption makes unlikely.
- **Contention-safe shared feedback logs.** Concurrent appends to
  `feedback_log.jsonl` etc. may interleave; acceptable because the UI never
  reads these global logs back.
- **In-flight job recovery.** A job that dies on worker restart is not resumed;
  it fails clearly (fix 4) and the user reruns.
- **Collaborative editing / concurrent regeneration of one project.** One user
  edits a project at a time.
- **Project listing or a searchable index.** The project URL is the only handle;
  no URL means no in-app discovery.
- **Horizontal scale beyond a single App Service plan, and any data
  retention/cleanup policy.** Data may be kept forever for now.

## Done

- `QUALITROL_DATA_DIR=/home/data` set in Azure; artifacts appear there and
  survive a redeploy.
- Doc download succeeds with `--workers 2` after a worker restart.
- Concurrent poll during a job write never returns a partial/invalid JSON.
- A stale in-flight job returns a clear stale/error status instead of polling
  forever.
- A user can bookmark/share `/?project=<project_id>` and reopen a completed
  project from server files after browser storage is cleared.

## Files likely touched

- `qualitrol_core/config.py` — env-driven `OUTPUT_DIR`.
- `qualitrol_core/io_utils.py` — atomic write helper for the poll files.
- `webapp/server.py` — widen `project_id` to `hex[:16]`, atomic
  `_job.json`/`_result.json` writes, remove `_generated_docs`, deterministic doc
  downloads, stale-job detection, `/api/v1/projects/<project_id>`.
- `webapp/static/app.js` — project URL handling, server-backed project resume,
  stop using `localStorage` for resume (full history-UI removal is a follow-up).
- `webapp/docgen.py` — deterministic doc filenames.
- Azure App Settings — `QUALITROL_DATA_DIR`.
