import subprocess

from harness.goal.discovery import (
    DISCOVERY_MAX_ROUNDS,
    DiscoverySupervisor,
    _assigned_paths,
    _parse_report,
    build_repo_map,
    iter_readable_files,
)
from harness.goal.discovery_models import DiscoveryJob, DiscoveryManifest, Evidence
from harness.goal.discovery_store import load_job_states, load_manifest, save_job_state, save_manifest


def test_repo_map_excludes_secrets_builds_and_binary_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("import os\n\ndef run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")

    files = iter_readable_files(tmp_path)
    shards = build_repo_map(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["src/app.py"]
    assert shards[0].symbols == ("run",)
    assert shards[0].imports == ("os",)
    assert len(shards[0].sha256) == 64


def test_repo_map_respects_gitignore_but_allows_explicit_ignored_source_path(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("generated/\n*.cache\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "generated").mkdir()
    (tmp_path / "src" / "app.py").write_text("def run(): pass\n", encoding="utf-8")
    (tmp_path / "generated" / "snapshot.py").write_text("def generated(): pass\n", encoding="utf-8")
    (tmp_path / "notes.cache").write_text("local output", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")

    automatic = iter_readable_files(tmp_path)
    explicit = iter_readable_files(tmp_path, paths=("generated/snapshot.py", ".env"))

    assert [path.relative_to(tmp_path).as_posix() for path in automatic] == ["src/app.py"]
    assert [path.relative_to(tmp_path).as_posix() for path in explicit] == ["generated/snapshot.py"]
    assert [shard.path for shard in build_repo_map(tmp_path)] == ["src/app.py"]

    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        path = kwargs["read_paths"][0]
        return (
            '{"evidence":[{"path":"%s","lines":[1,1],'
            '"excerpt":"def generated(): pass","claim":"generated source exists"}],"gaps":[]}'
        ) % path

    DiscoverySupervisor(runner=fake_runner).run(
        goal_id="goal-ignored-file",
        target="fix generated/snapshot.py",
        workspace=tmp_path,
        roles=("implementation",),
    )

    assert calls[0]["read_paths"] == ("generated/snapshot.py",)


def test_discovery_store_writes_jobs_and_manifest_atomically(tmp_path):
    save_job_state(tmp_path, "goal-1", {"id": "job-1", "status": "done"})
    save_manifest(tmp_path, "goal-1", DiscoveryManifest(
        goal_id="goal-1", base_revision="abc", evidence=(Evidence("E1", "src/app.py", "hash", "runs"),)
    ).to_dict())

    assert load_job_states(tmp_path, "goal-1") == [{"id": "job-1", "status": "done"}]
    manifest = load_manifest(tmp_path, "goal-1")
    assert manifest["evidence"][0]["id"] == "E1"


def test_discovery_supervisor_merges_only_assigned_hashed_evidence(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        path = kwargs["read_paths"][0]
        return '{"evidence":[{"path":"%s","lines":[1,2],"excerpt":"def run():\\n    return 1","claim":"entry exists"},{"path":"secret.py","lines":[1,2],"excerpt":"bad","claim":"reject"}],"gaps":[]}' % path

    manifest = DiscoverySupervisor(runner=fake_runner, concurrency=2).run(
        goal_id="goal-1", target="improve app", workspace=tmp_path, roles=("implementation",)
    )

    assert len(calls) == 1
    assert calls[0]["tools_override"] == ("read_file",)
    assert calls[0]["max_rounds"] == DISCOVERY_MAX_ROUNDS
    assert manifest.evidence[0].path == "app.py"
    assert len(manifest.evidence[0].sha256) == 64
    assert all(item.path != "secret.py" for item in manifest.evidence)


def test_discovery_derives_excerpt_when_compact_report_omits_it(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    manifest = DiscoverySupervisor(
        runner=lambda **kwargs: (
            '{"evidence":[{"path":"app.py","lines":[1,2],"claim":"entry exists"}],"gaps":[]}'
        )
    ).run(goal_id="goal-compact", target="improve app", workspace=tmp_path, roles=("implementation",))

    assert manifest.evidence[0].path == "app.py"
    assert manifest.evidence[0].excerpt_sha256


def test_discovery_retries_an_invalid_json_report_with_compact_format(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return "I found the entry point but forgot the required JSON format."
        return '{"evidence":[{"path":"app.py","lines":[1,2],"claim":"entry exists"}],"gaps":[]}'

    manifest = DiscoverySupervisor(runner=fake_runner).run(
        goal_id="goal-retry", target="improve app", workspace=tmp_path, roles=("architecture",)
    )

    assert len(calls) == 2
    assert calls[1]["description"] == "repair architecture discovery report format"
    assert calls[1]["max_rounds"] == 1
    assert manifest.jobs[0].status == "done"
    assert manifest.evidence[0].path == "app.py"


def test_discovery_extends_a_round_slice_when_it_reads_new_assigned_files(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["stats"].llm_rounds = DISCOVERY_MAX_ROUNDS
            kwargs["stats"].stop_reason = "max_rounds"
            kwargs["stats"].read_paths.append("app.py")
            return "still reading"
        return '{"evidence":[{"path":"app.py","lines":[1,2],"claim":"entry exists"}],"gaps":[]}'

    manifest = DiscoverySupervisor(runner=fake_runner).run(
        goal_id="goal-continued", target="improve app", workspace=tmp_path, roles=("implementation",)
    )

    assert len(calls) == 2
    assert calls[1]["description"] == "continue implementation discovery (slice 2)"
    assert manifest.jobs[0].slices == 2
    assert manifest.jobs[0].read_paths_seen == ("app.py",)


def test_discovery_continues_max_tokens_with_the_same_conversation(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["stats"].stop_reason = "max_tokens"
            kwargs["stats"].read_paths.append("app.py")
            return "partial evidence report"
        return '{"evidence":[{"path":"app.py","lines":[1,2],"claim":"entry exists"}],"gaps":[]}'

    manifest = DiscoverySupervisor(runner=fake_runner).run(
        goal_id="goal-token-continued",
        target="improve app",
        workspace=tmp_path,
        roles=("implementation",),
    )

    assert len(calls) == 2
    assert calls[0]["conversation"] is calls[1]["conversation"]
    assert calls[1]["prompt"].startswith("Continue the same discovery conversation")
    assert manifest.jobs[0].slices == 2
    assert manifest.evidence[0].path == "app.py"


def test_discovery_runs_requirement_then_architecture_with_handoff(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "INPUT.md").write_text("Update src/app.py\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("export const enabled = true;\n", encoding="utf-8")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        role = kwargs["agent_type"].removeprefix("goal_discovery_")
        if role == "requirement":
            return ('{"evidence":[{"path":"docs/INPUT.md","lines":[1,1],'
                    '"claim":"the requested behavior targets the app"}],"gaps":[]}')
        if role == "architecture":
            assert "docs/INPUT.md: the requested behavior targets the app" in kwargs["prompt"]
            return ('{"evidence":[{"path":"src/app.py","lines":[1,1],'
                    '"claim":"the app has one implementation module"}],"gaps":[]}')
        return '{"evidence":[],"gaps":[]}'

    manifest = DiscoverySupervisor(runner=fake_runner, concurrency=2).run(
        goal_id="goal-pipeline", target="implement docs/INPUT.md", workspace=tmp_path,
        roles=("requirement", "architecture", "implementation", "tests"),
    )

    roles = [call["agent_type"].removeprefix("goal_discovery_") for call in calls]
    assert roles[:2] == ["requirement", "architecture"]
    assert {item.path for item in manifest.evidence} == {"docs/INPUT.md", "src/app.py"}


def test_discovery_event_exposes_bounded_read_only_work_metadata(monkeypatch):
    import harness.goal.discovery as discovery
    import harness.ui.events as events

    captured = []
    monkeypatch.setattr(events, "is_enabled", lambda: True)
    monkeypatch.setattr(events, "emit", lambda event_type, **payload: captured.append((event_type, payload)))
    job = DiscoveryJob(
        id="implementation-1", role="implementation",
        read_paths=tuple(f"src/module_{index}.py" for index in range(14)),
        started_at=123.0,
    )

    discovery._emit_discovery_job("goal-1", job, event="started")

    event_type, payload = captured[0]
    assert event_type == "goal_discovery_job"
    assert payload["tools"] == ["read_file"]
    assert payload["read_path_count"] == 14
    assert payload["read_paths"] == [f"src/module_{index}.py" for index in range(12)]
    assert payload["started_at"] == 123.0


def test_discovery_scopes_files_to_the_goal_and_ignores_generated_worktrees(tmp_path):
    (tmp_path / "node_tui" / "docs").mkdir(parents=True)
    (tmp_path / "node_tui" / "src-open").mkdir()
    (tmp_path / "node_tui" / "test").mkdir()
    (tmp_path / ".worktrees" / "old" / "tests").mkdir(parents=True)
    (tmp_path / ".task_outputs").mkdir()
    (tmp_path / "node_tui" / "docs" / "INPUT.md").write_text("requirements", encoding="utf-8")
    (tmp_path / "node_tui" / "src-open" / "App.tsx").write_text("export {}", encoding="utf-8")
    (tmp_path / "node_tui" / "test" / "input.test.ts").write_text("test", encoding="utf-8")
    (tmp_path / ".worktrees" / "old" / "tests" / "stale_test.py").write_text("test", encoding="utf-8")
    (tmp_path / ".task_outputs" / "test_result.txt").write_text("old", encoding="utf-8")

    shards = build_repo_map(tmp_path)
    target = "implement node_tui/docs/INPUT.md"

    test_paths = _assigned_paths("tests", shards, target=target)
    implementation_paths = _assigned_paths("implementation", shards, target=target)

    assert "node_tui/test/input.test.ts" in test_paths
    assert all(not path.startswith((".worktrees/", ".task_outputs/")) for path in (*test_paths, *implementation_paths))
    assert "node_tui/src-open/App.tsx" in implementation_paths


def test_discovery_follows_requirement_references_and_typescript_imports(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src-open").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "INPUT.md").write_text(
        "Change src-open/App.tsx and src/autocomplete.ts", encoding="utf-8"
    )
    (tmp_path / "src-open" / "App.tsx").write_text(
        "import { footerHint } from './interaction.js';\nimport { startBackend } from '../src/backend.js';\n",
        encoding="utf-8",
    )
    (tmp_path / "src-open" / "interaction.ts").write_text("export const footerHint = () => '';\n", encoding="utf-8")
    (tmp_path / "src" / "autocomplete.ts").write_text("export const complete = () => [];\n", encoding="utf-8")
    (tmp_path / "src" / "backend.ts").write_text("export const startBackend = () => {};\n", encoding="utf-8")

    paths = _assigned_paths("implementation", build_repo_map(tmp_path), target="implement docs/INPUT.md")

    assert {"docs/INPUT.md", "src-open/App.tsx", "src-open/interaction.ts", "src/autocomplete.ts", "src/backend.ts"}.issubset(paths)


def test_discovery_report_parser_finds_valid_json_after_non_json_prefix():
    raw = 'analysis with {not-json} before a fenced report\n```json\n{"evidence": [], "gaps": ["missing test"]}\n```'

    assert _parse_report(raw) == {"evidence": [], "gaps": ["missing test"]}


def test_discovery_records_failed_role_as_a_manifest_gap(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    def fake_runner(**kwargs):
        if kwargs["agent_type"].endswith("tests"):
            return "not a JSON report"
        path = kwargs["read_paths"][0]
        return '{"evidence":[{"path":"%s","lines":[1,2],"excerpt":"def run():\\n    return 1","claim":"entry exists"}],"gaps":[]}' % path

    manifest = DiscoverySupervisor(runner=fake_runner).run(
        goal_id="goal-1", target="improve app", workspace=tmp_path, roles=("implementation", "tests")
    )

    assert any("tests discovery unavailable" in gap for gap in manifest.gaps)
