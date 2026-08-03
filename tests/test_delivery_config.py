from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_uses_importlib_mode_for_duplicate_test_basenames() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = project["tool"]["pytest"]["ini_options"]["addopts"]

    assert "--import-mode=importlib" in addopts


def test_autogen_dependencies_are_bounded() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert "autogen-agentchat>=0.7,<0.8" in project["dependencies"]
    assert "autogen-core>=0.7,<0.8" in project["dependencies"]


def test_frozen_dataset_fixtures_are_checked_out_with_lf_endings() -> None:
    attributes_file = ROOT / ".gitattributes"
    assert attributes_file.is_file(), "repository .gitattributes must define fixture EOLs"

    datasets = (
        ROOT / "evals" / "datasets" / "supportlab-v1",
        ROOT / "evals" / "datasets" / "supportlab-review-v1",
    )
    fixture_paths = sorted(
        path
        for dataset in datasets
        for pattern in ("*.jsonl", "*.json")
        for path in dataset.glob(pattern)
    )
    relative_paths = [path.relative_to(ROOT).as_posix() for path in fixture_paths]
    result = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", *relative_paths],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    expected = {
        f"{path}: {attribute}: {value}"
        for path in relative_paths
        for attribute, value in (("text", "set"), ("eol", "lf"))
    }
    assert set(result.stdout.splitlines()) == expected


def test_phase_2_delivery_is_safe_and_reproducible() -> None:
    required_files = (".env.example", "README.md")
    missing = [path for path in required_files if not (ROOT / path).is_file()]
    assert not missing, f"missing Phase 2 delivery files: {missing}"

    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"(?m)^DEEPSEEK_API_KEY=$", environment)
    assert re.search(r"(?m)^DEEPSEEK_MODEL=deepseek-v4-flash$", environment)
    assert re.search(r"(?m)^SPANVOUCH_API_KEY=$", environment)
    assert re.search(
        r"(?m)^SPANVOUCH_AUDIT_SIGNING_KEY_PATH=.data/audit-signing-key.pem$",
        environment,
    )
    assert re.search(
        r"(?m)^SPANVOUCH_AUDIT_EXPORT_DIR=.data/audit-exports$",
        environment,
    )

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "evals/reports/generated/" in gitignore

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "spanvouch evaluate diagnosis" in readme
    assert "--allow-live-api" in readme
    assert "POST /v1/traces/{trace_id}/diagnoses" in readme
    assert "rules" in readme and "DEEPSEEK_API_KEY" in readme


def test_phase_1_delivery_configuration_is_reproducible() -> None:
    required_files = (
        ".dockerignore",
        ".python-version",
        "Dockerfile",
        "compose.yaml",
        ".github/workflows/ci.yml",
        "README.md",
    )
    missing = [path for path in required_files if not (ROOT / path).is_file()]
    assert not missing, f"missing Phase 1 delivery files: {missing}"

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "arizephoenix/phoenix:latest" not in compose
    assert re.search(r"arizephoenix/phoenix@sha256:[0-9a-f]{64}", compose)

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Verify frozen dataset hashes" in workflow
    assert "docker compose config --quiet" in workflow


def test_release_handoff_command_is_documented_and_gated_in_ci() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    command = "spanvouch release verify --repo-root . --expected-version"

    assert command in readme
    assert command in readme_zh
    assert command in contributing
    assert "version=\"$(python -c" in workflow
    assert "tomllib" in workflow
    assert (
        "uv run --no-sync spanvouch release verify --repo-root . "
        '--expected-version "$version"'
    ) in workflow
    assert workflow.index("uv sync --frozen --group dev --no-install-project") < workflow.index(
        command
    )
    assert workflow.index(command) < workflow.index("uv run --no-sync ruff check src tests")


