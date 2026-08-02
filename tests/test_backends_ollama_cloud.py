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
