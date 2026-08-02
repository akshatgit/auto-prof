# Implementation Backlog

Divides `docs/DESIGN.md` into concrete, ordered implementation tasks.
Built test-first (TDD): a task isn't "done" until it has failing tests
written before the implementation, then passing after. Status is kept
current here as work lands — check this before starting new work to
avoid rebuilding something that already exists.

Backends: this build targets **Codex CLI** (`codex exec`) and **Ollama
Cloud** (HTTP API) as the pluggable AI backends behind a common
interface — not hardcoded to a single provider, so either can serve
generation or review work per job kind, and a third backend can be added
later without touching callers. This supersedes DESIGN.md §5's original
"claude -p for generation, codex exec for review" split; §4's review
pipeline can still use Codex specifically for review isolation, but
generation is backend-agnostic.

## Phase 0 — Foundations ✅ done

- [x] `docs/schema.sql` — SQLite schema (iteratively codex-reviewed, then
      extended across every later phase — see "Schema changes since
      DESIGN.md" below).
- [x] `docs/DESIGN.md` — architecture.
- [x] `autoprof/db.py` — connection helper, per-connection `PRAGMA
      foreign_keys`, schema auto-init.
- [x] `tests/` — zero-dependency `unittest`-based test infra +
      `run_tests.sh` (pytest isn't installed; stdlib only).

## Phase 1 — Modular AI Backend Layer ✅ done

- [x] `autoprof/backends/base.py` — `Backend` ABC + `BackendResult`.
- [x] `autoprof/backends/codex.py` — `CodexBackend`, shells out to `codex
      exec -o <tmpfile>`, injectable `runner`, parses rate-limit phrasing.
- [x] `autoprof/backends/ollama_cloud.py` — `OllamaCloudBackend`, HTTP
      POST via stdlib `urllib.request`, `OLLAMA_API_KEY` env var.
- [x] `autoprof/backends/registry.py` — config-driven selection
      (`autoprof.toml` + env-var overrides), precedence: per-kind env >
      per-kind config > category env > category config > hardcoded
      fallback (generation→ollama_cloud, review→codex, including
      `lab_review`).
- [x] `autoprof/create_prof.py` refactored onto the registry. Verified
      end-to-end against real `codex exec` calls.

## Phase 2 — Job Execution Core ✅ done

- [x] `autoprof/jobs.py` — `claim_job`/`complete_job`/`fail_job`
      (lease protocol + retry policy, DESIGN.md §5.1/§5.2),
      `record_rate_limit` (§5.3), `reclaim_expired_leases`.
- [x] `autoprof/artifacts.py` — `write_artifact`: temp-file + atomic
      rename, idempotent per §5.2.
- [x] `autoprof/runner.py` — `execute_job`: claim → build prompt
      (pluggable `prompt_builders`) → backend call → branch on
      rate-limited/error/success → idempotent artifact write → complete.
- [x] `autoprof/events.py` — `record_job_event` (job-sourced) +
      `record_human_event` (job_id NULL, actor_type 'human').
- [x] `autoprof/prompt_builders.py` — real prompt builders for
      `professor_decompose` and `student_work`, reading actual task/
      professor/student rows and lab_dir memory files. **Scope note:**
      these write raw model output back to memory.md but do NOT yet
      parse it into new task/paper rows — see "Explicitly deferred"
      below.

## Phase 3 — Student Lifecycle Controls ✅ done

"Any student can be manually edited, stopped, replayed" — a human
override layer that sits alongside the autonomous loop, not inside it.

- [x] `autoprof/student_ctl.py` + `autoprof/student_cli.py` →
      `autoprof student {list,show,stop,resume,edit,replay}`. Idempotent
      stop/resume via `students.paused_at` (orthogonal to `status`);
      `replay` creates a new job linked via `jobs.replayed_from_job_id`,
      leaving the original untouched.

## Phase 4 — Daemon Tick Loop ✅ done

- [x] `autoprof/daemon.py`:
  - `SingleInstanceLock` — OS `flock`, so at most one daemon runs against
    a given `autoprof.db` (§5.2 — this is what makes the lease
    protocol's guarantees real, not just theoretical).
  - `next_wake_delay` — dynamic sleep per §5.3 (min of heartbeat,
    nearest job backoff, nearest provider window reset).
  - `dispatch_pending_jobs(..., special_handlers=None)` — generic path
    via `runner.execute_job` + `prompt_builders`; a job kind present in
    `special_handlers` takes precedence (used by `lab_review`, which
    needs to parse a verdict and tally reviewers — more than one
    PromptSpec artifact write can express).
  - `run_tick` / `run_daemon` — the full loop, `once=True` for a single
    tick (used by `autoprof daemon run --once` and by tests).
- [x] `autoprof/daemon_cli.py` → `autoprof daemon run [--once] [--interval]
      [--budget] [--lab-dir] [--db-path] [--config-path] [--lock-path]`.
- [x] State-machine advancement through paper acceptance — see Phase 4.6.
- [ ] **Still not built:** professor callback (§3.3) on an accepted
      paper, nomination, defense dispatch+tally. A task reaching
      `pending_prof_review` is currently where the automated chain stops.

## Phase 4.5 — Lab Review (added mid-session, not in original DESIGN.md) ✅ done

A gate DESIGN.md didn't originally have: a newly created lab's root
problem is unvetted. `create_prof.py` now creates labs in
`pending_review`, not `active` — the daemon won't dispatch any work
against a lab until review passes.

- [x] `autoprof/lab_review.py`:
  - `request_lab_review(conn, lab_id)` — enqueues 3 independent
    `lab_review` jobs for the lab's `current_review_round` (idempotency
    guard: raises if that round was already requested).
  - `execute_lab_review_job` — parses the reviewer's `VERDICT:` line,
    writes the rationale file, inserts a `reviews` row. On the round's
    3rd review landing: tallies 2-of-3 `strong_accept` (same threshold as
    paper review) — **pass** activates the lab and auto-enqueues its
    first `professor_decompose` job in the same commit (this is what
    "review propagates downstream" means concretely); **fail** leaves the
    lab `pending_review` for revision + a fresh round.
  - `templates/lab_review_rubric.md` — **not** the same file as
    `templates/review_rubric.md`. That rubric evaluates a *completed*
    paper/defense (proof present, Related Work present) and was tried
    here first; live-tested against real `codex exec` calls, it
    systematically rejected bare problem statements for lacking a proof
    they were never supposed to have yet. The lab-specific rubric
    evaluates well-posedness / novelty / scope / tractability instead.
- [x] `autoprof/lab_cli.py` → `autoprof lab {list, review-request <id>,
      revise <id> [problem]}`.
- [x] `revise_root_problem` (added Phase 4.6): the missing half of "fail
      leaves the lab `pending_review` for revision + a fresh round" —
      nothing actually performed the revision, so a rejected lab was a
      dead end. Bumps `current_review_round` **before** enqueuing the new
      reviewer set so the new `reviews` rows validate against it, leaves
      prior rounds on the record, and refuses to rewrite the root problem
      of an `active` lab (that would silently invalidate every task
      already decomposed from the old one).
- [x] Wired into `daemon_cli.py`'s `special_handlers`.
- [x] Live-tested against real `codex exec` calls end-to-end (see
      "Live end-to-end verification" below) — including catching and
      fixing the rubric-mismatch bug above via real reviewer output, not
      simulated tests.

## Phase 4.6 — Research Lifecycle Advancement ✅ done

Closes the "biggest remaining gap" the Explicitly-deferred section named:
the chain now runs unattended from an activated lab all the way to an
accepted (or rejected) paper.

- [x] `autoprof/jsonio.py` — `extract_json_object`, shared by
      `create_prof` and `decompose`. Salvages fenced JSON and the
      "Here is the JSON:\n{...}" shape rather than failing the job.
- [x] `autoprof/decompose.py` — `professor_decompose` as a special
      handler. Asks for **structured JSON** (not prose) and creates real
      `tasks` rows + briefs, seeds one student per task, and enqueues the
      `student_work` jobs. Validation rejects rather than repairs: a bad
      `direction` or missing `end_criteria` fails the job for retry,
      since the schema CHECK-constrains both and guessing them out of
      free text is a silent-corruption source a multi-year run can't
      recover from. Capped at `MAX_TASKS_PER_DECOMPOSITION`; idempotent
      against a lab that already has tasks.
- [x] `autoprof/paper.py` — `student_work` (work the problem → memory.md
      → enqueue write-up) and `student_write_paper` (draft the ACM HTML
      paper from `templates/paper_template.html` → `papers` row →
      request review). Two job kinds, not one, so a formatting failure in
      the write-up doesn't discard the research work that preceded it.
      Respects `students.paused_at` by releasing the lease without
      burning an attempt.
- [x] `autoprof/paper_review.py` — same shape as `lab_review`: 3
      independent reviewers, 2-of-3 `strong_accept`, no partial tallying.
      Uses `templates/review_rubric.md` (the completed-document rubric).
      Pass → paper `accepted`, task → `pending_prof_review`. Fail →
      `rejected`; a fresh round is deliberately NOT auto-requested,
      because §3.3 gives the professor the revise/re-scope/abandon
      choice. `resubmit_paper` starts round N+1 when that choice is made.
- [x] All five lifecycle kinds wired into `daemon_cli.SPECIAL_HANDLERS`;
      `student_write_paper` added to the registry's generation kinds.

Bugs this phase found and fixed (all pre-existing):
- `professors.memory_path` was stored repo-root-relative
  (`lab/<lab_id>/...`) but every consumer joins it onto `lab_dir`, so the
  daemon wrote professor memory to `lab/lab/<id>/...` and the memory
  `create-prof` had seeded was never actually updated. All artifact paths
  are now uniformly lab_dir-relative (`<lab_id>/...`), including
  `lab_review`'s rationale paths.
- `build_review_prompt` had to use `str.replace`, not `str.format`: an
  ACM-style HTML paper is full of CSS braces, every one of which
  `format()` reads as a replacement field.
- `--db-path` parsed only *before* the subcommand for `lab`/`student`
  (group-level flag) but *after* it for `create-prof`/`daemon run` (leaf
  flag). Now accepted in both positions for all four.
- `CodexBackend` timeout was 280s — too short for writing or reviewing a
  full paper. Now 900s, overridable via `AUTOPROF_CODEX_TIMEOUT`, still
  inside the 1800s job lease.

## Phase 5 — CLI Surface Completion (partial)

- [x] `autoprof lab list` / `review-request`.
- [x] `autoprof status` — `autoprof/status_cli.py`, full read-only tree
      view: labs → professor → lab-review tally → tasks → assigned
      student (with a PAUSED marker) → papers → per-round paper verdicts,
      then the job queue broken down by status and kind, and any failed
      jobs with their first error line. Verdicts render as `++/+/~+/~-/-/--`
      with the strong_accept count spelled out, since that count is what
      the 2-of-3 gate actually turns on.
- [ ] `autoprof approve-lab` / `reject-lab` — for `lab_proposals` (student
      → new professor promotion, DESIGN.md §3.5). Not started; no code
      path creates `lab_proposals` rows yet since defense/graduation
      isn't built.
- [ ] `autoprof init` — thin wrapper taking an already-written problem
      statement, distinct from `create-prof`'s idea-to-soul step. Not
      started; `create-prof` covers the only bootstrap path that exists.

## Phase 6 — Web UI ✅ done (read-only core)

- [x] `autoprof/webserver.py` — stdlib `http.server` only, zero deps.
      Routes: `/` (lab list), `/labs/<id>` (tasks + lab reviews),
      `/students/<id>`, `/professors/<id>` (+ their students). All
      DB-sourced content passed through `html.escape` — verified via a
      dedicated XSS-in-root-problem test, not just assumed safe.
- [x] `autoprof/webserver_cli.py` → `autoprof web run [--host] [--port]
      [--db-path]`.
- [ ] Pending-approvals view / approve-reject write path — blocked on
      Phase 5's `lab_proposals` flow not existing yet.

## Live end-to-end verification (beyond unit tests)

Every phase above was also exercised against a real, throwaway DB with
real `codex exec` calls (not just the unit test suite), specifically:
`create-prof` → `lab review-request` → `daemon run --once` → tally →
(pass: lab activates + `professor_decompose` auto-enqueued, verified
deterministically in unit tests; fail: stays `pending_review`, verified
live against real reviewer output on two different problem ideas). This
live run is what caught two real bugs the unit tests couldn't have caught
because they mock the backend: (1) `daemon_cli.py`/`create_prof.py` CLI
wrappers hardcoded `db.LAB_DIR` instead of taking a `--lab-dir` flag,
silently writing artifacts into the wrong directory when `--db-path` was
overridden; (2) the lab-review rubric mismatch described in Phase 4.5.

## Schema changes since DESIGN.md (not yet back-filled into the prose)

`docs/schema.sql` is current; `docs/DESIGN.md`'s prose predates all of
this and hasn't been rewritten to match — tracked here so it isn't lost:
- `students.paused_at`, `jobs.replayed_from_job_id` (Phase 3).
- `events.job_id` nullable, `actor_type` gained `'human'`, with
  `CHECK (job_id IS NOT NULL OR actor_type = 'human')`.
- `jobs.review_round` / `jobs.reviewer_index` (nullable; only meaningful
  for review-kind jobs).
- `reviews.target_type` gained `'lab'`; `trg_reviews_valid_target` gained
  a lab branch (3 reviewers, round must match `labs.current_review_round`).
- `labs.status` gained `'pending_review'` (now the *initial* status, not
  `'active'`); `labs.current_review_round` added.
- `provider_state.provider` CHECK constraint (`'claude'`/`'codex'`) was
  **removed entirely** — it predated the /goal pivot to Codex + Ollama
  Cloud and would have rejected `ollama_cloud`/any future backend name;
  caught by a real test failure, not by inspection.

## Explicitly deferred

- **Professor callback (§3.3) and everything downstream of it**: an
  accepted paper leaves its task in `pending_prof_review` and stops
  there. Nothing yet makes the resolve/keep-going/split/abandon decision,
  so tasks are never `completed`, students are never nominated, and the
  defense → graduation → `lab_proposals` chain (§3.4/§3.5) has no
  upstream trigger. This is now the biggest remaining gap.
  (Decomposition → task rows → student work → paper → review tally is
  built; see Phase 4.6.)
- Memory compaction jobs (§6.3).
- `lab_proposals` / defense / promotion flow (§3.4/§3.5) — nothing
  upstream of it (defenses) exists yet.
- Multi-daemon / distributed execution — out of scope per DESIGN.md §9.