def test_api_image_is_immutable_unprivileged_and_minimal() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base_images = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.MULTILINE)

    assert len(base_images) >= 3
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in base_images)
    assert any(image.startswith("python:3.12") for image in base_images)
    assert any(image.startswith("ghcr.io/astral-sh/uv:0.8.15@") for image in base_images)

    runtime = dockerfile.rsplit("FROM ", maxsplit=1)[1]
    assert "USER 10001:10001" in runtime
    assert "--chown=10001:10001" in runtime
    assert "/opt/venv/bin/uvicorn" in runtime
    assert "COPY src" not in runtime
    assert "COPY --from=ghcr.io" not in runtime


def test_phase_3_sqlite_data_directory_is_owned_and_persisted() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.rsplit("FROM ", maxsplit=1)[1]
    user_offset = runtime.index("USER 10001:10001")

    assert "mkdir -p /app /data" in runtime[:user_offset]
    assert "chown 10001:10001 /app /data" in runtime[:user_offset]

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    api_block, _, volumes_block = compose.partition("  phoenix:")
    assert re.search(r"(?m)^\s+SPANVOUCH_DB_PATH:\s*/data/spanvouch\.db$", api_block)
    assert re.search(r"(?m)^\s+- spanvouch_data:/data$", api_block)
    assert re.search(r"(?m)^\s{2}spanvouch_data:\s*$", volumes_block)

    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"(?m)^SPANVOUCH_DB_PATH=\.data/spanvouch\.db$", environment)

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\.data/$", gitignore)
    assert re.search(r"(?m)^evals/reports/generated/$", gitignore)


def test_docker_build_context_excludes_local_secrets_and_runtime_data() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = set(dockerignore.splitlines())

    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns
    assert ".data/" in patterns
    assert ".cache/" in patterns
    assert "evals/reports/generated/" in patterns


def test_phase_3_ci_regenerates_reviews_and_proves_restart_recovery() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    required_fragments = (
        "spanvouch dataset generate-review",
        "Verify frozen review dataset hashes",
        "spanvouch evaluate review --output .cache/ci-review-a.json",
        "spanvouch evaluate review --output .cache/ci-review-b.json",
        "cmp --silent .cache/ci-review-a.json .cache/ci-review-b.json",
        "docker compose restart api",
        "spanvouch review show",
        "spanvouch review decide",
        'id -u):$(id -g)" = "10001:10001',
        "stat -c %u:%g /data",
        "docker compose down --volumes --remove-orphans",
    )
    missing = [fragment for fragment in required_fragments if fragment not in workflow]
    assert not missing, f"missing Phase 3 CI persistence gates: {missing}"

    assert "DEEPSEEK_API_KEY" not in workflow
    assert "--allow-live-api" not in workflow

    cleanup = workflow.split("          cleanup() {", maxsplit=1)[1].split(
        "          trap cleanup EXIT", maxsplit=1
    )[0]
    failure_branch = cleanup.split('if [ "$status" -ne 0 ]; then', maxsplit=1)[1].split(
        "            fi", maxsplit=1
    )[0]
    assert "docker compose logs --no-color api" in failure_branch
    assert "docker compose down --remove-orphans || true" in cleanup
    assert "docker compose down --volumes" not in cleanup
    assert 'exit "$status"' in cleanup

    trap = workflow.index("          trap cleanup EXIT")
    final_audit_assertion = workflow.index(
        'assert [event["event_sequence"] for event in payload["events"]]'
    )
    trap_disabled = workflow.index("          trap - EXIT")
    destructive_cleanup = workflow.index(
        "          docker compose down --volumes --remove-orphans"
    )
    destructive_line = workflow[destructive_cleanup:].splitlines()[0]
    assert trap < final_audit_assertion < trap_disabled < destructive_cleanup
    assert "|| true" not in destructive_line
    assert workflow.count("docker compose down --volumes --remove-orphans") == 1


def test_readme_offline_review_walkthrough_uses_a_frozen_trace_end_to_end() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_fragments = (
        "evals/datasets/supportlab-v1/traces.jsonl",
        "POST /v1/traces",
        "Authorization: Bearer $SPANVOUCH_API_KEY",
        'trace_id="$(',
        "--data-binary @.cache/spanvouch-demo-trace.json",
        'created="$(uv run spanvouch review create',
        'case_id="$(python',
        'version="$(python',
        'uv run spanvouch review show --case-id "$case_id"',
        '--expected-version "$version"',
    )
    missing = [fragment for fragment in required_fragments if fragment not in readme]
    assert not missing, f"README offline walkthrough is not reproducible: {missing}"


