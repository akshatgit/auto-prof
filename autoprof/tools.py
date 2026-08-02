"""Lab-agnostic tools students can call while working.

Four capabilities, available to every lab:

- **verify**: run a self-contained Python program and capture what it
  printed. For the finite claims these papers are full of -- "every pure
  rank-4 system with this property has defect >= 1/2", "the spectrum in
  rank 5 is exactly S" -- exhaustive search settles in seconds what prose
  argues about for rounds. A reviewer told one student their claim about
  the first failing rank was simply wrong; a search would have caught it
  before the paper existed.
- **visualize**: turn a declarative chart spec into an inline SVG. The
  spec is declarative rather than a drawing program on purpose: the
  colour, axis and labelling rules that make a figure readable (and
  greyscale-safe, since these papers are printed) are enforced here once,
  instead of being restated in a prompt and followed inconsistently.

- **readfile**: read a file from a repository the lab is studying. A lab
  whose subject IS a codebase needs to see the code; it also needs to be
  unable to wander outside the configured root.
- **propose_patch**: record a proposed change to that repository. It is
  never applied. An agent studying the system it runs inside must be able
  to suggest improvements and must not be able to edit the daemon
  executing it -- a bad write to the job loop breaks the process
  mid-flight, and a self-modifying agent cannot be reviewed after the
  fact. The proposal is the deliverable; a human applies it.

All are invoked from a student's own output via fenced blocks, executed
between prompts, and recorded in `tool_runs` -- so "verified by exhaustive
search" in a paper is traceable to the exact program and its exact output.
"""

import json
import re
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

VERIFY_TIMEOUT_SECONDS = 60
VERIFY_MEMORY_BYTES = 1_024 * 1_024 * 1_024  # 1 GiB
VERIFY_OUTPUT_LIMIT = 20_000
MAX_TOOL_CALLS_PER_ROUND = 4

# Validated for colour-vision deficiency (see the dataviz palette
# validator). Slots 3 and 4 fall below 3:1 contrast on white, which is why
# every series is also directly labelled and given its own dash pattern --
# identity never rests on colour alone, and the figure survives greyscale
# printing.
SERIES_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SERIES_DASHES = ("", "6 3", "2 3", "8 3 2 3")

