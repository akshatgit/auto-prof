import json
import unittest

from autoprof.backends.ollama_cloud import OllamaCloudBackend


def fake_http_ok(response_text="hello from ollama", model="gpt-oss:120b"):
    def http_call(url, headers, body, timeout):
        return 200, {}, json.dumps({"response": response_text, "model": model}).encode()

    return http_call


class OllamaCloudBackendTests(unittest.TestCase):
    def test_missing_api_key_is_an_error_not_a_network_call(self):
        called = {"n": 0}

        def http_call(*a, **kw):
            called["n"] += 1
            return 200, {}, b"{}"

        backend = OllamaCloudBackend(api_key=None, http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.is_error)
        self.assertIn("OLLAMA_API_KEY", result.error)
        self.assertEqual(called["n"], 0, "must not attempt the HTTP call without a key")

    def test_successful_call_returns_text_and_model(self):
        backend = OllamaCloudBackend(api_key="k", http_call=fake_http_ok("42 is the answer"))
        result = backend.run("what is the answer")
        self.assertEqual(result.text, "42 is the answer")
        self.assertEqual(result.model_version, "gpt-oss:120b")
        self.assertFalse(result.is_error)

    def test_sends_bearer_auth_header_and_json_body(self):
        captured = {}

        def http_call(url, headers, body, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body)
            return 200, {}, json.dumps({"response": "ok", "model": "m"}).encode()

        backend = OllamaCloudBackend(api_key="secret-key", model="llama3.1:405b", http_call=http_call)
        backend.run("prompt text")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(captured["body"]["model"], "llama3.1:405b")
        self.assertEqual(captured["body"]["prompt"], "prompt text")
        self.assertIn("ollama.com", captured["url"])

    def test_model_override_via_opts(self):
        captured = {}

        def http_call(url, headers, body, timeout):
            captured["body"] = json.loads(body)
            return 200, {}, json.dumps({"response": "ok", "model": "override-model"}).encode()

        backend = OllamaCloudBackend(api_key="k", model="default-model", http_call=http_call)
        backend.run("hi", model="override-model")
        self.assertEqual(captured["body"]["model"], "override-model")

    def test_429_sets_rate_limited(self):
        def http_call(url, headers, body, timeout):
            return 429, {"Retry-After": "20"}, b""

        backend = OllamaCloudBackend(api_key="k", http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.rate_limited)
        self.assertFalse(result.is_error)
        self.assertEqual(result.retry_after_seconds, 20.0)

    def test_429_without_retry_after_header_still_flagged(self):
        def http_call(url, headers, body, timeout):
            return 429, {}, b""

        backend = OllamaCloudBackend(api_key="k", http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.rate_limited)
        self.assertIsNone(result.retry_after_seconds)

    def test_other_4xx_5xx_is_a_hard_error(self):
        def http_call(url, headers, body, timeout):
            return 500, {}, b"internal error"

        backend = OllamaCloudBackend(api_key="k", http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.is_error)
        self.assertFalse(result.rate_limited)
        self.assertIn("500", result.error)

    def test_non_json_response_is_a_hard_error_not_a_crash(self):
        def http_call(url, headers, body, timeout):
            return 200, {}, b"not json at all"

        backend = OllamaCloudBackend(api_key="k", http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.is_error)

    def test_timeout_reported_as_error(self):
        def http_call(url, headers, body, timeout):
            raise TimeoutError("timed out")

        backend = OllamaCloudBackend(api_key="k", http_call=http_call, timeout=1)
        result = backend.run("hi")
        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.error.lower())

    def test_connection_error_reported_as_error_not_raised(self):
        def http_call(url, headers, body, timeout):
            raise OSError("connection refused")

        backend = OllamaCloudBackend(api_key="k", http_call=http_call)
        result = backend.run("hi")
        self.assertTrue(result.is_error)

    def test_api_key_read_from_env_when_not_passed(self):
        import os

        os.environ["OLLAMA_API_KEY"] = "from-env"
        try:
            backend = OllamaCloudBackend(http_call=fake_http_ok())
            result = backend.run("hi")
            self.assertFalse(result.is_error)
        finally:
            del os.environ["OLLAMA_API_KEY"]

    def test_backend_name(self):
        self.assertEqual(OllamaCloudBackend(api_key="k").name, "ollama_cloud")


if __name__ == "__main__":
    unittest.main()


class StreamingProgressTests(unittest.TestCase):
    """An ollama job must be able to show that it is working."""

    def _backend(self, stream_lines):
        b = OllamaCloudBackend(api_key="k", model="m")

        def fake_stream(url, headers, body, timeout, on_chunk):
            for line in stream_lines:
                on_chunk(line + "\n")
            return 200, {}, ("\n".join(stream_lines)).encode()

        b.stream_call = fake_stream
        b.http_call = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("non-streaming path used despite a progress callback"))
        return b

    def test_streams_when_a_progress_callback_is_given(self):
        seen = []
        lines = [json.dumps({"model": "m", "response": "Hel", "done": False}),
                 json.dumps({"model": "m", "response": "lo", "done": False}),
                 json.dumps({"model": "m", "response": "", "done": True,
                             "eval_count": 12, "prompt_eval_count": 300})]
        r = self._backend(lines).run("p", on_progress=lambda s, c, n: seen.append(c))
        self.assertEqual(r.text, "Hello")
        self.assertEqual(len(seen), 3)

    def test_request_body_asks_for_streaming_only_when_watched(self):
        captured = {}

        def fake_stream(url, headers, body, timeout, on_chunk):
            captured["body"] = json.loads(body)
            on_chunk(json.dumps({"response": "x", "done": True, "eval_count": 1}))
            return 200, {}, json.dumps({"response": "x", "done": True}).encode()

        b = OllamaCloudBackend(api_key="k", model="m")
        b.stream_call = fake_stream
        b.run("p", on_progress=lambda *a: None)
        self.assertTrue(captured["body"]["stream"])

    def test_without_a_callback_it_stays_non_streaming(self):
        captured = {}

        def fake_http(url, headers, body, timeout):
            captured["body"] = json.loads(body)
            return 200, {}, json.dumps({"response": "ok", "model": "m"}).encode()

        b = OllamaCloudBackend(api_key="k", model="m")
        b.http_call = fake_http
        r = b.run("p")
        self.assertFalse(captured["body"]["stream"])
        self.assertEqual(r.text, "ok")

    def test_a_broken_callback_does_not_fail_the_job(self):
        lines = [json.dumps({"response": "a", "done": True, "eval_count": 2})]
        b = self._backend(lines)
        r = b.run("p", on_progress=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(r.text, "a")