def test_python_patch_is_shared_by_ci_and_docker() -> None:
    version_file = ROOT / ".python-version"
    assert version_file.is_file()
    python_version = version_file.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"3\.12\.\d+", python_version)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    docker_python_versions = re.findall(
        r"^FROM\s+python:([^\s@-]+)-slim@sha256:", dockerfile, flags=re.MULTILINE
    )
    assert docker_python_versions
    assert set(docker_python_versions) == {python_version}

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'python-version-file: ".python-version"' in workflow
    assert not re.search(r"^\s+python-version:\s*", workflow, flags=re.MULTILINE)


def test_api_build_backend_is_fully_hash_locked() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_system = pyproject["build-system"]
    backend_distribution = build_system["build-backend"].partition(".")[0]
    build_requirements = build_system["requires"]
    assert len(build_requirements) == 1
    backend_requirement = build_requirements[0]
    assert re.fullmatch(rf"{re.escape(backend_distribution)}==[^=<>!~\s]+", backend_requirement)

    constraints_input = (ROOT / "build-constraints.in").read_text(encoding="utf-8").strip()
    assert constraints_input == backend_requirement

    constraints_path = ROOT / "build-constraints.txt"
    assert constraints_path.is_file()

    constraints = constraints_path.read_text(encoding="utf-8")
    requirement_starts = list(re.finditer(r"(?m)^[a-zA-Z0-9_.-]+==[^\s\\]+", constraints))
    assert requirement_starts
    assert requirement_starts[0].group() == backend_requirement

    for index, match in enumerate(requirement_starts):
        end = requirement_starts[index + 1].start() if index + 1 < len(requirement_starts) else None
        requirement_block = constraints[match.start() : end]
        assert "--hash=sha256:" in requirement_block

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compact_dockerfile = " ".join(dockerfile.split())
    assert "COPY pyproject.toml uv.lock README.md build-constraints.txt ./" in compact_dockerfile
    assert (
        "uv build --wheel --build-constraints build-constraints.txt --require-hashes --no-cache"
        in compact_dockerfile
    )
    assert "uv sync --frozen --no-dev --no-install-project --no-cache" in compact_dockerfile
    assert (
        "uv pip install --python /opt/venv/bin/python --no-deps --no-cache dist/*.whl"
        in compact_dockerfile
    )


def test_compose_defines_executable_phoenix_healthcheck() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "http://localhost:6006/healthz" in compose
    assert compose.count("healthcheck:") == 2


def test_ci_pins_platform_inputs_and_smoke_tests_api_image() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

    assert "runs-on: ubuntu-24.04" in workflow
    assert action_lines
    assert all(re.search(r"@[0-9a-f]{40}\s+#\s+v\d", line) for line in action_lines)
    assert "docker compose build api" in workflow
    assert "docker compose up --detach --wait --wait-timeout" in workflow
    assert "curl --fail" in workflow
    assert "docker compose logs --no-color api" in workflow
    assert "docker compose down --volumes --remove-orphans" in workflow


def test_ci_builds_and_installs_the_hash_constrained_wheel_before_checks() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    run_commands = re.findall(r"^\s+- run:\s+(.+)$", workflow, flags=re.MULTILINE)

    protected_install = [
        "uv sync --frozen --group dev --no-install-project",
        "uv build --wheel --build-constraints build-constraints.txt --require-hashes --no-cache",
        "uv pip install --python .venv/bin/python --no-deps --no-cache dist/*.whl",
    ]
    assert all(command in run_commands for command in protected_install)
    protected_indices = [run_commands.index(command) for command in protected_install]
    assert protected_indices == sorted(protected_indices)

    project_commands = [command for command in run_commands if command.startswith("uv run ")]
    assert project_commands
    assert protected_indices[-1] < min(run_commands.index(command) for command in project_commands)
    assert all(command.startswith("uv run --no-sync ") for command in project_commands)


