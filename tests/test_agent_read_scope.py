from pathlib import Path

from harness.agents.runner import _tools_for_agent


def test_read_scoped_agent_cannot_glob_dependency_tree(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.ts").write_text("export const app = true;\n", encoding="utf-8")
    dependency = tmp_path / "node_modules" / "package" / "index.ts"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("export const dependency = true;\n", encoding="utf-8")

    _tools, handlers = _tools_for_agent(
        ["glob", "read_file", "search_text"],
        cwd=tmp_path,
        read_roots=("src",),
    )

    assert handlers["glob"](pattern="src/**/*.ts").replace("\\", "/") == "src/app.ts"
    assert handlers["glob"](pattern="node_modules/**/*.ts").startswith("Glob blocked:")
    assert handlers["glob"](pattern="**/*").startswith("Glob blocked:")
    assert handlers["read_file"](path="node_modules/package/index.ts").startswith("Read blocked:")