_TOOL_BLOCK_RE = re.compile(
    r"```tool:(verify|visualize|readfile|propose_patch|apply_patch)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)

# Where `readfile` and `propose_patch` are allowed to look. Set per lab by
# whoever founds it; a lab with no repo configured simply has no access.
# Deliberately one directory rather than a path list: a research lab that
# needs to read two unrelated trees is a sign the scope is wrong.
REPO_ROOT_ENV = "AUTOPROF_REPO_ROOT"
READFILE_LIMIT = 40_000

TOOL_DOCS = """You have these tools. To use one, emit a fenced block in your response; it will be \
run and the result given back to you before you finalise your work.

**verify** -- run a self-contained Python 3 program and get back what it printed. Use this to \
CHECK finite claims rather than asserting them: exhaustive search over small cases, computing an \
invariant on a construction you propose, testing a conjectured formula against brute force. \
Standard library only, no network, {timeout}s limit. Print your conclusion clearly.

```tool:verify
from itertools import combinations
# ... your program ...
print("RESULT: ...")
```

**visualize** -- turn a chart spec into an SVG figure you can paste straight into your paper. \
Give JSON, not drawing code.

```tool:visualize
{{"kind": "step", "title": "Exact spectrum", "x_label": "epsilon", "y_label": "R_r",
  "series": [{{"name": "r=4", "points": [[0,1],[0.5,0.5],[0.667,0.333],[1,0.25]]}}]}}
```

`kind` is "line", "step" or "scatter". Each series needs a `name` (it is directly labelled) and \
`points` as [x, y] pairs. Up to {max_series} series; axes are chosen automatically.

**readfile** -- read one file from the repository this lab studies, path relative to its root. Only available when the lab has a repository configured.

```tool:readfile
autoprof/daemon.py
```

**propose_patch** -- propose a change WITHOUT applying it. Supply a unified diff (`diff -u`). It is recorded as an artifact for a human to review. Use this when a change is too large or too risky to land automatically.

```tool:propose_patch
--- a/autoprof/daemon.py
+++ b/autoprof/daemon.py
@@ ...
```

**apply_patch** -- actually change the repository. Supply a unified diff. The patch is applied on the lab's own branch, the FULL TEST SUITE is run, and the change is then either committed (tests passed) or reverted completely (tests failed). You are told which.

This is real: a passing patch changes the system you are running inside. Therefore:
- Include tests for behaviour you add or change. Untested behaviour is behaviour nobody can verify later.
- Change one thing at a time. A large patch that fails tells you nothing about which part broke.
- If your patch is reverted, read the test output before retrying; resubmitting the same diff fails identically.
- Never weaken or delete a test to make a patch pass. That is how a system loses the ability to detect its own regressions.

```tool:apply_patch
--- a/autoprof/daemon.py
+++ b/autoprof/daemon.py
@@ ...
```

Rules:
- At most {max_calls} tool calls per response.
- A claim you verified computationally is far stronger at review than one you argued for. If a \
claim is finite and checkable, check it.
- If a verification CONTRADICTS what you believed, say so and follow the computation. Do not \
quietly keep the claim.
"""


class ToolError(RuntimeError):
    pass


def _repo_root() -> Path | None:
    import os

    root = os.environ.get(REPO_ROOT_ENV)
    return Path(root).resolve() if root else None


def run_readfile(body: str) -> dict:
    """Read a file from the lab's configured repository.

    Read-only and confined to the configured root: a research agent
    studying a codebase needs to see it, and needs to be unable to wander
    out of it. Resolves symlinks before checking containment, so a link
    pointing outside the tree is refused rather than followed.
    """
    root = _repo_root()
    if root is None:
        return {"status": "error", "output": f"(no repository configured; set {REPO_ROOT_ENV})"}

    rel = body.strip().splitlines()[0].strip() if body.strip() else ""
    if not rel:
        return {"status": "error", "output": "(give one path, relative to the repository root)"}

    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        return {"status": "error", "output": f"({rel}: outside the repository root)"}
    if not target.is_file():
        return {"status": "error", "output": f"({rel}: not a file)"}

    text = target.read_text(errors="replace")
    truncated = len(text) > READFILE_LIMIT
    return {
        "status": "ok",
        "output": text[:READFILE_LIMIT] + ("\n[... truncated ...]" if truncated else ""),
    }


def run_propose_patch(body: str) -> dict:
    """Record a proposed change to the repository. NEVER applies it.

    A research agent studying the system it runs inside must be able to
    propose improvements, and must not be able to edit the daemon
    executing it: a bad write to the job loop breaks the process
    mid-flight, and a self-modifying agent cannot be reviewed after the
    fact. So a patch is an artifact a human reads, applies and reverts --
    the proposal is the deliverable, not the mutation.
    """
    text = (body or "").strip()
    if not text:
        return {"status": "error", "output": "(empty patch)"}
    looks_like_diff = text.startswith(("diff ", "--- ", "+++ ", "@@")) or "\n@@" in text
    note = (
        "Recorded as a PROPOSAL. It has not been applied: a human reviews and applies patches, "
        "so nothing you write here can change the running system. Explain in your write-up what "
        "the change does and what evidence supports it."
    )
    if not looks_like_diff:
        note += (
            " NOTE: this does not look like a unified diff. Supply `diff -u` output so a reviewer "
            "can apply it directly."
        )
    return {"status": "ok", "output": note}


APPLY_BRANCH_ENV = "AUTOPROF_APPLY_BRANCH"
APPLY_TEST_COMMAND = ("./run_tests.sh",)
APPLY_TEST_TIMEOUT = 600


def _git(root: Path, *args, timeout: int = 120):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        timeout=timeout, stdin=subprocess.DEVNULL,
    )