def test_active_delivery_configuration_has_only_spanvouch_public_names() -> None:
    legacy_product = "AF" + "C"
    legacy_import = legacy_product.lower()
    forbidden_literals = (
        f"{legacy_product}_",
        f"{legacy_import}-",
        f"{legacy_import}_data",
        f"src/{legacy_import}",
        f"from {legacy_import}",
        f"import {legacy_import}",
        "Agent Failure" + " Clinic",
        "agent-failure" + "-clinic",
    )
    active_paths = (
        ROOT / "src",
        ROOT / ".github" / "workflows",
        ROOT / "pyproject.toml",
        ROOT / "Dockerfile",
        ROOT / "compose.yaml",
        ROOT / ".env.example",
        ROOT / "README.md",
    )
    files = sorted(
        path
        for active_path in active_paths
        for path in ([active_path] if active_path.is_file() else active_path.rglob("*"))
        if path.is_file()
        and path.suffix
        in {"", ".example", ".md", ".py", ".toml", ".yaml", ".yml"}
    )
    active_text = "\n".join(path.read_text(encoding="utf-8") for path in files)

    active_hits = {literal for literal in forbidden_literals if literal in active_text}
    assert active_hits == set()

    test_files = sorted((ROOT / "tests").rglob("*.py"))
    test_text_by_path = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in test_files
    }
    test_hits = {
        (relative_path, literal)
        for relative_path, test_text in test_text_by_path.items()
        for literal in forbidden_literals
        if literal in test_text
    }
    assert test_hits == set()

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"] == {
        "spanvouch": "spanvouch.cli.main:main"
    }


def test_phase_4_release_candidate_documents_delivery_and_six_contract_roots() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    docker_runtime = (ROOT / "Dockerfile").read_text(encoding="utf-8").rsplit(
        "FROM ", maxsplit=1
    )[1]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    legacy_environment = "AF" + "C_DB_PATH"

    assert project["project"]["name"] == "spanvouch"
    assert project["project"]["version"] == "0.6.0"
    assert project["project"]["scripts"] == {"spanvouch": "spanvouch.cli.main:main"}
    assert "SPANVOUCH_DB_PATH" in compose
    assert "SPANVOUCH_AUDIT_SIGNING_KEY_PATH" in compose
    assert "SPANVOUCH_AUDIT_EXPORT_DIR" in compose
    assert legacy_environment not in compose
    assert "name: spanvouch" in compose
    assert "10001:10001" in docker_runtime
    assert 'org.opencontainers.image.title="SpanVouch"' in dockerfile
    assert "SPANVOUCH_BUILD_GIT_COMMIT" in dockerfile
    assert "/app/uv.lock" in docker_runtime
    assert "evals/reports/reference/" in dockerignore
    assert "tests/contracts tests/architecture tests/test_delivery_config.py -v" in workflow
    assert "git diff --exit-code -- evals/datasets" in workflow
    assert "spanvouch admin bootstrap --database /data/spanvouch.db" in workflow
    assert "Authorization: Bearer $SPANVOUCH_API_KEY" in workflow
    assert "source_connection.backup(destination_connection)" in workflow
    assert "spanvouch admin audit export" in workflow
    assert "spanvouch admin audit verify" in workflow
    assert "SPANVOUCH_BUILD_GIT_COMMIT: ${{ github.sha }}" in workflow
    assert 'actual["candidates_sha256"] == expected["candidates_sha256"]' not in workflow
    assert "IVAD" in readme
    assert "spanvouch admin project create" in readme
    assert "SPANVOUCH_API_KEY" in readme
    assert "spanvouch admin audit verify" in readme
    assert "简体中文" in readme_zh
    assert "项目隔离" in readme_zh
    assert "SPANVOUCH_API_KEY" in readme_zh
    assert "spanvouch admin audit verify" in readme_zh
    assert not (ROOT / "docs").exists()

    expected_roots = {
        "artifact-manifest",
        "diagnosis",
        "diagnostic-context",
        "review",
        "trace",
        "verification",
    }
    schema_roots = {
        path.name.removeprefix("spanvouch.").removesuffix("-1.0.schema.json")
        for path in (ROOT / "schemas" / "v1").glob("spanvouch.*-1.0.schema.json")
    }
    fixture_roots = {
        path.name.removesuffix(".valid.json")
        for path in (ROOT / "tests" / "contracts" / "fixtures" / "v1").glob("*.valid.json")
    }
    assert schema_roots == expected_roots
    assert fixture_roots == expected_roots

    assert (ROOT / "schemas" / "v1").is_dir()
    assert (ROOT / "evals" / "datasets").is_dir()


