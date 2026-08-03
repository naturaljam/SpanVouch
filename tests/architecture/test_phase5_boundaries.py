from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from spanvouch.evaluation.artifacts import require_safe_artifact_content
from spanvouch.evaluation.experiments.provider import RequestIdentity
from spanvouch.evaluation.provider_view import ProviderVisibleVerificationInput

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "spanvouch"
CORE_ROOTS = (
    "contracts",
    "trace",
    "diagnosis",
    "verification",
    "review",
    "invariants",
)
STAGE_A_PATHS = (
    SOURCE_ROOT / "evaluation" / "corpus" / "inventory.py",
    SOURCE_ROOT / "evaluation" / "corpus" / "generate.py",
    SOURCE_ROOT / "evaluation" / "run_phase5_corpus.py",
)
FORBIDDEN_STAGE_A = (
    "spanvouch.evaluation.corpus.gold_specs",
    "spanvouch.evaluation.corpus.labels",
    "spanvouch.evaluation.experiments.provider",
    "spanvouch.evaluation.statistics",
    "spanvouch.evaluation.provider_view",
)
FORBIDDEN_PROVIDER_FIELDS = {
    "gold",
    "gold_label",
    "label",
    "expected_failure",
    "expected_critical_operation",
    "split",
    "raw_body",
    "raw_response",
    "hidden_reasoning",
}
FORBIDDEN_ARTIFACT_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "hidden_reasoning",
    "messages",
    "password",
    "prompt",
    "raw_body",
    "raw_response",
    "response_body",
}
SECRET_VALUE = re.compile(
    r"(?:bearer\s+\S+|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _module_for_from(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = ("spanvouch", *path.relative_to(SOURCE_ROOT).parent.parts)
    retained = package[: len(package) - (node.level - 1)]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*retained, *suffix))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _module_for_from(path, node)
            if module:
                modules.add(module)
                modules.update(f"{module}.{alias.name}" for alias in node.names)
    return modules


def _matches_any(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == item or module.startswith(f"{item}.") for item in prefixes)


def _forbidden_imports(paths: tuple[Path, ...], prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for path in paths:
        matched = sorted(module for module in _imports(path) if _matches_any(module, prefixes))
        if matched:
            violations[path.relative_to(SOURCE_ROOT).as_posix()] = matched
    return violations


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key).lower() for key in value),
            *(item for child in value.values() for item in _walk_keys(child)),
        }
    if isinstance(value, list):
        return {item for child in value for item in _walk_keys(child)}
    return set()


def test_production_core_cannot_import_phase5_labs_or_evaluation() -> None:
    paths = tuple(
        path
        for root in CORE_ROOTS
        for path in sorted((SOURCE_ROOT / root).rglob("*.py"))
    )
    assert _forbidden_imports(paths, ("spanvouch.labs", "spanvouch.evaluation")) == {}


def test_stage_a_cannot_import_labels_providers_statistics_or_provider_views() -> None:
    assert all(path.is_file() for path in STAGE_A_PATHS)
    assert _forbidden_imports(STAGE_A_PATHS, FORBIDDEN_STAGE_A) == {}


