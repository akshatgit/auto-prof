import unittest

from autoprof.backends.base import Backend, BackendResult


class BackendResultTests(unittest.TestCase):
    def test_defaults(self):
        r = BackendResult(text="hello")
        self.assertEqual(r.text, "hello")
        self.assertIsNone(r.model_version)
        self.assertFalse(r.rate_limited)
        self.assertIsNone(r.retry_after_seconds)
        self.assertIsNone(r.error)
        self.assertFalse(r.is_error)

    def test_error_result_flags_is_error(self):
        r = BackendResult(text="", error="boom")
        self.assertTrue(r.is_error)

    def test_rate_limited_result(self):
        r = BackendResult(text="", rate_limited=True, retry_after_seconds=30.0)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.retry_after_seconds, 30.0)
        # a rate-limited result is not the same thing as a hard error --
        # per docs/DESIGN.md §5.1/§5.3, rate limiting must never be
        # conflated with a genuine execution failure.
        self.assertFalse(r.is_error)


class FakeBackend(Backend):
    """Minimal concrete Backend for testing the ABC contract itself."""

    name = "fake"

    def run(self, prompt: str, **opts) -> BackendResult:
        return BackendResult(text=f"echo: {prompt}", model_version="fake-1")


class BackendContractTests(unittest.TestCase):
    def test_cannot_instantiate_abstract_backend_directly(self):
        with self.assertRaises(TypeError):
            Backend()

    def test_concrete_backend_runs(self):
        backend = FakeBackend()
        result = backend.run("hi")
        self.assertEqual(result.text, "echo: hi")
        self.assertEqual(result.model_version, "fake-1")

    def test_backend_has_a_name(self):
        self.assertEqual(FakeBackend().name, "fake")


if __name__ == "__main__":
    unittest.main()