def run_apply_patch(body: str) -> dict:
    """Apply a patch to the repository, verify it, and revert if it fails.

    Real self-improvement needs the change to actually land, but an agent
    editing the daemon that is executing it can break the process
    mid-flight. The resolution is the same one the recovery controller
    uses everywhere else in this system: perform the action, VERIFY the
    postcondition, and undo it when verification fails.

    Concretely: apply to a dedicated branch (never the default one), run
    the test suite, commit on success, and `git checkout` the working tree
    back on failure. A patch that breaks the tests therefore costs a test
    run and leaves no trace -- which is what makes "if anything breaks we
    fix it" true rather than aspirational.
    """
    root = _repo_root()
    if root is None:
        return {"status": "error", "output": f"(no repository configured; set {REPO_ROOT_ENV})"}

    diff = (body or "").strip()
    if not diff:
        return {"status": "error", "output": "(empty patch)"}
    if not diff.endswith("\n"):
        diff += "\n"

    import os

    branch = os.environ.get(APPLY_BRANCH_ENV, "auto-research")
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0:
        return {"status": "error", "output": f"(not a git repository: {head.stderr.strip()[:200]})"}
    current = head.stdout.strip()

    if current != branch:
        # Never touch whatever branch a human is on. Create or switch to
        # the lab's own branch first.
        switched = _git(root, "checkout", "-B", branch)
        if switched.returncode != 0:
            return {
                "status": "error",
                "output": f"(could not switch to branch {branch}: {switched.stderr.strip()[:300]})",
            }

    # Refuse on a dirty tree. Two ways this would otherwise destroy a
    # human's work: `git add -A` sweeps their uncommitted edits into the
    # lab's commit, and the revert path (`git checkout -- .`) discards
    # them outright. Neither is recoverable, so the tool declines instead.
    dirty = _git(root, "status", "--porcelain")
    if dirty.stdout.strip():
        changed = len(dirty.stdout.strip().splitlines())
        return {
            "status": "error",
            "output": f"(refusing to touch the repository: {changed} uncommitted change(s) "
                      "present. Applying here would sweep them into the commit, and reverting "
                      "would delete them. Commit or stash them first.)",
        }

    check = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=root, input=diff,
        capture_output=True, text=True, timeout=60,
    )
    if check.returncode != 0:
        return {
            "status": "error",
            "output": "(patch does not apply cleanly -- regenerate it against current file "
                      f"contents)\n{check.stderr.strip()[:800]}",
        }

    applied = subprocess.run(
        ["git", "apply", "-"], cwd=root, input=diff,
        capture_output=True, text=True, timeout=60,
    )
    if applied.returncode != 0:
        return {"status": "error", "output": f"(apply failed)\n{applied.stderr.strip()[:800]}"}

    try:
        tests = subprocess.run(
            list(APPLY_TEST_COMMAND), cwd=root, capture_output=True, text=True,
            timeout=APPLY_TEST_TIMEOUT, stdin=subprocess.DEVNULL,
        )
        passed = tests.returncode == 0
        tail = ((tests.stdout or "") + (tests.stderr or "")).strip()[-1500:]
    except subprocess.TimeoutExpired:
        passed, tail = False, f"(test suite exceeded {APPLY_TEST_TIMEOUT}s)"

    if not passed:
        # Undo cleanly. The tree returns to exactly its prior state, so a
        # failed experiment costs nothing but the test run.
        _git(root, "checkout", "--", ".")
        return {
            "status": "error",
            "output": "(patch APPLIED, tests FAILED, change REVERTED -- the repository is "
                      f"unchanged)\n{tail}",
        }

    _git(root, "add", "-A")
    committed = _git(
        root, "commit", "-m",
        "auto-research: apply patch proposed by the meta-lab\n\n"
        "Applied and verified by the test suite before committing; a failing\n"
        "patch is reverted automatically and never reaches a commit.",
    )
    if committed.returncode != 0:
        return {
            "status": "error",
            "output": f"(tests passed but commit failed)\n{committed.stderr.strip()[:400]}",
        }

    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {
        "status": "ok",
        "output": f"(patch applied, tests PASSED, committed {sha} on branch {branch})\n{tail[-600:]}",
    }


def parse_tool_calls(text: str) -> list[tuple[str, str]]:
    """Extract (tool, body) pairs from a model response, capped."""
    return [
        (match.group(1).lower(), match.group(2))
        for match in _TOOL_BLOCK_RE.finditer(text or "")
    ][:MAX_TOOL_CALLS_PER_ROUND]


