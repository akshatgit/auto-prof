"""Tests for the web UI's Markdown renderer."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import markdown  # noqa: E402


class SafetyTests(unittest.TestCase):
    """Every character rendered here was written by a model. The renderer
    only ever ADDS tags to already-escaped text."""

    def test_html_in_source_is_escaped(self):
        out = markdown.render("A <script>alert(1)</script> review")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_img_onerror_cannot_be_injected(self):
        out = markdown.render('<img src=x onerror="steal()">')
        self.assertNotIn("<img", out)

    def test_only_http_links_become_anchors(self):
        """A javascript: link is left as inert text -- never an anchor."""
        out = markdown.render("[click](javascript:alert(1)) and [ok](https://example.com)")
        self.assertNotIn("href=\"javascript:", out)
        self.assertNotIn("<a href=\"j", out)
        self.assertIn('href="https://example.com"', out)

    def test_links_carry_noopener(self):
        self.assertIn("noopener", markdown.render("[x](https://example.com)"))


class MathProtectionTests(unittest.TestCase):
    """Markdown and LaTeX collide: underscores are subscripts in one and
    emphasis in the other."""

    def test_subscripts_are_not_eaten_as_emphasis(self):
        out = markdown.render(r"the value \(x_1 + x_2\) is fixed")
        self.assertIn(r"\(x_1 + x_2\)", out)
        self.assertNotIn("<em>", out)

    def test_asterisks_inside_math_do_not_open_italics(self):
        out = markdown.render(r"\(a * b * c\) holds")
        self.assertIn(r"\(a * b * c\)", out)
        self.assertNotIn("<em>", out)

    def test_display_math_survives_intact(self):
        for src in (r"\[ \frac{a}{b} \]", "$$ \\sum_{i=1}^n x_i $$"):
            self.assertIn(src.strip(), markdown.render(f"before\n\n{src}\n\nafter"))

    def test_multiline_display_math_is_preserved(self):
        src = "\\[\n  R_r(\\varepsilon) = 1\n\\]"
        self.assertIn(src, markdown.render(f"text\n\n{src}\n\ntext"))


class StructureTests(unittest.TestCase):
    def test_headings_never_emit_h1(self):
        """The page owns its own <h1>; a document heading must not compete."""
        out = markdown.render("# Top\n\n## Second")
        self.assertNotIn("<h1>", out)
        self.assertIn("<h2>Top</h2>", out)
        self.assertIn("<h3>Second</h3>", out)

    def test_bold_and_inline_code(self):
        out = markdown.render("**Novelty.** uses `verify` here")
        self.assertIn("<strong>Novelty.</strong>", out)
        self.assertIn("<code>verify</code>", out)

    def test_bullet_and_numbered_lists(self):
        out = markdown.render("- one\n- two")
        self.assertIn("<ul>", out)
        self.assertEqual(out.count("<li>"), 2)
        numbered = markdown.render("1. first\n2. second")
        self.assertIn("<ol>", numbered)

    def test_list_type_switches_cleanly(self):
        out = markdown.render("- bullet\n\n1. number")
        self.assertIn("<ul>", out)
        self.assertIn("<ol>", out)
        self.assertEqual(out.count("</ul>"), 1)

    def test_paragraphs_are_separated(self):
        out = markdown.render("first para\n\nsecond para")
        self.assertEqual(out.count("<p>"), 2)

    def test_code_block_contents_are_not_formatted(self):
        out = markdown.render("```\n**not bold** and _not italic_\n```")
        self.assertIn("<pre><code>", out)
        self.assertNotIn("<strong>", out)

    def test_blockquote_and_rule(self):
        out = markdown.render("> quoted\n\n---")
        self.assertIn("<blockquote>", out)
        self.assertIn("<hr>", out)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(markdown.render(""), "")
        self.assertEqual(markdown.render(None), "")


class RealReviewTests(unittest.TestCase):
    def test_a_verdict_line_survives(self):
        out = markdown.render(
            "**Correctness.** Step 3 fails.\n\nVERDICT: strong_accept\n"
        )
        self.assertIn("VERDICT: strong_accept", out)


if __name__ == "__main__":
    unittest.main()
