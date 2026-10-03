"""RAG 统一目录迁移与运行时读写的回归测试。

覆盖五个验收场景（打开项目工作区 = ``harness.settings.switch_workspace``，
数据创建/读取经 RAG 实际读写入口 ``harness.rag.ingest`` / ``harness.rag.lexical`` /
``harness.rag.assets`` / ``harness.rag.selection``）：

1. AC1 新项目工作区没有历史 RAG 数据：获取索引、资源、分块和清单路径并经实际
   入口创建数据后，路径与新增数据均归属当前项目 ``.project`` 下的 RAG 位置，
   不创建根目录旧 ``.rag``。
2. AC2 根目录旧 ``.rag`` 存在历史数据且统一位置对应项为空：打开项目后历史数据
   经迁移继续可读，新增数据只写入统一目录。
3. AC3 旧 ``.rag`` 与统一位置存在同名但内容不同的项：常规读取始终采用统一位置
   内容，旧内容保持不变且可显式读取，重开不覆盖或复制冲突项。
4. AC4 旧 ``.rag`` 部分项已发布而另一项发布失败：未发布项仍可通过实际入口读取，
   旧数据不被回退写入修改，新写入只进统一目录，重试恢复未发布项并保留已发布内容。
5. AC5 两个项目各有不同 RAG 数据且 RAG 模块已加载：切换活跃项目后所有路径、读取
   与写入跟随当前项目且互不混用，不因模块级路径缓存访问旧项目，也不把项目选择
   写回静态配置。

统一目录的具体内部子布局沿用现有格式（chunks / index / assets / manifest），
本测试只断言其归属 ``<root>/.project`` 之下，不绑定具体子目录命名。
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from harness import settings as settings_mod
from harness.rag import assets as assets_mod  # noqa: F401 - 资源读写入口同属被测边界
from harness.rag import config as rag_config
from harness.rag import ingest as ingest_mod
from harness.rag import lexical as lexical_mod

#: 旧布局：工作区根目录平铺的 ``.rag``（chunks / index / assets / manifest.json）。
LEGACY_RAG_DIRNAME = ".rag"
LEGACY_CHUNKS_DIRNAME = "chunks"
LEGACY_MANIFEST_NAME = "manifest.json"

#: 本测试涉及的 RAG 配置路径名（索引 / 资源 / 分块 / 清单），沿用现有配置符号。
CONFIG_PATH_NAMES = ("INDEX_DIR", "ASSETS_DIR", "CHUNKS_DIR", "MANIFEST_PATH", "RAG_DIR")


@pytest.fixture(autouse=True)
def _restore_workspace_and_rag_state():
    """保存并恢复进程级活跃工作区与 RAG 模块内缓存，避免污染其他测试。"""
    original_root = settings_mod.get_workdir()
    original_generation = settings_mod._workspace_generation
    original_corpus = lexical_mod._corpus
    yield
    settings_mod._workspace = original_root
    settings_mod._workspace_generation = original_generation
    lexical_mod._corpus = original_corpus


def _config_path(name: str) -> Path:
    """读取 RAG 配置声明的路径（兼容模块常量与无参函数两种表示）。"""
    value = getattr(rag_config, name)
    return Path(value() if callable(value) else value)


def _under_project(path: Path, root: Path) -> bool:
    project_dir = (root / ".project").resolve()
    resolved = Path(path).resolve()
    return resolved == project_dir or project_dir in resolved.parents


def _tree_texts(base: Path) -> dict:
    if not base.exists():
        return {}
    return {
        str(path.relative_to(base)).replace("\\", "/"): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def _write_corpus(root: Path, name: str = "notes.md") -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    body = (
        "# 迁移测试文档\n\n## 第一节\n\n"
        + (
            "统一目录迁移需要保证历史索引与分块数据在迁移后继续可读，"
            "并且新增数据只写入当前项目 .project 下的 RAG 位置。"
        )
        * 4
        + "\n\n## 第二节\n\n"
        "读取回退保证未发布项仍可从旧位置读取，单项发布失败不影响其他项并支持重试。\n"
    )
    (corpus / name).write_text(body, encoding="utf-8")
    return corpus


def _write_legacy_chunks(root: Path, source: str, chunks: list[dict]) -> Path:
    legacy_chunks = root / LEGACY_RAG_DIRNAME / LEGACY_CHUNKS_DIRNAME
    legacy_chunks.mkdir(parents=True, exist_ok=True)
    safe_name = source.replace("/", "__").replace("\\", "__")
    path = legacy_chunks / f"{safe_name}.chunks.jsonl"
    path.write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks),
        encoding="utf-8",
    )
    return path


def _write_legacy_manifest(root: Path, manifest: dict) -> Path:
    legacy_dir = root / LEGACY_RAG_DIRNAME
    legacy_dir.mkdir(parents=True, exist_ok=True)
    path = legacy_dir / LEGACY_MANIFEST_NAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _legacy_chunk(source: str, chunk_id: str, text: str, extra: dict | None = None) -> dict:
    chunk = {
        "id": chunk_id,
        "source": source,
        "text": text,
        "heading_path": "迁移测试",
        "chapter": "",
        "section": "",
        "char_count": len(text),
        "chunk_index": 0,
    }
    if extra:
        chunk.update(extra)
    return chunk


# --- AC1：路径与新增数据归属当前项目 .project，不创建根目录旧 .rag -------------

def test_rag_paths_and_new_data_belong_to_project_dot_project(tmp_path):
    root = tmp_path / "fresh-project"
    root.mkdir()
    corpus = _write_corpus(root)

    settings_mod.switch_workspace(root)

    # 获取索引、资源、分块和清单路径：均归属当前项目 .project 之下
    for name in CONFIG_PATH_NAMES:
        path = _config_path(name)
        assert _under_project(path, root), f"{name} 必须归属当前项目 .project 下的 RAG 位置，实际为 {path}"

    # 通过实际工具创建数据
    result = ingest_mod.ingest_path(str(corpus))
    assert result["files"], "实际入库入口应产生分块数据"
    lexical_mod.index_chunks()

    # 新增数据均归属 .project 下的 RAG 位置
    project_tree = _tree_texts(root / ".project")
    assert any(name.endswith(".chunks.jsonl") for name in project_tree), "分块数据应落在 .project 下"
    assert any(name.endswith(LEGACY_MANIFEST_NAME) for name in project_tree), "清单应落在 .project 下"
    assert any("corpus" in name and name.endswith(".json") for name in project_tree), "索引应落在 .project 下"

    # 不创建根目录旧 .rag
    assert not (root / LEGACY_RAG_DIRNAME).exists(), "新项目不得创建根目录旧 .rag"


# --- AC2：历史数据迁移后继续可读，新增数据只写入统一目录 -----------------------

def test_legacy_rag_data_readable_after_open_and_new_writes_stay_unified(tmp_path):
    root = tmp_path / "legacy-project"
    root.mkdir()
    source = "history.md"
    old_chunk = _legacy_chunk(source, "hist-1", "历史分块内容：旧 .rag 中的索引数据应当迁移后继续可读。")
    _write_legacy_chunks(root, source, [old_chunk])
    _write_legacy_manifest(
        root,
        {
            "schema_version": 2,
            "sources": {source: {"chunks": 1, "chars": len(old_chunk["text"]), "suffix": ".md"}},
            "embedding_model": None,
        },
    )
    legacy_before = _tree_texts(root / LEGACY_RAG_DIRNAME)

    settings_mod.switch_workspace(root)

    # 历史数据经迁移继续可读（经实际读取入口）
    manifest = ingest_mod.load_manifest()
    assert source in (manifest.get("sources") or {}), "清单历史项应迁移后可读"
    chunks = ingest_mod.load_chunks_for_source(source)
    assert [chunk["id"] for chunk in chunks] == ["hist-1"], "历史分块应迁移后可读"
    assert chunks[0]["text"] == old_chunk["text"]

    # 新增数据只写入统一目录
    new_source = "new.md"
    new_chunk = _legacy_chunk(new_source, "new-1", "迁移之后新增的分块数据只允许写入统一目录。")
    ingest_mod.write_chunks_jsonl(new_source, [new_chunk])
    ingest_mod.save_manifest(
        {"schema_version": 2, "sources": {**manifest.get("sources", {}), new_source: {"chunks": 1}}}
    )

    project_tree = _tree_texts(root / ".project")
    assert any(new_source.replace("/", "__") in name for name in project_tree), "新增分块应写入统一目录"
    legacy_after = _tree_texts(root / LEGACY_RAG_DIRNAME)
    assert set(legacy_after) == set(legacy_before), "新增数据不得写入旧位置"
    for name, payload in legacy_before.items():
        assert legacy_after[name] == payload, f"旧位置 {name} 不得被修改"


# --- AC3：常规读取采用统一位置内容，旧内容不变且可显式读取，重开不覆盖 ----------

def test_regular_reads_prefer_unified_content_and_reopen_keeps_legacy(tmp_path):
    root = tmp_path / "conflict-project"
    root.mkdir()
    source = "conflict.md"
    legacy_path = _write_legacy_chunks(
        root, source, [_legacy_chunk(source, "old-1", "旧位置内容：与统一位置同名但内容不同。")]
    )
    legacy_text = legacy_path.read_text(encoding="utf-8")
    unified_chunk = _legacy_chunk(source, "new-1", "统一位置内容：常规读取应始终采用这一份。")

    settings_mod.switch_workspace(root)
    # 以实际写入入口在统一位置建立同名项（内容不同）
    ingest_mod.write_chunks_jsonl(source, [unified_chunk])
    unified_texts = _tree_texts(root / ".project")

    # 常规读取采用统一位置内容
    chunks = ingest_mod.load_chunks_for_source(source)
    assert [chunk["id"] for chunk in chunks] == ["new-1"], "常规读取必须采用统一位置内容"
    assert chunks[0]["text"] == unified_chunk["text"]

    # 旧内容保持不变且可显式读取（直接读取旧位置文件）
    assert legacy_path.read_text(encoding="utf-8") == legacy_text, "旧内容不得被修改"
    legacy_chunks = [
        json.loads(line)
        for line in legacy_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert legacy_chunks[0]["text"] == "旧位置内容：与统一位置同名但内容不同。"

    # 重复打开项目：不覆盖或复制冲突项
    settings_mod.switch_workspace(tmp_path / "elsewhere")
    settings_mod.switch_workspace(root)

    assert _tree_texts(root / ".project") == unified_texts, "重开不得覆盖或改动统一位置既有项"
    assert legacy_path.read_text(encoding="utf-8") == legacy_text, "重开不得改动旧位置冲突项"
    chunks = ingest_mod.load_chunks_for_source(source)
    assert [chunk["id"] for chunk in chunks] == ["new-1"], "重开后常规读取仍采用统一位置内容"


# --- AC4：单项发布失败不影响其他项，未发布项可读，重试恢复 ---------------------

def test_single_publish_failure_keeps_unpublished_readable_and_retry_recovers(tmp_path, monkeypatch):
    root = tmp_path / "partial-project"
    root.mkdir()
    ok_source = "published.md"
    fail_source = "unpublished.md"
    ok_chunk = _legacy_chunk(ok_source, "ok-1", "已发布项内容：发布成功后应保留在统一目录。")
    fail_chunk = _legacy_chunk(fail_source, "fail-1", "未发布项内容：发布失败后仍可从旧位置读取。")
    _write_legacy_chunks(root, ok_source, [ok_chunk])
    legacy_fail_path = _write_legacy_chunks(root, fail_source, [fail_chunk])
    _write_legacy_manifest(
        root,
        {
            "schema_version": 2,
            "sources": {
                ok_source: {"chunks": 1, "suffix": ".md"},
                fail_source: {"chunks": 1, "suffix": ".md"},
            },
            "embedding_model": None,
        },
    )
    legacy_before = _tree_texts(root / LEGACY_RAG_DIRNAME)

    # 单项发布失败注入：仅在窗口内阻止 unpublished.md 相关项发布到统一目录，
    # 模拟迁移中途单项发布失败/中断；窗口外一律放行，且只拦截统一目录下的目标。
    failure = {"active": True}
    blocked_name = "unpublished.md"

    def _is_blocked(target) -> bool:
        return (
            failure["active"]
            and blocked_name in str(target)
            and _under_project(Path(target), root)
        )

    real_replace, real_rename = os.replace, os.rename
    real_copy2, real_copyfile, real_move = shutil.copy2, shutil.copyfile, shutil.move
    real_write_bytes, real_write_text = Path.write_bytes, Path.write_text

    def _blocked_replace(src, dst, *args, **kwargs):
        if _is_blocked(dst):
            raise OSError("simulated single-item publish failure")
        return real_replace(src, dst, *args, **kwargs)

    def _blocked_rename(src, dst, *args, **kwargs):
        if _is_blocked(dst):
            raise OSError("simulated single-item publish failure")
        return real_rename(src, dst, *args, **kwargs)

    def _blocked_copy2(src, dst, *args, **kwargs):
        if _is_blocked(dst):
            raise OSError("simulated single-item publish failure")
        return real_copy2(src, dst, *args, **kwargs)

    def _blocked_copyfile(src, dst, *args, **kwargs):
        if _is_blocked(dst):
            raise OSError("simulated single-item publish failure")
        return real_copyfile(src, dst, *args, **kwargs)

    def _blocked_move(src, dst, *args, **kwargs):
        if _is_blocked(dst):
            raise OSError("simulated single-item publish failure")
        return real_move(src, dst, *args, **kwargs)

    def _blocked_write_bytes(self, data):
        if _is_blocked(self):
            raise OSError("simulated single-item publish failure")
        return real_write_bytes(self, data)

    def _blocked_write_text(self, data, *args, **kwargs):
        if _is_blocked(self):
            raise OSError("simulated single-item publish failure")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _blocked_replace)
    monkeypatch.setattr(os, "rename", _blocked_rename)
    monkeypatch.setattr(shutil, "copy2", _blocked_copy2)
    monkeypatch.setattr(shutil, "copyfile", _blocked_copyfile)
    monkeypatch.setattr(shutil, "move", _blocked_move)
    monkeypatch.setattr(Path, "write_bytes", _blocked_write_bytes)
    monkeypatch.setattr(Path, "write_text", _blocked_write_text)

    settings_mod.switch_workspace(root)

    # 未发布项仍可通过实际 RAG 入口读取
    chunks = ingest_mod.load_chunks_for_source(fail_source)
    assert [chunk["id"] for chunk in chunks] == ["fail-1"], "未发布项应仍可读取"
    assert chunks[0]["text"] == fail_chunk["text"]

    # 新写入只进统一目录（目标名不含被阻断项名，窗口内放行）
    new_chunk = _legacy_chunk("extra.md", "extra-1", "故障期间新增的数据只允许写入统一目录。")
    ingest_mod.write_chunks_jsonl("extra.md", [new_chunk])

    project_tree = _tree_texts(root / ".project")
    assert any("extra.md" in name for name in project_tree), "新写入必须进入统一目录"
    legacy_after = _tree_texts(root / LEGACY_RAG_DIRNAME)
    assert set(legacy_after) == set(legacy_before), "新写入不得落到旧位置"
    for name, payload in legacy_before.items():
        assert legacy_after[name] == payload, f"旧数据 {name} 不得被回退写入修改"

    published_before = {
        name: payload for name, payload in project_tree.items() if "unpublished.md" not in name
    }
    assert published_before, "其他项应已发布到统一目录"

    # 解除故障后重试（重新打开项目并再次读取）
    failure["active"] = False
    settings_mod.switch_workspace(tmp_path / "elsewhere")
    settings_mod.switch_workspace(root)

    chunks = ingest_mod.load_chunks_for_source(fail_source)
    assert [chunk["id"] for chunk in chunks] == ["fail-1"], "重试后未发布项应可读取"
    project_after = _tree_texts(root / ".project")
    fail_bytes = legacy_fail_path.read_bytes()
    assert any(
        "unpublished.md" in name and payload == fail_bytes
        for name, payload in project_after.items()
    ), "重试应把未发布项发布到统一目录"
    for name, payload in published_before.items():
        assert project_after.get(name) == payload, f"重试不得改变已发布内容 {name}"
    legacy_final = _tree_texts(root / LEGACY_RAG_DIRNAME)
    for name, payload in legacy_before.items():
        assert legacy_final[name] == payload, f"重试不得丢失或修改旧数据 {name}"


# --- AC5：访问跟随运行时活跃工作区，项目选择不写回静态配置 ---------------------

def test_rag_access_follows_active_workspace_across_switches(tmp_path):
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()

    settings_mod.switch_workspace(root_a)
    chunk_a = _legacy_chunk("doc.md", "a-1", "项目A 的分块内容，不应与项目B 混用。")
    ingest_mod.write_chunks_jsonl("doc.md", [chunk_a])
    ingest_mod.save_manifest({"schema_version": 2, "sources": {"doc.md": {"chunks": 1}}})

    settings_mod.switch_workspace(root_b)
    chunk_b = _legacy_chunk("doc.md", "b-1", "项目B 的分块内容，与项目A 不同。")
    ingest_mod.write_chunks_jsonl("doc.md", [chunk_b])
    ingest_mod.save_manifest({"schema_version": 2, "sources": {"doc.md": {"chunks": 2}}})

    # RAG 模块已加载后切换：路径必须跟随当前项目
    for name in CONFIG_PATH_NAMES:
        assert _under_project(_config_path(name), root_b), f"{name} 必须跟随活跃工作区"

    chunks_b = ingest_mod.load_chunks_for_source("doc.md")
    assert [chunk["id"] for chunk in chunks_b] == ["b-1"], "读取必须跟随当前项目"
    assert ingest_mod.load_manifest()["sources"]["doc.md"]["chunks"] == 2

    tree_b = _tree_texts(root_b / ".project")
    assert any("doc.md" in name for name in tree_b), "写入必须落在当前项目"

    # 切回项目A：互不混用，不因模块级路径缓存访问旧项目
    settings_mod.switch_workspace(root_a)
    chunks_a = ingest_mod.load_chunks_for_source("doc.md")
    assert [chunk["id"] for chunk in chunks_a] == ["a-1"], "切回后读取不得混用项目B 数据"
    assert ingest_mod.load_manifest()["sources"]["doc.md"]["chunks"] == 1

    tree_a = _tree_texts(root_a / ".project")
    assert any("doc.md" in name for name in tree_a)
    assert _tree_texts(root_b / ".project") == tree_b, "访问项目A 不得改动项目B 数据"


def test_workspace_switch_does_not_write_project_choice_to_static_config(tmp_path):
    config_dir = settings_mod.BUILTIN_CONFIG_DIR
    before = _tree_texts(config_dir)

    root_a = tmp_path / "static-a"
    root_b = tmp_path / "static-b"
    root_a.mkdir()
    root_b.mkdir()
    settings_mod.switch_workspace(root_a)
    ingest_mod.save_manifest({"schema_version": 2, "sources": {}})
    settings_mod.switch_workspace(root_b)
    ingest_mod.save_manifest({"schema_version": 2, "sources": {}})

    assert _tree_texts(config_dir) == before, "项目选择不得写回静态配置"
