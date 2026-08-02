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
</style></head>
<body>{body}</body></html>"""


def _e(value) -> str:
    return html.escape(str(value)) if value is not None else ""


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
        f"<td>{_e(r['root_problem'][:140])}</td></tr>"
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
        f"<tr><td>#{t['id']}</td><td>{_e(t['title'])}</td><td class='status'>{_e(t['status'])}</td>"
        f"<td>{_e(t['direction'])}</td></tr>"
        for t in tasks
    ) or "<tr><td colspan='4'><em>no tasks yet</em></td></tr>"

    review_rows = "".join(
        f"<tr><td>round {r['review_round']}</td><td>#{r['reviewer_index']}</td>"
        f"<td class='status'>{_e(r['verdict'])}</td></tr>"
        for r in reviews
    ) or "<tr><td colspan='3'><em>no reviews yet</em></td></tr>"

    body = (
        f"<p><a href='/'>&larr; all labs</a></p>"
        f"<h1>Lab #{lab['id']}</h1>"
        f"<p>status: <span class='status'>{_e(lab['status'])}</span> "
        f"&mdash; professor: <a href='/professors/{professor['id']}'>{_e(professor['name'])}</a> "
        f"({_e(professor['field'])})</p>"
        f"<h2>Root problem</h2><pre>{_e(lab['root_problem'])}</pre>"
        f"<h2>Tasks</h2><table><tr><th>id</th><th>title</th><th>status</th><th>direction</th></tr>{task_rows}</table>"
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


_ROUTES = [
    (re.compile(r"^/$"), lambda conn, m: render_lab_list(conn)),
    (re.compile(r"^/labs/(\d+)$"), lambda conn, m: render_lab_detail(conn, int(m.group(1)))),
    (re.compile(r"^/students/(\d+)$"), lambda conn, m: render_student_detail(conn, int(m.group(1)))),
    (re.compile(r"^/professors/(\d+)$"), lambda conn, m: render_professor_detail(conn, int(m.group(1)))),
]


def make_server(db_path, host: str = "127.0.0.1", port: int = 8765) -> HTTPServer:
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
                        result = render(conn, m)
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


def run_server(db_path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(db_path, host, port)
    print(f"autoprof web UI listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nautoprof web UI stopping (Ctrl-C)")
    finally:
        server.server_close()
