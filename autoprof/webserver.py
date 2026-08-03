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
a {{ color: #06c; }}
pre {{ white-space: pre-wrap; background: #f6f6f6; padding: 0.75rem; border-radius: 4px; }}
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
<body>{body}</body></html>"""

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
        f"<td class='status'>{_e(r['verdict'])}</td></tr>"
        for r in reviews
    ) or "<tr><td colspan='3'><em>no reviews yet</em></td></tr>"

    body = (
        f"<p><a href='/'>&larr; all labs</a></p>"
        f"<h1>Lab #{lab['id']}</h1>"
        f"<p>status: <span class='status'>{_e(lab['status'])}</span> "
        f"&mdash; professor: <a href='/professors/{professor['id']}'>{_e(professor['name'])}</a> "
        f"({_e(professor['field'])})</p>"
        f"<h2>Root problem</h2><div class='mathdoc'>{_e(lab['root_problem'])}</div>"
        f"<h2>Tasks</h2><table><tr><th>id</th><th>title</th><th>status</th><th>direction</th><th>papers</th></tr>{task_rows}</table>"
        f"<h2>Lab reviews</h2><table><tr><th>round</th><th>reviewer</th><th>verdict</th></tr>{review_rows}</table>"
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
        f"<tr><td>#{t['id']}</td><td>{_e(t['tool'])}</td>"
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
    (re.compile(r"^/labs/(\d+)$"), lambda conn, m, d: render_lab_detail(conn, int(m.group(1)))),
    (re.compile(r"^/students/(\d+)$"), lambda conn, m, d: render_student_detail(conn, int(m.group(1)))),
    (re.compile(r"^/professors/(\d+)$"), lambda conn, m, d: render_professor_detail(conn, int(m.group(1)))),
    (re.compile(r"^/papers/(\d+)$"), lambda conn, m, d: render_paper_detail(conn, int(m.group(1)))),
    (re.compile(r"^/papers/(\d+)/full$"), lambda conn, m, d: render_paper_full(conn, int(m.group(1)), d)),
    (re.compile(r"^/reviews/(\d+)$"), lambda conn, m, d: render_review_rationale(conn, int(m.group(1)), d)),
    (re.compile(r"^/tasks/(\d+)$"), lambda conn, m, d: render_task_detail(conn, int(m.group(1)), d)),
    (re.compile(r"^/supervision/(\d+)/(\d+)$"),
     lambda conn, m, d: render_supervision(conn, int(m.group(1)), int(m.group(2)), d)),
]


def make_server(db_path, host: str = "127.0.0.1", port: int = 8765, lab_dir=None) -> HTTPServer:
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
