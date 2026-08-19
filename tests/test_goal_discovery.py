from harness.goal.discovery import build_repo_map, iter_readable_files
from harness.goal.discovery_models import DiscoveryManifest, Evidence
from harness.goal.discovery_store import load_job_states, load_manifest, save_job_state, save_manifest
from harness.goal.discovery import DiscoverySupervisor


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
    assert manifest.evidence[0].path == "app.py"
    assert len(manifest.evidence[0].sha256) == 64
    assert all(item.path != "secret.py" for item in manifest.evidence)