def test_phase_5_ci_enforces_coverage_and_explicit_offline_acceptance_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert (
        "uv run --no-sync pytest --cov=spanvouch --cov-report=term-missing "
        "--cov-fail-under=91"
    ) in workflow
    assert (
        "uv run pytest --cov=spanvouch --cov-report=term-missing "
        "--cov-fail-under=91"
    ) in contributing
    assert (
        "uv run --no-sync pytest tests/architecture/test_phase5_boundaries.py -v"
    ) in workflow
    assert (
        "uv run --no-sync pytest tests/evaluation/test_phase5_offline_e2e.py -v"
    ) in workflow
    assert "${{ secrets." not in workflow


def test_phase_4_acceptance_evidence_includes_a_clean_offline_reference_bundle() -> None:
    bundle = ROOT / "evals" / "reports" / "reference" / "phase4-offline-bundle"
    required_bundle_files = (
        "README.md",
        "config.json",
        "environment.txt",
        "manifest.json",
        "metrics.json",
        "structured-events.jsonl",
    )
    missing = [name for name in required_bundle_files if not (bundle / name).is_file()]
    assert not missing, f"incomplete Phase 4 reference bundle: {missing}"
    assert hashlib.sha256((bundle / "metrics.json").read_bytes()).hexdigest() == (
        "c3fc1f4fc2015cbc0a3d6691bf01da98e1a77f7ac139d37f7e75cd2038742f9b"
    )

    assert (bundle / "README.md").is_file()


def test_phase_4_reference_bundle_limits_label_boundaries_to_metrics_analysis() -> None:
    bundle = ROOT / "evals" / "reports" / "reference" / "phase4-offline-bundle"
    bundle_text = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(bundle.iterdir())
        if path.is_file()
    }

    assert '"mutation_kind"' in bundle_text["metrics.json"]
    assert all(
        '"mutation_kind"' not in content
        for name, content in bundle_text.items()
        if name != "metrics.json"
    )
    forbidden_label_keys = ('"gold', '"expected', '"split')
    assert all(
        key not in content
        for content in bundle_text.values()
        for key in forbidden_label_keys
    )

    assert (ROOT / "README.md").is_file()


def test_active_old_product_name_scan_has_no_literal_hits() -> None:
    legacy_product = "AF" + "C"
    legacy_import = legacy_product.lower()
    patterns = (
        f"{legacy_product}_",
        f"{legacy_import}-",
        f"src/{legacy_import}",
        f"from {legacy_import}",
        f"import {legacy_import}",
        "Agent Failure" + " Clinic",
    )
    paths = (
        ROOT / "src",
        ROOT / "tests",
        ROOT / "pyproject.toml",
        ROOT / "Dockerfile",
        ROOT / "compose.yaml",
        ROOT / ".env.example",
        ROOT / "README.md",
        ROOT / ".github",
    )
    files = (
        path
        for root in paths
        for path in ([root] if root.is_file() else root.rglob("*"))
        if path.is_file()
        and path.suffix in {"", ".example", ".md", ".py", ".toml", ".yaml", ".yml"}
    )
    hits = {
        (path.relative_to(ROOT).as_posix(), pattern)
        for path in files
        for pattern in patterns
        if pattern in path.read_text(encoding="utf-8")
    }
    assert hits == set()
