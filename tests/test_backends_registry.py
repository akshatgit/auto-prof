import tempfile
import unittest
from pathlib import Path

from autoprof.backends import registry
from autoprof.backends.registry import (
    DEFAULT_BACKEND_FOR_CATEGORY,
    Registry,
    classify_kind,
    load_config,
    resolve_backend_name,
)


class ClassifyKindTests(unittest.TestCase):
    def test_review_kinds(self):
        self.assertEqual(classify_kind("paper_review"), "review")
        self.assertEqual(classify_kind("defense_review"), "review")

    def test_generation_kinds(self):
        for kind in ("professor_decompose", "student_work", "professor_callback", "memory_compact"):
            self.assertEqual(classify_kind(kind), "generation")

    def test_lab_review_is_a_review_kind(self):
        self.assertEqual(classify_kind("lab_review"), "review")

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            classify_kind("not_a_real_kind")


class ResolveBackendNameTests(unittest.TestCase):
    def test_hardcoded_fallback_matches_the_goal_defaults(self):
        # per the /goal directive: codex + ollama cloud as the backends,
        # review isolation stays on codex, generation defaults to ollama cloud.
        self.assertEqual(resolve_backend_name("student_work", {}, {}), "ollama_cloud")
        self.assertEqual(resolve_backend_name("paper_review", {}, {}), "codex")

    def test_category_default_from_config_wins_over_hardcoded_fallback(self):
        config = {"backends": {"default": {"generation": "codex"}}}
        self.assertEqual(resolve_backend_name("student_work", config, {}), "codex")

    def test_per_kind_config_override_wins_over_category_default(self):
        config = {
            "backends": {
                "default": {"generation": "codex"},
                "overrides": {"student_work": "ollama_cloud"},
            }
        }
        self.assertEqual(resolve_backend_name("student_work", config, {}), "ollama_cloud")
        # sibling kind still gets the category default, not the override
        self.assertEqual(resolve_backend_name("professor_decompose", config, {}), "codex")

    def test_category_env_override_wins_over_config_default(self):
        config = {"backends": {"default": {"generation": "codex"}}}
        env = {"AUTOPROF_GENERATION_BACKEND": "ollama_cloud"}
        self.assertEqual(resolve_backend_name("student_work", config, env), "ollama_cloud")

    def test_per_kind_env_override_wins_over_everything(self):
        config = {
            "backends": {
                "default": {"generation": "codex"},
                "overrides": {"student_work": "codex"},
            }
        }
        env = {
            "AUTOPROF_GENERATION_BACKEND": "codex",
            "AUTOPROF_BACKEND_STUDENT_WORK": "ollama_cloud",
        }
        self.assertEqual(resolve_backend_name("student_work", config, env), "ollama_cloud")

    def test_defaults_table_documents_both_categories(self):
        self.assertEqual(DEFAULT_BACKEND_FOR_CATEGORY["generation"], "ollama_cloud")
        self.assertEqual(DEFAULT_BACKEND_FOR_CATEGORY["review"], "codex")


class LoadConfigTests(unittest.TestCase):
    def test_missing_path_returns_empty_dict(self):
        self.assertEqual(load_config(None), {})
        self.assertEqual(load_config(Path("/nonexistent/autoprof.toml")), {})

    def test_parses_real_toml_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "autoprof.toml"
            path.write_text(
                "[backends.default]\n"
                "generation = \"codex\"\n"
                "review = \"codex\"\n"
                "\n"
                "[backends.overrides]\n"
                "student_work = \"ollama_cloud\"\n"
            )
            config = load_config(path)
            self.assertEqual(config["backends"]["default"]["generation"], "codex")
            self.assertEqual(config["backends"]["overrides"]["student_work"], "ollama_cloud")


class FakeBackend:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run(self, prompt, **opts):
        raise NotImplementedError


