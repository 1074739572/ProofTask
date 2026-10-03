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


def test_exact_read_paths_allow_inspection_without_directory_escape(tmp_path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    other = tmp_path / "src" / "secret.py"
    other.write_text("TOKEN = 'hidden'\n", encoding="utf-8")

    tool_defs, handlers = _tools_for_agent(
        ["read_file", "search_text", "glob"], cwd=tmp_path,
        read_paths=("src/app.py",), read_roots=(),
    )

    assert {item["name"] for item in tool_defs} == {"read_file", "search_text", "glob"}
    assert "return 1" in handlers["read_file"](path="src/app.py")
    assert "run" in handlers["search_text"](pattern="run", path="src/app.py")
    assert handlers["read_file"](path="src/secret.py").startswith("Read blocked:")
    assert handlers["search_text"](pattern="TOKEN", path="src").startswith("Search blocked:")
    assert handlers["glob"](pattern="src/*.py").startswith("Glob blocked:")
