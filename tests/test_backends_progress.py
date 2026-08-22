import json
import unittest

from autoprof.backends.progress import Progress


class ProgressTests(unittest.TestCase):
    """Token counts are the signal that a long job is really working."""

    def _codex_stream(self):
        return [
            '{"type":"thread.started","thread_id":"abc"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}',
            '{"type":"turn.completed","usage":{"input_tokens":14635,'
            '"cached_input_tokens":11008,"output_tokens":5,"reasoning_output_tokens":7}}',
        ]

    def test_counts_tokens_items_and_turns(self):
        p = Progress()
        for line in self._codex_stream():
            p.feed(line)
        self.assertEqual(p.items, 1)
        self.assertEqual(p.turns, 1)
        self.assertEqual(p.output_tokens, 5)
        self.assertEqual(p.reasoning_tokens, 7)
        self.assertEqual(p.produced_tokens, 12)
        self.assertEqual(p.last_event, "turn.completed")

    def test_usage_accumulates_across_turns(self):
        p = Progress()
        for _ in range(3):
            p.feed('{"type":"turn.completed","usage":{"output_tokens":10,'
                   '"reasoning_output_tokens":5,"input_tokens":100}}')
        self.assertEqual(p.produced_tokens, 45)
        self.assertEqual(p.turns, 3)
        self.assertEqual(p.input_tokens, 300)

    def test_non_json_output_still_counts_as_liveness(self):
        p = Progress()
        p.feed("plain progress text\n")
        self.assertEqual(p.lines, 1)
        self.assertGreater(p.bytes_seen, 0)
        self.assertEqual(p.produced_tokens, 0)

    def test_malformed_json_is_ignored(self):
        p = Progress()
        p.feed('{"type":"turn.completed", TRUNCATED')
        self.assertEqual(p.turns, 0)
        self.assertEqual(p.lines, 1)

    def test_missing_usage_block_does_not_crash(self):
        p = Progress()
        p.feed('{"type":"turn.completed"}')
        self.assertEqual(p.turns, 1)
        self.assertEqual(p.produced_tokens, 0)

    def test_summary_and_metadata_shapes(self):
        p = Progress()
        for line in self._codex_stream():
            p.feed(line)
        self.assertIn("12 tokens", p.summary())
        md = p.as_metadata()
        self.assertEqual(md["produced_tokens"], 12)
        self.assertEqual(md["items"], 1)


class OllamaStreamTests(unittest.TestCase):
    """Ollama reports continuously; codex only at turn close."""

    def test_token_batches_count_as_work(self):
        p = Progress()
        for frag in ("Hel", "lo", " there"):
            p.feed(json.dumps({"model": "m", "response": frag, "done": False}))
        self.assertEqual(p.items, 3)
        self.assertEqual(p.produced_tokens, 0)   # counters arrive at the end

    def test_final_object_supplies_the_counters(self):
        p = Progress()
        p.feed(json.dumps({"response": "x", "done": False}))
        p.feed(json.dumps({"response": "", "done": True,
                           "eval_count": 128, "prompt_eval_count": 4096}))
        self.assertEqual(p.produced_tokens, 128)
        self.assertEqual(p.input_tokens, 4096)
        self.assertEqual(p.turns, 1)

    def test_codex_events_are_unaffected(self):
        p = Progress()
        p.feed('{"type":"item.completed","item":{}}')
        p.feed('{"type":"turn.completed","usage":{"output_tokens":7}}')
        self.assertEqual(p.items, 1)
        self.assertEqual(p.produced_tokens, 7)

    def test_empty_response_fragments_are_not_counted_as_work(self):
        p = Progress()
        p.feed(json.dumps({"response": "", "done": False}))
        self.assertEqual(p.items, 0)