class AnotherFakeBackend(FakeBackend):
    pass


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.fake_classes = {"codex": FakeBackend, "ollama_cloud": AnotherFakeBackend}

    def test_returns_backend_instance_for_kind(self):
        registry = Registry(config={}, env={}, backend_classes=self.fake_classes)
        backend = registry.get_backend("paper_review")
        self.assertIsInstance(backend, FakeBackend)

    def test_generation_kind_gets_ollama_cloud_by_default(self):
        registry = Registry(config={}, env={}, backend_classes=self.fake_classes)
        backend = registry.get_backend("student_work")
        self.assertIsInstance(backend, AnotherFakeBackend)

    def test_caches_backend_instances_per_name(self):
        registry = Registry(config={}, env={}, backend_classes=self.fake_classes)
        b1 = registry.get_backend("paper_review")
        b2 = registry.get_backend("defense_review")
        self.assertIs(b1, b2, "both are 'codex' -- should be the same cached instance")

    def test_unknown_backend_name_raises_clear_error(self):
        config = {"backends": {"overrides": {"student_work": "nonexistent_backend"}}}
        registry = Registry(config=config, env={}, backend_classes=self.fake_classes)
        with self.assertRaises(ValueError) as ctx:
            registry.get_backend("student_work")
        self.assertIn("nonexistent_backend", str(ctx.exception))

    def test_unknown_job_kind_raises(self):
        registry = Registry(config={}, env={}, backend_classes=self.fake_classes)
        with self.assertRaises(ValueError):
            registry.get_backend("not_a_real_kind")


if __name__ == "__main__":
    unittest.main()


class OllamaModelSelectionTests(unittest.TestCase):
    """The model was a hardcoded class default, so the model that wrote
    every paper in this system was invisible to the config and could not
    be changed without editing source."""

    def test_defaults_to_the_class_default_when_unconfigured(self):
        self.assertEqual(registry.backend_options("ollama_cloud", {}, {}), {})

    def test_config_selects_the_model(self):
        opts = registry.backend_options(
            "ollama_cloud", {"backends": {"ollama_model": "deepseek-v4-pro"}}, {}
        )
        self.assertEqual(opts, {"model": "deepseek-v4-pro"})

    def test_env_beats_config(self):
        opts = registry.backend_options(
            "ollama_cloud",
            {"backends": {"ollama_model": "from-config"}},
            {"AUTOPROF_OLLAMA_MODEL": "from-env"},
        )
        self.assertEqual(opts, {"model": "from-env"})

    def test_other_backends_take_no_model_kwarg(self):
        # codex and claude take no `model=` in this position; passing one
        # would raise at construction.
        self.assertEqual(
            registry.backend_options("codex", {"backends": {"ollama_model": "x"}}, {}), {}
        )

    def test_registry_threads_the_model_into_the_instance(self):
        reg = registry.Registry(config={"backends": {"ollama_model": "deepseek-v4-pro"}}, env={})
        self.assertEqual(reg.get_backend("student_work").model, "deepseek-v4-pro")

    def test_timeout_is_configurable(self):
        # 280s was a hardcoded constructor default. When generation moved
        # to a slower model the longest jobs exceeded it on EVERY attempt,
        # so all five retries hit the same wall and the task stranded --
        # retries cannot rescue work that is simply longer than a fixed
        # limit.
        opts = registry.backend_options(
            "ollama_cloud", {"backends": {"ollama_timeout": 900}}, {}
        )
        self.assertEqual(opts["timeout"], 900.0)

    def test_env_timeout_wins_and_is_left_to_the_backend(self):
        opts = registry.backend_options(
            "ollama_cloud",
            {"backends": {"ollama_timeout": 900}},
            {"AUTOPROF_OLLAMA_TIMEOUT": "1200"},
        )
        self.assertNotIn("timeout", opts)   # the backend reads the env var itself

    def test_a_bad_timeout_value_falls_back_rather_than_raising(self):
        opts = registry.backend_options(
            "ollama_cloud", {"backends": {"ollama_timeout": "not-a-number"}}, {}
        )
        self.assertNotIn("timeout", opts)