def test_provider_runner_cannot_reference_or_open_sealed_labels() -> None:
    provider = SOURCE_ROOT / "evaluation" / "experiments" / "provider.py"
    assert _forbidden_imports(
        (provider,),
        (
            "spanvouch.evaluation.corpus.gold_specs",
            "spanvouch.evaluation.corpus.labels",
            "spanvouch.evaluation.diagnosis_labels",
            "spanvouch.evaluation.review_labels",
        ),
    ) == {}
    tree = ast.parse(provider.read_text(encoding="utf-8"), filename=str(provider))
    identifiers = {
        node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    string_literals = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not {item for item in identifiers if "label" in item or "gold" in item}
    assert not {
        item for item in string_literals if "labels-sealed" in item or "gold-label" in item
    }


def test_post_call_evaluators_cannot_import_provider_execution() -> None:
    paths = [SOURCE_ROOT / "evaluation" / "statistics" / "metrics.py"]
    analysis = SOURCE_ROOT / "evaluation" / "run_phase5_analysis.py"
    if analysis.exists():
        paths.append(analysis)
    assert _forbidden_imports(
        tuple(paths),
        (
            "spanvouch.adapters.models",
            "spanvouch.evaluation.experiments.provider",
        ),
    ) == {}


def test_only_framework_wrappers_import_langgraph_or_autogen() -> None:
    allowed = {
        "labs/frameworks/autogen.py",
        "labs/frameworks/langgraph.py",
    }
    violations: dict[str, list[str]] = {}
    for path in sorted((SOURCE_ROOT / "labs").rglob("*.py")):
        external = sorted(
            module
            for module in _imports(path)
            if module == "langgraph"
            or module.startswith("langgraph.")
            or module == "autogen_agentchat"
            or module.startswith("autogen_agentchat.")
            or module == "autogen_core"
            or module.startswith("autogen_core.")
        )
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if external and relative not in allowed:
            violations[relative] = external
    assert violations == {}


def test_stage_b_runner_imports_neither_agent_framework() -> None:
    runner = SOURCE_ROOT / "evaluation" / "experiments" / "conditions.py"
    assert _forbidden_imports(
        (runner,),
        ("langgraph", "autogen_agentchat", "autogen_core", "spanvouch.labs.frameworks"),
    ) == {}


def test_provider_visible_models_exclude_gold_and_raw_fields() -> None:
    assert set(ProviderVisibleVerificationInput.model_fields) == {
        "diagnosis",
        "diagnosis_sha256",
        "verifier_contract",
    }
    visible_fields = {
        *(field.lower() for field in ProviderVisibleVerificationInput.model_fields),
        *(field.lower() for field in RequestIdentity.model_fields),
    }
    assert visible_fields.isdisjoint(FORBIDDEN_PROVIDER_FIELDS)


def test_checked_in_reference_artifacts_exclude_secrets_and_raw_model_content() -> None:
    reference_root = ROOT / "evals" / "reports" / "reference"
    assert reference_root.is_dir()
    for path in sorted(item for item in reference_root.rglob("*") if item.is_file()):
        text = path.read_text(encoding="utf-8")
        assert SECRET_VALUE.search(text) is None, path.relative_to(ROOT)
        if "phase5-formal-deepseek-only" in path.parts:
            assert "raw_provider_payload" not in text.lower(), path.relative_to(ROOT)
            if path.suffix == ".json":
                keys = _walk_keys(json.loads(text))
                assert keys.isdisjoint(FORBIDDEN_ARTIFACT_KEYS), path.relative_to(ROOT)
            continue
        if path.name == "manifest.json":
            payload: Any = json.loads(text)
            location = "manifest"
        elif path.name == "config.json":
            payload = json.loads(text)
            location = "config"
        elif path.name == "metrics.json":
            payload = json.loads(text)
            location = "metrics"
        elif path.name == "structured-events.jsonl":
            payload = tuple(json.loads(line) for line in text.splitlines() if line)
            location = "structured_events"
        elif path.name == "environment.txt":
            payload = dict(line.split("=", maxsplit=1) for line in text.splitlines())
            location = "environment"
        else:
            payload = text
            location = "readme"
        try:
            require_safe_artifact_content(location, payload)
        except ValueError as exc:
            raise AssertionError(path.relative_to(ROOT)) from exc
        if path.suffix == ".json":
            keys = _walk_keys(payload)
            assert keys.isdisjoint(FORBIDDEN_ARTIFACT_KEYS), path.relative_to(ROOT)
        elif path.suffix == ".jsonl":
            keys = _walk_keys(list(payload))
            assert keys.isdisjoint(FORBIDDEN_ARTIFACT_KEYS), path.relative_to(ROOT)


def test_ci_has_offline_phase5_gate_and_preserves_build_jobs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    phase5_name = "Verify offline Phase 5 architecture and security boundaries"
    phase5_command = "pytest tests/architecture/test_phase5_boundaries.py -v"
    assert phase5_name in workflow
    assert phase5_command in workflow
    assert workflow.index(phase5_name) > workflow.index("pytest --cov=spanvouch")
    assert "uv build --wheel" in workflow
    assert "docker compose build api" in workflow
    for forbidden in (
        "DEEPSEEK_API_KEY",
        "--allow-live-api",
        "vllm",
        "experiments run",
        "rent gpu",
    ):
        assert forbidden.lower() not in workflow.lower()


def test_environment_template_exposes_only_managed_qwen_runtime() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    for required in (
        "SPANVOUCH_QWEN_BASE_URL=",
        "SPANVOUCH_QWEN_API_KEY=",
        "SPANVOUCH_PHASE5_QWEN_PRICING_PATH=",
    ):
        assert required in environment
    for forbidden in ("SPANVOUCH_VLLM_", "SPANVOUCH_PHASE5_GPU_LEASE_PATH"):
        assert forbidden not in environment


def test_phase5_public_snapshot_states_engineering_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for required in (
        "supportlab",
        "opslab",
        "langgraph",
        "autogen",
        "offline",
        "commercial deployment",
        "provider",
    ):
        assert required in readme
