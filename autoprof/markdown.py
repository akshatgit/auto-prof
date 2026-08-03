"""A small, safe Markdown renderer for the web UI.

Stdlib only, like the rest of this project -- the web UI has no
dependencies and adding one for formatting would be a poor trade.

Two properties matter more than completeness:

- **Escape first, format second.** Every character of agent-written text
  is HTML-escaped before any markup is generated, so no review, paper
  title or root problem can inject markup into the page. The renderer
  only ever *adds* tags to already-safe text.
- **Math is protected from Markdown.** These documents are full of LaTeX,
  and the two languages collide: `x_1 \\cdot x_2` would come out with the
  underscores eaten as emphasis, and `*` inside a formula would open an
  italic span. Math spans are lifted out before Markdown runs and put
  back afterwards, untouched, for MathJax to typeset.
"""

import html
import re

_MATH_PATTERNS = (
    re.compile(r"\\\[.*?\\\]", re.DOTALL),   # display  \[ ... \]
    re.compile(r"\$\$.*?\$\$", re.DOTALL),   # display  $$ ... $$
    re.compile(r"\\\(.*?\\\)", re.DOTALL),   # inline   \( ... \)
)

_PLACEHOLDER = "\x00MATH{}\x00"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
# Matched AFTER escaping, so the marker is already `&gt;`.
_QUOTE_RE = re.compile(r"^\s*&gt;\s?(.*)$")

# Inline spans, applied after escaping. `code` first so its contents are
# not then treated as emphasis.
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _protect_math(text: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(0))
        return _PLACEHOLDER.format(len(spans) - 1)

    for pattern in _MATH_PATTERNS:
        text = pattern.sub(stash, text)
    return text, spans


def _restore_math(text: str, spans: list[str]) -> str:
    for index, span in enumerate(spans):
        # Math is already escaped along with everything else; MathJax reads
        # the DOM text, so &lt; and friends resolve before it sees them.
        text = text.replace(_PLACEHOLDER.format(index), span)
    return text


def _inline(text: str) -> str:
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    # Links are rebuilt from escaped text, and only http(s) ever matched,
    # so javascript: and data: URLs cannot appear here.
    text = _LINK_RE.sub(r'<a href="\2" rel="noopener noreferrer">\1</a>', text)
    return text


def render(text: str) -> str:
    """Render Markdown-with-LaTeX to HTML that is safe to embed."""
    if not text:
        return ""

    protected, math_spans = _protect_math(text)
    escaped = html.escape(protected)

    out: list[str] = []
    list_stack: list[str] = []   # 'ul' / 'ol' currently open
    paragraph: list[str] = []
    in_code = False
    code_buffer: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    for raw_line in escaped.splitlines():
        line = raw_line.rstrip()

        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + "\n".join(code_buffer) + "</code></pre>")
                code_buffer.clear()
                in_code = False
            else:
                close_paragraph()
                close_lists()
                in_code = True
            continue
        if in_code:
            code_buffer.append(line)
            continue

        if not line.strip():
            close_paragraph()
            close_lists()
            continue

        if _HR_RE.match(line):
            close_paragraph()
            close_lists()
            out.append("<hr>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            close_paragraph()
            close_lists()
            level = min(len(heading.group(1)) + 1, 6)  # never emit <h1>; the page owns that
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            close_paragraph()
            close_lists()
            out.append("<blockquote>" + _inline(quote.group(1)) + "</blockquote>")
            continue

        bullet = _UL_RE.match(line)
        numbered = _OL_RE.match(line)
        if bullet or numbered:
            close_paragraph()
            want = "ul" if bullet else "ol"
            if list_stack and list_stack[-1] != want:
                out.append(f"</{list_stack.pop()}>")
            if not list_stack:
                out.append(f"<{want}>")
                list_stack.append(want)
            out.append("<li>" + _inline((bullet or numbered).group(1)) + "</li>")
            continue

        close_lists()
        paragraph.append(line.strip())

    if in_code and code_buffer:
        out.append("<pre><code>" + "\n".join(code_buffer) + "</code></pre>")
    close_paragraph()
    close_lists()

    return _restore_math("\n".join(out), math_spans)