def _limit_resources():  # pragma: no cover -- runs in the child process
    resource.setrlimit(resource.RLIMIT_AS, (VERIFY_MEMORY_BYTES, VERIFY_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_verifier(code: str, timeout: int = VERIFY_TIMEOUT_SECONDS) -> dict:
    """Execute a student's verification program and capture its output.

    Bounded rather than trusted: its own temp directory, wall-clock
    timeout, address-space cap, no core dumps, stdin closed. `-I` isolates
    it from the environment and from this repo's modules, so a program
    cannot reach into the lab database.

    Returns {status, output} -- never raises for a failing program, since
    a program that crashes is a legitimate (and informative) result the
    student should see.
    """
    with tempfile.TemporaryDirectory() as work_dir:
        script = Path(work_dir) / "verify.py"
        script.write_text(code)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                preexec_fn=_limit_resources,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "output": f"(no result: exceeded the {timeout}s limit -- reduce the search space)",
            }
        except OSError as e:
            return {"status": "error", "output": f"(could not run: {e})"}

    output = (proc.stdout or "")[:VERIFY_OUTPUT_LIMIT]
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[-2000:]
        return {"status": "error", "output": f"{output}\n--- program failed ---\n{stderr}".strip()}
    if not output.strip():
        return {"status": "error", "output": "(the program printed nothing -- print your result)"}
    return {"status": "ok", "output": output}


def _scale(values, lo, hi, out_lo, out_hi):
    if hi == lo:
        return [(out_lo + out_hi) / 2 for _ in values]
    return [out_lo + (v - lo) * (out_hi - out_lo) / (hi - lo) for v in values]


def render_chart(spec: dict) -> str:
    """Render a declarative spec as an inline SVG.

    Declarative on purpose. The rules that make a figure readable are
    applied here once -- fixed colour order, a single y-axis, every series
    directly labelled AND dash-coded so it survives greyscale printing,
    recessive axes -- instead of being restated in a prompt and followed
    inconsistently.
    """
    kind = str(spec.get("kind", "line")).lower()
    if kind not in ("line", "step", "scatter"):
        raise ToolError(f"unknown chart kind {kind!r}; use line, step or scatter")

    series = spec.get("series") or []
    if not series:
        raise ToolError("a chart needs at least one series")
    if len(series) > len(SERIES_COLOURS):
        raise ToolError(
            f"at most {len(SERIES_COLOURS)} series; combine or split into multiple figures"
        )

    cleaned = []
    for item in series:
        points = [
            (float(x), float(y))
            for x, y in (item.get("points") or [])
            if isinstance(x, (int, float)) and isinstance(y, (int, float))
        ]
        if not points:
            raise ToolError(f"series {item.get('name', '?')!r} has no usable points")
        cleaned.append({"name": str(item.get("name") or "series"), "points": sorted(points)})

    xs = [x for s in cleaned for x, _ in s["points"]]
    ys = [y for s in cleaned for _, y in s["points"]]
    x_lo, x_hi, y_lo, y_hi = min(xs), max(xs), min(ys), max(ys)
    # A y-axis that does not include 0 exaggerates differences; include it
    # unless the data genuinely lives far from it.
    if y_lo > 0 and y_lo < (y_hi - y_lo):
        y_lo = 0.0

    width, height = 620, 340
    left, right, top, bottom = 62, 150, 34, 46  # right margin holds direct labels
    plot_w, plot_h = width - left - right, height - top - bottom

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_esc(spec.get("title", "figure"))}">',
        '<g font-family="system-ui, sans-serif" font-size="11">',
        # Recessive axes only -- no grid, which competes with the data.
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
        'stroke="#666" stroke-width="1"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#666" stroke-width="1"/>',
    ]

    for frac in (0.0, 0.5, 1.0):
        y = top + plot_h - frac * plot_h
        out.append(
            f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" fill="#52514e">'
            f"{y_lo + frac * (y_hi - y_lo):.3g}</text>"
        )
        x = left + frac * plot_w
        out.append(
            f'<text x="{x}" y="{top + plot_h + 18}" text-anchor="middle" fill="#52514e">'
            f"{x_lo + frac * (x_hi - x_lo):.3g}</text>"
        )

    for index, item in enumerate(cleaned):
        colour = SERIES_COLOURS[index]
        dash = SERIES_DASHES[index]
        px = _scale([x for x, _ in item["points"]], x_lo, x_hi, left, left + plot_w)
        py = _scale([y for _, y in item["points"]], y_lo, y_hi, top + plot_h, top)

        if kind == "scatter":
            for x, y in zip(px, py):
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}"/>')
        else:
            coords = []
            for i, (x, y) in enumerate(zip(px, py)):
                if kind == "step" and i:
                    coords.append(f"L{x:.1f},{py[i - 1]:.1f}")
                coords.append(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}")
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            out.append(
                f'<path d="{" ".join(coords)}" fill="none" stroke="{colour}" '
                f'stroke-width="2"{dash_attr}/>'
            )

        # Direct label at the series end: identity never rests on colour.
        out.append(
            f'<text x="{left + plot_w + 8}" y="{py[-1] + 4:.1f}" fill="#52514e">'
            f"{_esc(item['name'])}</text>"
        )

    if spec.get("x_label"):
        out.append(
            f'<text x="{left + plot_w / 2}" y="{height - 8}" text-anchor="middle" '
            f'fill="#52514e">{_esc(spec["x_label"])}</text>'
        )
    if spec.get("y_label"):
        out.append(
            f'<text x="14" y="{top + plot_h / 2}" text-anchor="middle" fill="#52514e" '
            f'transform="rotate(-90 14 {top + plot_h / 2})">{_esc(spec["y_label"])}</text>'
        )
    if spec.get("title"):
        out.append(
            f'<text x="{left}" y="{top - 12}" font-size="12" font-weight="bold" fill="#0b0b0b">'
            f'{_esc(spec["title"])}</text>'
        )

    out.append("</g></svg>")
    return "\n".join(out)


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def run_visualizer(body: str) -> dict:
    try:
        spec = json.loads(body)
    except json.JSONDecodeError as e:
        return {"status": "error", "output": f"(chart spec is not valid JSON: {e})"}
    if not isinstance(spec, dict):
        return {"status": "error", "output": "(chart spec must be a JSON object)"}
    try:
        return {"status": "ok", "output": render_chart(spec)}
    except ToolError as e:
        return {"status": "error", "output": f"({e})"}


