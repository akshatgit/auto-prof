"""Minimal read-only web UI -- docs/DESIGN.md §8.

stdlib http.server only, no framework -- consistent with the rest of
this project's "no heavy dependencies" principle. Read-only over all
research state (labs/professors/tasks/students/reviews); there are no
write routes here yet -- §8's only planned write path is the human
approval gate for lab_proposals, which isn't built yet either (see
docs/TASKS.md Phase 5).

Every value pulled from the DB is model-generated or human-supplied text,
never trusted as safe HTML -- html.escape() is applied at render time
everywhere user/model content is interpolated.
"""

import html
import re
import sqlite3
from datetime import datetime
from urllib.parse import quote, unquote

from . import markdown
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }}
.status {{ font-family: monospace; }}
.nav {{ margin: 0 0 1.25rem; padding-bottom: 0.5rem; border-bottom: 1px solid #e2e2e2; }}
.nav a {{ margin-right: 1rem; text-decoration: none; font-weight: 600; }}
.work {{ font-family: monospace; }}
.stale {{ color: #b1450d; }} .fresh {{ color: #14794a; }} .muted {{ color: #777; }}
a {{ color: #06c; }}
pre {{ white-space: pre-wrap; background: #f6f6f6; padding: 0.75rem; border-radius: 4px; }}
.tool-payload {{ max-height: 32rem; overflow: auto; border: 1px solid #e2e2e2; }}
/* Not <pre>: MathJax skips pre/code by default, so LaTeX inside one is
   never typeset. pre-wrap here keeps the source formatting while still
   letting MathJax process the content. */
.mathdoc {{ white-space: pre-wrap; background: #f6f6f6; padding: 0.75rem;
           border-radius: 4px; line-height: 1.5; overflow-x: auto; }}
/* Rendered Markdown: NOT pre-wrap -- the renderer emits real block
   elements, and pre-wrap would double every paragraph break. */
.doc {{ background: #fbfbfa; padding: 0.75rem 1rem; border-radius: 4px; line-height: 1.55; overflow-x: auto; }}
.doc h2, .doc h3, .doc h4 {{ margin: 1rem 0 0.4rem; line-height: 1.3; }}
.doc h2 {{ font-size: 1.1rem; }} .doc h3 {{ font-size: 1rem; }} .doc h4 {{ font-size: 0.95rem; }}
.doc p {{ margin: 0.5rem 0; }}
.doc ul, .doc ol {{ margin: 0.5rem 0 0.5rem 1.4rem; }}
.doc li {{ margin: 0.25rem 0; }}
.doc blockquote {{ margin: 0.5rem 0; padding-left: 0.8rem; border-left: 3px solid #d6d5d0; color: #52514e; }}
.doc code {{ background: #eeeeec; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }}
.doc pre {{ background: #f2f2f0; padding: 0.6rem; border-radius: 4px; overflow-x: auto; }}
.doc pre code {{ background: none; padding: 0; }}
.doc hr {{ border: 0; border-top: 1px solid #ddd; margin: 1rem 0; }}
</style>
<script>
  // Root problems and task briefs are written in LaTeX. Without this they
  // render as literal \\( ... \\) source. If the CDN is unreachable the page
  // still works -- you just see the raw LaTeX, which is what it did before.
  window.MathJax = {{
    tex: {{
      inlineMath: [['\\\\(', '\\\\)']],
      displayMath: [['\\\\[', '\\\\]'], ['$$', '$$']]
    }},
    options: {{ skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'code'] }}
  }};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
</head>
<body><nav class='nav'><a href='/'>Labs</a><a href='/jobs'>Jobs</a></nav>{body}</body></html>"""

_MATH_STRIP_RE = re.compile(r"\\[\[\]()]|\\[a-zA-Z]+\s*|[{}$]")


def _e(value) -> str:
    return html.escape(str(value)) if value is not None else ""



def _reviewer_label(row) -> str:
    """`#2 (claude)` -- names the model family that judged.

    Shown because a panel is only meaningful if it is actually mixed, and
    the failure mode is silent: a misconfigured panel that collapses to
    one family still renders three reviews that look independent.
    """
    try:
        backend = row["reviewer_backend"]
    except (IndexError, KeyError):
        backend = None
    suffix = f" <span class='muted'>({_e(backend)})</span>" if backend else ""
    return f"#{row['reviewer_index']}{suffix}"


def _plain_preview(text: str, limit: int = 140) -> str:
    """A readable one-line preview of a LaTeX document.

    Truncating raw LaTeX at a fixed offset usually cuts mid-command and
    leaves a fragment like `\\(E,\\mathcal I` in the table. Strip the
    markup first so the preview is words, then truncate.
    """
    stripped = _MATH_STRIP_RE.sub(" ", str(text or ""))
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped[:limit] + ("..." if len(stripped) > limit else "")


def render_lab_list(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT labs.*, professors.name AS professor_name "
        "FROM labs JOIN professors ON professors.id = labs.professor_id "
        "ORDER BY labs.id"
    ).fetchall()
    items = "".join(
        f"<tr><td><a href='/labs/{r['id']}'>#{r['id']}</a></td>"
        f"<td class='status'>{_e(r['status'])}</td>"
        f"<td>{_e(r['professor_name'])}</td>"
        f"<td>{_e(_plain_preview(r['root_problem']))}</td></tr>"
        for r in rows
    )
    body = (
        "<h1>Labs</h1>"
        "<table><tr><th>id</th><th>status</th><th>professor</th><th>root problem</th></tr>"
        f"{items}</table>"
    )
    return _PAGE.format(title="autoprof — Labs", body=body)


def render_jobs(conn: sqlite3.Connection) -> str:
    """Live view of what the daemon is doing right now.

    The column that matters is `work`: tokens the model has actually
    produced and discrete items completed. Without it a job deep in a long
    research round and a deadlocked one look identical, which is exactly
    the confusion that cost this installation hours.
    """
    running = conn.execute(
        "SELECT id, kind, target_type, target_id, started_at, lease_expires_at, "
        "progress_at, progress_tokens, progress_items, attempts, backend, backend_model "
        "FROM jobs WHERE status = 'running' ORDER BY id"
    ).fetchall()

    rows = []
    for r in running:
        if r["progress_at"]:
            tokens, items = r["progress_tokens"] or 0, r["progress_items"] or 0
            work = (f"<span class='work fresh'>{tokens} tokens &middot; {items} items</span>"
                    f"<br><span class='muted'>last output {_e(r['progress_at'])}</span>")
        else:
            work = "<span class='work stale'>no output yet</span>"
        harness = _e(r["backend"] or "?")
        if r["backend_model"]:
            harness += f"<br><span class='muted'>{_e(r['backend_model'])}</span>"
        rows.append(
            f"<tr><td><a href='/jobs/{r['id']}'>#{r['id']}</a></td><td>{_e(r['kind'])}</td>"
            f"<td>{_e(r['target_type'])} {r['target_id']}</td>"
            f"<td class='work'>{harness}</td>"
            f"<td class='muted'>{_e(r['started_at'])}</td>"
            f"<td>{work}</td></tr>"
        )
    running_table = (
        "<table><tr><th>job</th><th>kind</th><th>target</th><th>harness</th>"
        f"<th>started</th><th>work produced</th></tr>{''.join(rows)}</table>"
        if rows else "<p class='muted'>Nothing running.</p>"
    )

    queued = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM jobs WHERE status = 'pending' "
        "GROUP BY kind ORDER BY n DESC"
    ).fetchall()
    queue = "".join(
        f"<tr><td>{_e(q['kind'])}</td><td>{q['n']}</td></tr>" for q in queued
    )
    queue_table = (
        f"<table><tr><th>kind</th><th>pending</th></tr>{queue}</table>"
        if queue else "<p class='muted'>Queue empty.</p>"
    )

    totals = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY n DESC"
    ).fetchall()
    totals_line = " &middot; ".join(f"{_e(t['status'])} {t['n']}" for t in totals)

    failed = conn.execute(
        "SELECT id, kind, target_id, substr(last_error, 1, 160) AS err FROM jobs "
        "WHERE status = 'failed' ORDER BY id DESC LIMIT 15"
    ).fetchall()
    fail_rows = "".join(
        f"<tr><td><a href='/jobs/{f['id']}'>#{f['id']}</a></td><td>{_e(f['kind'])}</td>"
        f"<td>{f['target_id']}</td>"
        f"<td class='muted'>{_e((f['err'] or '').splitlines()[0] if f['err'] else '')}</td></tr>"
        for f in failed
    )
    fail_table = (
        "<table><tr><th>job</th><th>kind</th><th>target</th><th>error</th></tr>"
        f"{fail_rows}</table>" if fail_rows else "<p class='muted'>No failures.</p>"
    )

    from . import usage as usage_mod
    tot = usage_mod.totals(conn)
    r20 = usage_mod.rate(conn, minutes=20)
    r10 = usage_mod.rate(conn, minutes=10)
    spend = usage_mod.cost(conn)

    def n(value):
        return f"{int(value):,}"

    if spend["by_model"]:
        rows_cost = "".join(
            f"<tr><td>{_e(m['model'])}</td><td class='work'>${m['amount']:,.2f}</td>"
            f"<td class='muted'>{n(m['produced'])} out / {n(m['input'])} in</td></tr>"
            for m in spend["by_model"])
        cost_block = (f"<table><tr><th>model</th><th>estimated cost</th><th>tokens</th></tr>"
                      f"{rows_cost}</table>"
                      f"<p class='work'>total estimate ${spend['total']:,.2f}</p>")
    else:
        cost_block = "<p class='muted'>No prices configured, so no cost is estimated.</p>"
    if spend["unpriced"]:
        cost_block += (
            "<p class='muted'>Unpriced: " + _e(", ".join(spend["unpriced"])) +
            ". Set <code>AUTOPROF_PRICE_&lt;MODEL&gt;_INPUT|CACHED|OUTPUT</code> "
            "(dollars per million tokens) to include them.</p>")

    usage_block = (
        "<h2>Tokens</h2>"
        "<table>"
        f"<tr><th>produced (lifetime)</th><td class='work'>{n(tot['produced'])}</td></tr>"
        f"<tr><th>prompt in / of which cached</th>"
        f"<td class='work'>{n(tot['input'])} / {n(tot['cached'])}</td></tr>"
        f"<tr><th>last 10 min</th><td class='work'>{n(r10['produced'])} "
        f"<span class='muted'>({n(r10['per_hour'])}/hr across {r10['jobs']} job(s))</span></td></tr>"
        f"<tr><th>last 20 min</th><td class='work'>{n(r20['produced'])} "
        f"<span class='muted'>({n(r20['per_hour'])}/hr across {r20['jobs']} job(s))</span></td></tr>"
        "</table>"
        "<h2>Estimated cost</h2>" + cost_block
    )

    body = (
        "<h1>Jobs</h1>"
        f"<p class='muted'>{totals_line}</p>"
        + usage_block
        + f"<h2>Running ({len(running)})</h2>{running_table}"
        f"<h2>Queued</h2>{queue_table}"
        "<h2>Recent failures</h2>" + fail_table +
        # Reload rather than a <meta refresh>: the page template is shared and
        # this keeps its signature unchanged.
        "<script>setTimeout(function(){ location.reload(); }, 10000);</script>"
    )
    return _PAGE.format(title="autoprof — Jobs", body=body)


JOB_LOG_TAIL_BYTES = 200_000


def render_job_detail(conn: sqlite3.Connection, job_id: int, db_path=None) -> str | None:
    """Read-only live view of one job: what it is, and what it is emitting.

    The log is the backend's own stream, tailed while the job runs. It is a
    window, not the record of results -- handlers still write artifacts on
    completion.
    """
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None

    def cell(name, value):
        return f"<tr><td>{name}</td><td class='work'>{_e(value)}</td></tr>"

    harness = job["backend"] or "?"
    if job["backend_model"]:
        harness += f" / {job['backend_model']}"
    produced = (
        f"{job['progress_tokens'] or 0} tokens, {job['progress_items'] or 0} items"
        if job["progress_at"] else "no output recorded"
    )
    meta = "".join([
        cell("kind", job["kind"]),
        cell("target", f"{job['target_type']} {job['target_id']}"),
        cell("status", job["status"]),
        cell("harness", harness),
        cell("started", job["started_at"] or "-"),
        cell("last output", job["progress_at"] or "-"),
        cell("work produced", produced),
        cell("attempts", job["attempts"]),
        cell("session", job["backend_session_id"] or "-"),
    ])

    text = ""
    note = ""
    if db_path is not None:
        from .jobs import job_log_path
        path = job_log_path(db_path, job_id)
        try:
            if path.is_file():
                size = path.stat().st_size
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    if size > JOB_LOG_TAIL_BYTES:
                        handle.seek(size - JOB_LOG_TAIL_BYTES)
                        note = (f"<p class='muted'>showing the last "
                                f"{JOB_LOG_TAIL_BYTES // 1000}KB of {size // 1000}KB</p>")
                    text = handle.read()
        except OSError as e:
            note = f"<p class='muted'>log unavailable: {_e(e)}</p>"

    if not text.strip():
        stream = ("<p class='muted'>No streamed output. Jobs record this only while "
                  "running, and only for backends that stream.</p>")
    else:
        stream = f"<pre class='tool-payload'>{_e(text)}</pre>"

    refresh = ("<script>setTimeout(function(){ location.reload(); }, 5000);</script>"
               if job["status"] == "running" else "")
    body = (
        f"<p><a href='/jobs'>&larr; jobs</a></p><h1>Job #{job_id}</h1>"
        f"<table>{meta}</table>"
        f"<h2>Live output</h2>{note}{stream}{refresh}"
    )
    return _PAGE.format(title=f"autoprof — job {job_id}", body=body)


def render_lab_detail(conn: sqlite3.Connection, lab_id: int) -> str | None:
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
    if lab is None:
        return None
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
    ).fetchone()
    tasks = conn.execute("SELECT * FROM tasks WHERE lab_id = ? ORDER BY id", (lab_id,)).fetchall()
    reviews = conn.execute(
        "SELECT * FROM reviews WHERE target_type='lab' AND target_id=? ORDER BY review_round, reviewer_index",
        (lab_id,),
    ).fetchall()

    task_rows = "".join(
        f"<tr><td><a href='/tasks/{t['id']}'>#{t['id']}</a></td><td>{_e(t['title'])}</td><td class='status'>{_e(t['status'])}</td>"
        f"<td>{_e(t['direction'])}</td><td>{_task_paper_links(conn, t['id'])}</td></tr>"
        for t in tasks
    ) or "<tr><td colspan='5'><em>no tasks yet</em></td></tr>"

    review_rows = "".join(
        f"<tr><td>round {r['review_round']}</td><td>{_reviewer_label(r)}</td>"
        f"<td class='status'>{_e(r['verdict'])}</td>"
        f"<td><a href='/reviews/{r['id']}'>rationale</a></td></tr>"
        for r in reviews
    ) or "<tr><td colspan='4'><em>no reviews yet</em></td></tr>"

    body = (
        f"<p><a href='/'>&larr; all labs</a></p>"
        f"<h1>Lab #{lab['id']}</h1>"
        f"<p>status: <span class='status'>{_e(lab['status'])}</span> "
        f"&mdash; professor: <a href='/professors/{professor['id']}'>{_e(professor['name'])}</a> "
        f"({_e(professor['field'])})</p>"
        f"<h2>Root problem</h2><div class='doc'>{markdown.render(lab['root_problem'])}</div>"
        f"<h2>Tasks</h2><table><tr><th>id</th><th>title</th><th>status</th><th>direction</th><th>papers</th></tr>{task_rows}</table>"
        f"<h2>Lab reviews</h2><table><tr><th>round</th><th>reviewer</th>"
        f"<th>verdict</th><th></th></tr>{review_rows}</table>"
    )
    return _PAGE.format(title=f"autoprof — Lab #{lab['id']}", body=body)


def render_student_detail(conn: sqlite3.Connection, student_id: int) -> str | None:
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        return None
    task = None
    if student["task_id"] is not None:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (student["task_id"],)).fetchone()

    paused = f"<p><strong>PAUSED</strong> since {_e(student['paused_at'])}</p>" if student["paused_at"] else ""
    task_html = (
        f"<p>task: <a href='/labs/{task['lab_id']}'>#{task['id']} {_e(task['title'])}</a></p>"
        if task
        else "<p>task: <em>unassigned</em></p>"
    )

    body = (
        f"<p><a href='/'>&larr; all labs</a></p>"
        f"<h1>Student #{student['id']}</h1>"
        f"<p>status: <span class='status'>{_e(student['status'])}</span></p>"
        f"{paused}{task_html}"
        f"<p>memory: <code>{_e(student['memory_path'])}</code></p>"
    )
    return _PAGE.format(title=f"autoprof — Student #{student['id']}", body=body)


def render_professor_detail(conn: sqlite3.Connection, professor_id: int) -> str | None:
    professor = conn.execute("SELECT * FROM professors WHERE id = ?", (professor_id,)).fetchone()
    if professor is None:
        return None
    students = conn.execute(
        "SELECT * FROM students WHERE professor_id = ? ORDER BY id", (professor_id,)
    ).fetchall()
    student_rows = "".join(
        f"<tr><td><a href='/students/{s['id']}'>#{s['id']}</a></td>"
        f"<td class='status'>{_e(s['status'])}</td></tr>"
        for s in students
    ) or "<tr><td colspan='2'><em>no students yet</em></td></tr>"

    body = (
        f"<p><a href='/'>&larr; all labs</a></p>"
        f"<h1>{_e(professor['name'])}</h1>"
        f"<p>field: {_e(professor['field'])} &mdash; status: <span class='status'>{_e(professor['status'])}</span></p>"
        f"<h2>Students</h2><table><tr><th>id</th><th>status</th></tr>{student_rows}</table>"
    )
    return _PAGE.format(title=f"autoprof — {professor['name']}", body=body)


def _task_paper_links(conn: sqlite3.Connection, task_id: int) -> str:
    """Link every paper a task has produced, newest first.

    A task can accumulate several papers over its life, and each carries
    its own round and verdict history, so this lists them all rather than
    guessing which one is "current" -- the latest is simply first.
    """
    papers = conn.execute(
        "SELECT id, status, review_round FROM papers WHERE task_id = ? ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    if not papers:
        return "<em>none</em>"
    return " ".join(
        f"<a href='/papers/{p['id']}'>#{p['id']}</a> "
        f"<span class='status'>({_e(p['status'])} r{p['review_round']})</span>"
        for p in papers
    )


def render_paper_detail(conn: sqlite3.Connection, paper_id: int) -> str | None:
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        return None
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (paper["task_id"],)).fetchone()

    reviews = conn.execute(
        "SELECT * FROM reviews WHERE target_type='paper' AND target_id=? "
        "ORDER BY review_round, reviewer_index",
        (paper_id,),
    ).fetchall()
    review_rows = "".join(
        f"<tr><td>round {r['review_round']}</td><td>{_reviewer_label(r)}</td>"
        f"<td class='status'>{_e(r['verdict'])}</td>"
        f"<td><a href='/reviews/{r['id']}'>rationale</a></td></tr>"
        for r in reviews
    ) or "<tr><td colspan='4'><em>no reviews yet</em></td></tr>"

    body = (
        f"<p><a href='/labs/{task['lab_id']}'>&larr; lab #{task['lab_id']}</a></p>"
        f"<h1>Paper #{paper['id']}</h1>"
        f"<p>{_e(paper['title'])}</p>"
        f"<p>status: <span class='status'>{_e(paper['status'])}</span> "
        f"&mdash; round {paper['review_round']} "
        f"&mdash; task <a href='/labs/{task['lab_id']}'>#{task['id']}</a> "
        f"&mdash; student <a href='/students/{paper['student_id']}'>#{paper['student_id']}</a></p>"
        f"<p><a href='/papers/{paper['id']}/full'><strong>Read the full paper &rarr;</strong></a></p>"
        f"<h2>Reviews</h2><table><tr><th>round</th><th>reviewer</th><th>verdict</th><th></th></tr>"
        f"{review_rows}</table>"
    )
    return _PAGE.format(title=f"autoprof — Paper #{paper['id']}", body=body)



def _read_artifact(lab_dir, relpath: str) -> str | None:
    """Read a lab artifact, refusing anything that escapes lab_dir.

    The paths come from the DB rather than the URL, so this is defence in
    depth rather than the primary control -- but a bad path getting into
    the DB should not become arbitrary file read over HTTP.
    """
    if lab_dir is None or not relpath:
        return None
    root = Path(lab_dir).resolve()
    target = (root / relpath).resolve()
    if not target.is_file() or root not in target.parents:
        return None
    return target.read_text(errors="replace")


# A task's own directory is where its real output lives: the workspace it
# built, the artefacts it produced, the home directory it clones into. None
# of that is in the database, so without a browser the only way to see what a
# lab actually made is to ssh in and look.
TASK_FILE_MAX_BYTES = 400_000
_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".sh", ".json", ".toml", ".yaml", ".yml", ".cfg",
    ".ini", ".diff", ".patch", ".log", ".csv", ".tsv", ".html", ".css", ".js",
    ".go", ".rs", ".c", ".h", ".tex", ".sql", ".dockerfile", ".conf", "",
}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _task_root(conn: sqlite3.Connection, task_id: int, lab_dir) -> Path | None:
    if lab_dir is None:
        return None
    row = conn.execute("SELECT lab_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    root = Path(lab_dir).resolve() / str(row["lab_id"]) / "tasks" / str(task_id)
    return root if root.is_dir() else None


def _confine(root: Path, relpath: str) -> Path | None:
    """Resolve `relpath` under `root`, or None if it escapes.

    Unlike _read_artifact this path comes straight from the URL, so this
    is the primary control, not defence in depth. Resolving first and
    checking parents afterwards also refuses symlinks pointing outside.
    """
    root = root.resolve()
    target = (root / relpath).resolve() if relpath else root
    if target != root and root not in target.parents:
        return None
    return target


def _breadcrumb(task_id: int, relpath: str) -> str:
    parts = [p for p in relpath.split("/") if p]
    crumbs = [f"<a href='/tasks/{task_id}/files'>task {task_id}</a>"]
    for i, part in enumerate(parts):
        sub = "/".join(parts[: i + 1])
        crumbs.append(f"<a href='/tasks/{task_id}/files/{quote(sub)}'>{_e(part)}</a>")
    return " / ".join(crumbs)


def _render_directory(task_id: int, target: Path, relpath: str) -> str:
    try:
        entries = sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except OSError as e:
        return f"<p class='muted'>cannot list this directory: {_e(e)}</p>"

    rows = []
    for entry in entries:
        sub = f"{relpath}/{entry.name}" if relpath else entry.name
        try:
            st = entry.stat()
        except OSError:
            continue
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        if entry.is_dir():
            try:
                count = sum(1 for _ in entry.iterdir())
                size = f"{count} item{'' if count == 1 else 's'}"
            except OSError:
                size = "-"
            name = f"<a href='/tasks/{task_id}/files/{quote(sub)}'>{_e(entry.name)}/</a>"
        else:
            size = _human_size(st.st_size)
            name = f"<a href='/tasks/{task_id}/files/{quote(sub)}'>{_e(entry.name)}</a>"
        rows.append(
            f"<tr><td>{name}</td><td class='muted'>{size}</td>"
            f"<td class='muted'>{when}</td></tr>"
        )
    if not rows:
        return "<p class='muted'>this directory is empty</p>"
    return (
        "<table><tr><th>name</th><th>size</th><th>modified</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _render_file(target: Path) -> str:
    try:
        size = target.stat().st_size
    except OSError as e:
        return f"<p class='muted'>cannot read: {_e(e)}</p>"

    suffix = target.suffix.lower()
    if suffix not in _TEXT_SUFFIXES and target.name.lower() not in ("dockerfile", "makefile"):
        return (
            f"<p class='muted'>{_e(target.name)} — {_human_size(size)}, "
            "not a recognised text type, so it is not shown here.</p>"
        )
    if size > TASK_FILE_MAX_BYTES:
        # Truncate rather than refuse: the head of a huge log is usually
        # the part worth seeing, and refusing outright hides that the
        # file exists at all.
        text = target.read_text(errors="replace")[:TASK_FILE_MAX_BYTES]
        note = (
            f"<p class='muted'>showing the first {_human_size(TASK_FILE_MAX_BYTES)} "
            f"of {_human_size(size)}</p>"
        )
    else:
        try:
            text = target.read_text(errors="replace")
        except OSError as e:
            return f"<p class='muted'>cannot read: {_e(e)}</p>"
        note = f"<p class='muted'>{_human_size(size)}</p>"
    return note + f"<pre class='doc'>{_e(text)}</pre>"


def render_task_files(
    conn: sqlite3.Connection, task_id: int, relpath: str, lab_dir
) -> str | None:
    root = _task_root(conn, task_id, lab_dir)
    if root is None:
        return None
    relpath = unquote(relpath or "").strip("/")
    target = _confine(root, relpath)
    if target is None or not target.exists():
        return None

    body = (
        f"<p><a href='/tasks/{task_id}'>&larr; task #{task_id}</a></p>"
        f"<h1>Files</h1><p>{_breadcrumb(task_id, relpath)}</p>"
    )
    body += _render_directory(task_id, target, relpath) if target.is_dir() else _render_file(target)
    label = relpath or "task folder"
    return _PAGE.format(title=f"autoprof — task {task_id} — {label}", body=body)


_MATHJAX_TAG = (
    "<script>window.MathJax={tex:{inlineMath:[['\\\\(','\\\\)']],"
    "displayMath:[['\\\\[','\\\\]'],['$$','$$']],tags:'none'},"
    "options:{skipHtmlTags:['script','noscript','style','textarea','code']}};</script>"
    "<script async src=\"https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js\"></script>"
)


def _ensure_mathjax(document: str) -> str:
    """Inject MathJax into a paper that predates the template carrying it.

    templates/paper_template.html now includes MathJax, but papers already
    written from the older template are full of raw \\( ... \\) that would
    display as source. Papers are immutable review artifacts -- rewriting
    them on disk would change a document reviewers already ruled on -- so
    the fix is applied at serve time only, and only when absent.
    """
    if "mathjax" in document.lower():
        return document
    lowered = document.lower()
    for anchor in ("</head>", "<style"):
        i = lowered.find(anchor)
        if i != -1:
            return document[:i] + _MATHJAX_TAG + document[i:]
    return _MATHJAX_TAG + document


def render_paper_full(conn: sqlite3.Connection, paper_id: int, lab_dir) -> str | None:
    """Serve the generated ACM paper itself, as-is.

    Returned unmodified rather than embedded in the site chrome: it is a
    complete self-contained HTML document with its own two-column layout
    and CSS counters, and wrapping it would break exactly the formatting
    worth looking at.
    """
    paper = conn.execute("SELECT path FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        return None
    document = _read_artifact(lab_dir, paper["path"])
    if document is None:
        return None
    return _ensure_mathjax(document)


def render_review_rationale(conn: sqlite3.Connection, review_id: int, lab_dir) -> str | None:
    review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if review is None:
        return None
    text = _read_artifact(lab_dir, review["rationale_path"])
    if text is None:
        return None
    back = (
        f"/papers/{review['target_id']}" if review["target_type"] == "paper"
        else f"/labs/{review['target_id']}"
    )
    body = (
        f"<p><a href='{back}'>&larr; back</a></p>"
        f"<h1>Review: {_e(review['target_type'])} #{review['target_id']}</h1>"
        f"<p>round {review['review_round']} &mdash; reviewer {_reviewer_label(review)} "
        f"&mdash; verdict: <span class='status'>{_e(review['verdict'])}</span></p>"
        f"<div class='doc'>{markdown.render(text)}</div>"
    )
    return _PAGE.format(title=f"autoprof — review #{review['id']}", body=body)


# Each entry: (pattern, render(conn, match, lab_dir)). lab_dir is passed to
# every route so the file-backed ones (papers, review rationales) can reach
# the artifacts; DB-only routes ignore it.
# Status colours, paired ALWAYS with a text label -- verdict is state, not
# category, and colour alone excludes colour-blind readers and greyscale
# printing. See the palette validation in autoprof/tools.py.
_VERDICT_STYLE = {
    "strong_accept": ("#1b7f4f", "++"),
    "accept": ("#1baf7a", "+"),
    "weak_accept": ("#eda100", "~+"),
    "weak_reject": ("#eb6834", "~-"),
    "reject": ("#d9432f", "-"),
    "strong_reject": ("#a02617", "--"),
}
_SUPERVISION_STYLE = {
    "continue": ("#2a78d6", "continue"),
    "ready": ("#1b7f4f", "READY"),
    "abandon": ("#a02617", "abandoned"),
}


def render_task_timeline(meetings, rounds) -> str:
    """The long-horizon arc of one task as an inline SVG.

    Two tracks on one time axis because they are one story: the
    supervision loop that ran BEFORE any paper existed, then the review
    rounds after. Seeing eleven `continue` meetings followed by a forced
    write-up tells you something no table of counts does.
    """
    if not meetings and not rounds:
        return "<p><em>no supervision or review history yet</em></p>"

    step, left, top = 66, 90, 34
    # Lane separation is set by LOOKING at the render: at 58 the
    # supervision caption and the review round label sat 12px apart and
    # read as one line. Layout collisions are invisible to tests.
    lane_h = 86
    width = max(560, left + step * (max(len(meetings), len(rounds)) + 1))
    height = top + lane_h * 2 + 20

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="task timeline: {len(meetings)} supervision meetings, '
        f'{len(rounds)} review rounds">',
        '<g font-family="system-ui, sans-serif" font-size="11">',
    ]

    for lane, (label, items) in enumerate(
        (("supervision", meetings), ("review", rounds))
    ):
        y = top + lane * lane_h
        out.append(
            f'<text x="8" y="{y + 4}" fill="#52514e" font-weight="bold">{label}</text>'
        )
        if not items:
            out.append(f'<text x="{left}" y="{y + 4}" fill="#8a8983">none</text>')
            continue
        out.append(
            f'<line x1="{left}" y1="{y}" x2="{left + step * (len(items) - 1) + 1}" y2="{y}" '
            'stroke="#ddd" stroke-width="2"/>'
        )
        for index, item in enumerate(items):
            x = left + index * step
            if lane == 0:
                colour, caption = _SUPERVISION_STYLE.get(item["verdict"], ("#8a8983", item["verdict"]))
                out.append(f'<circle cx="{x}" cy="{y}" r="7" fill="{colour}"/>')
                out.append(
                    f'<text x="{x}" y="{y - 14}" text-anchor="middle" fill="#52514e">'
                    f'm{item["round"]}</text>'
                )
                out.append(
                    f'<text x="{x}" y="{y + 22}" text-anchor="middle" fill="#52514e" '
                    f'font-size="9">{caption}</text>'
                )
            else:
                # A round is 3 verdicts; draw them stacked so the tally is
                # visible rather than averaged into one mark.
                for slot, verdict in enumerate(item["verdicts"]):
                    colour, mark = _VERDICT_STYLE.get(verdict, ("#8a8983", "?"))
                    cy = y - 10 + slot * 10
                    out.append(
                        f'<rect x="{x - 9}" y="{cy - 4}" width="18" height="8" rx="2" '
                        f'fill="{colour}"><title>{_e(verdict)}</title></rect>'
                    )
                out.append(
                    f'<text x="{x}" y="{y - 24}" text-anchor="middle" fill="#52514e">'
                    f'r{item["round"]}</text>'
                )
                out.append(
                    f'<text x="{x}" y="{y + 26}" text-anchor="middle" fill="#52514e" '
                    f'font-size="9">{item["strong"]}x++</text>'
                )

    out.append("</g></svg>")
    return "".join(out)


def render_task_detail(conn: sqlite3.Connection, task_id: int, lab_dir) -> str | None:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        return None

    meetings = [
        {"round": r["round"], "verdict": r["verdict"], "path": r["guidance_path"]}
        for r in conn.execute(
            "SELECT * FROM supervisions WHERE task_id = ? ORDER BY round", (task_id,)
        )
    ]

    papers = conn.execute(
        "SELECT * FROM papers WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    rounds = []
    for paper in papers:
        for row in conn.execute(
            "SELECT review_round, GROUP_CONCAT(verdict) AS vs, "
            "SUM(verdict='strong_accept') AS strong FROM reviews "
            "WHERE target_type='paper' AND target_id=? GROUP BY review_round ORDER BY review_round",
            (paper["id"],),
        ):
            rounds.append({
                "round": row["review_round"],
                "verdicts": (row["vs"] or "").split(","),
                "strong": row["strong"] or 0,
            })

    meeting_rows = "".join(
        f"<tr><td>meeting {m['round']}</td>"
        f"<td class='status'>{_e(m['verdict'])}</td>"
        f"<td><a href='/supervision/{task_id}/{m['round']}'>guidance</a></td></tr>"
        for m in meetings
    ) or "<tr><td colspan='3'><em>no supervision meetings yet</em></td></tr>"

    paper_rows = "".join(
        f"<tr><td><a href='/papers/{p['id']}'>#{p['id']}</a></td>"
        f"<td>{_e(p['title'][:70])}</td>"
        f"<td class='status'>{_e(p['status'])}</td><td>round {p['review_round']}</td></tr>"
        for p in papers
    ) or "<tr><td colspan='4'><em>no papers yet</em></td></tr>"

    ledger = conn.execute(
        "SELECT * FROM assumptions WHERE task_id = ? ORDER BY "
        "CASE status WHEN 'refuted' THEN 0 WHEN 'assumed' THEN 1 ELSE 2 END, id",
        (task_id,),
    ).fetchall()
    ledger_rows = "".join(
        f"<tr><td class='status'>{_e(a['source'])}/{_e(a['status'])}</td>"
        f"<td>{_e(a['statement'][:140])}</td></tr>"
        for a in ledger
    ) or "<tr><td colspan='2'><em>no assumptions registered</em></td></tr>"

    tools_run = conn.execute(
        "SELECT * FROM tool_runs WHERE task_id = ? ORDER BY id DESC LIMIT 20", (task_id,)
    ).fetchall()
    tool_rows = "".join(
        f"<tr><td><a href='/tools/{t['id']}'>#{t['id']}</a></td>"
        f"<td><a href='/tools/{t['id']}'>{_e(t['tool'])}</a></td>"
        f"<td class='status'>{_e(t['status'])}</td>"
        f"<td>{_e((t['summary'] or '')[:80])}</td></tr>"
        for t in tools_run
    ) or "<tr><td colspan='4'><em>no tool runs</em></td></tr>"

    body = (
        f"<p><a href='/labs/{task['lab_id']}'>&larr; lab #{task['lab_id']}</a></p>"
        f"<h1>Task #{task['id']}</h1>"
        f"<p>{_e(task['title'])}</p>"
        f"<p>status: <span class='status'>{_e(task['status'])}</span> "
        f"&mdash; direction: {_e(task['direction'])}"
        + (f" &mdash; student <a href='/students/{task['assigned_student_id']}'>"
           f"#{task['assigned_student_id']}</a>" if task["assigned_student_id"] else "")
        + "</p>"
        f"<p><a href='/tasks/{task['id']}/files'><strong>Browse this task's files &rarr;</strong></a>"
        " <span class='muted'>workspace, artefacts and the task home</span></p>"
        f"<h2>Long-horizon progress</h2>{render_task_timeline(meetings, rounds)}"
        f"<h2>End criteria</h2><div class='mathdoc'>{_e(task['end_criteria'])}</div>"
        f"<h2>Supervision ({len(meetings)} meetings)</h2>"
        f"<table><tr><th></th><th>verdict</th><th></th></tr>{meeting_rows}</table>"
        f"<h2>Papers</h2><table><tr><th>id</th><th>title</th><th>status</th><th></th></tr>"
        f"{paper_rows}</table>"
        f"<h2>Assumption ledger</h2><table><tr><th>source/status</th><th>statement</th></tr>"
        f"{ledger_rows}</table>"
        f"<h2>Tool runs</h2><table><tr><th>id</th><th>tool</th><th>status</th><th>summary</th></tr>"
        f"{tool_rows}</table>"
    )
    return _PAGE.format(title=f"autoprof — Task #{task['id']}", body=body)


def render_tool_run(conn: sqlite3.Connection, run_id: int, lab_dir) -> str | None:
    """Render one recorded tool call without executing artifact content.

    Inputs and outputs are escaped inside ``pre`` elements.  In particular,
    an SVG or HTML-producing tool is shown as source rather than injected into
    the UI page.
    """
    run = conn.execute("SELECT * FROM tool_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None

    arguments = _read_artifact(lab_dir, run["input_path"])
    output = _read_artifact(lab_dir, run["output_path"])
    arguments_html = _e(arguments if arguments is not None else "(arguments artifact missing)")
    output_html = _e(output if output is not None else "(output artifact missing)")

    if run["status"] == "ok":
        result_html = f"<pre class='tool-payload'>{output_html}</pre>"
        error_html = "<p><em>none</em></p>"
    else:
        result_html = "<p><em>none</em></p>"
        error_html = f"<pre class='tool-payload'>{output_html}</pre>"

    back = f"/tasks/{run['task_id']}" if run["task_id"] is not None else f"/labs/{run['lab_id']}"
    body = (
        f"<p><a href='{back}'>&larr; back</a></p>"
        f"<h1>Tool call #{run['id']}: {_e(run['tool'])}</h1>"
        f"<p>status: <span class='status'>{_e(run['status'])}</span> "
        f"&mdash; created: {_e(run['created_at'])}</p>"
        f"<h2>Arguments</h2><pre class='tool-payload'>{arguments_html}</pre>"
        f"<h2>Result</h2>{result_html}"
        f"<h2>Error</h2>{error_html}"
    )
    return _PAGE.format(title=f"autoprof — Tool call #{run['id']}", body=body)


def render_supervision(conn: sqlite3.Connection, task_id: int, round_: int, lab_dir) -> str | None:
    row = conn.execute(
        "SELECT * FROM supervisions WHERE task_id = ? AND round = ?", (task_id, round_)
    ).fetchone()
    if row is None:
        return None
    text = _read_artifact(lab_dir, row["guidance_path"])
    if text is None:
        return None
    body = (
        f"<p><a href='/tasks/{task_id}'>&larr; task #{task_id}</a></p>"
        f"<h1>Supervision meeting {round_}</h1>"
        f"<p>verdict: <span class='status'>{_e(row['verdict'])}</span></p>"
        f"<div class='doc'>{markdown.render(text)}</div>"
    )
    return _PAGE.format(title=f"autoprof — task {task_id} meeting {round_}", body=body)


_ROUTES = [
    (re.compile(r"^/$"), lambda conn, m, d: render_lab_list(conn)),
    (re.compile(r"^/jobs$"), lambda conn, m, d: render_jobs(conn)),
    (re.compile(r"^/jobs/(\d+)$"),
     lambda conn, m, d: render_job_detail(conn, int(m.group(1)), _DB_PATH.get("path"))),
    (re.compile(r"^/labs/(\d+)$"), lambda conn, m, d: render_lab_detail(conn, int(m.group(1)))),
    (re.compile(r"^/students/(\d+)$"), lambda conn, m, d: render_student_detail(conn, int(m.group(1)))),
    (re.compile(r"^/professors/(\d+)$"), lambda conn, m, d: render_professor_detail(conn, int(m.group(1)))),
    (re.compile(r"^/papers/(\d+)$"), lambda conn, m, d: render_paper_detail(conn, int(m.group(1)))),
    (re.compile(r"^/papers/(\d+)/full$"), lambda conn, m, d: render_paper_full(conn, int(m.group(1)), d)),
    (re.compile(r"^/reviews/(\d+)$"), lambda conn, m, d: render_review_rationale(conn, int(m.group(1)), d)),
    (re.compile(r"^/tasks/(\d+)$"), lambda conn, m, d: render_task_detail(conn, int(m.group(1)), d)),
    (re.compile(r"^/tasks/(\d+)/files(?:/(.*))?$"),
     lambda conn, m, d: render_task_files(conn, int(m.group(1)), m.group(2) or "", d)),
    (re.compile(r"^/tools/(\d+)$"), lambda conn, m, d: render_tool_run(conn, int(m.group(1)), d)),
    (re.compile(r"^/supervision/(\d+)/(\d+)$"),
     lambda conn, m, d: render_supervision(conn, int(m.group(1)), int(m.group(2)), d)),
]


_DB_PATH: dict = {}


def make_server(db_path, host: str = "127.0.0.1", port: int = 8765, lab_dir=None) -> HTTPServer:
    _DB_PATH["path"] = db_path
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep test/CLI output quiet; not a design decision worth a knob yet

        def do_GET(self):
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            try:
                for pattern, render in _ROUTES:
                    m = pattern.match(self.path)
                    if m:
                        result = render(conn, m, lab_dir)
                        if result is None:
                            self._respond(404, "<h1>404</h1><p>not found</p>")
                        else:
                            self._respond(200, result)
                        return
                self._respond(404, "<h1>404</h1><p>not found</p>")
            finally:
                conn.close()

        def _respond(self, status: int, body: str):
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            # Every page is a live read of research state that changes every
            # few minutes. With no cache directive at all the browser is free
            # to reuse a heuristically-cached copy, which showed a stale lab
            # -- old tool runs, an old paper round -- while the database had
            # already moved on. Nothing here is cacheable by construction.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(encoded)

    return HTTPServer((host, port), Handler)


def run_server(db_path, host: str = "127.0.0.1", port: int = 8765, lab_dir=None) -> None:
    server = make_server(db_path, host, port, lab_dir)
    print(f"autoprof web UI listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nautoprof web UI stopping (Ctrl-C)")
    finally:
        server.server_close()