def execute_tool_calls(conn, calls, *, lab_id, task_id, student_id, lab_dir) -> str:
    """Run each call, record it, and format the results for the model.

    Returns the block to append to the next prompt, or "" when there were
    no calls -- so a caller can test whether another round is warranted.
    """
    from .artifacts import write_artifact

    if not calls:
        return ""

    sections = []
    for tool, body in calls:
        if tool == "verify":
            result = run_verifier(body)
        elif tool == "visualize":
            result = run_visualizer(body)
        elif tool == "readfile":
            result = run_readfile(body)
        elif tool == "propose_patch":
            result = run_propose_patch(body)
        else:
            result = run_apply_patch(body)

        cur = conn.execute(
            "INSERT INTO tool_runs (lab_id, task_id, student_id, tool, input_path, "
            "output_path, status, summary) VALUES (?, ?, ?, ?, 'pending', 'pending', ?, ?)",
            (lab_id, task_id, student_id, tool, result["status"], result["output"][:500]),
        )
        run_id = cur.lastrowid
        suffix = {"visualize": "svg", "propose_patch": "patch", "apply_patch": "patch"}.get(tool, "txt")
        if result["status"] != "ok":
            suffix = "txt"
        input_rel = f"{lab_id}/tools/{run_id}/input.txt"
        output_rel = f"{lab_id}/tools/{run_id}/output.{suffix}"
        conn.execute(
            "UPDATE tool_runs SET input_path = ?, output_path = ? WHERE id = ?",
            (input_rel, output_rel, run_id),
        )
        write_artifact(Path(lab_dir) / input_rel, body)
        write_artifact(
            Path(lab_dir) / output_rel,
            body if tool in ("propose_patch", "apply_patch") else result["output"],
        )
        conn.commit()

        sections.append(
            f"--- {tool} run #{run_id} [{result['status']}] ---\n{result['output']}"
        )

    return (
        "Results of the tools you called. Treat these as authoritative over your own "
        "expectations -- if a verification contradicts a claim you made, follow the computation "
        "and say what changed. An SVG returned by `visualize` can be pasted directly into your "
        "paper inside a <figure> element.\n"
        "<tool_results>\n" + "\n\n".join(sections) + "\n</tool_results>"
    )
