import importlib.util
import io
import json
import pathlib
import time
import zipfile

import pytest

from app import bundles
from app.rules import RuleError

CLIENTS = pathlib.Path(__file__).parents[1] / "clients"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, CLIENTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load("library_sync")
mc = _load("mc")


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


SKILL = {"SKILL.md": b"---\nname: demo\n---\n# Demo\nSee reference/a.md", "reference/a.md": b"# A\nTiming table."}


@pytest.fixture
def skill_dir(tmp_path):
    root = tmp_path / "skills"
    folder = root / "demo"
    for name, data in SKILL.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    (folder / ".env").write_text("TOKEN=secret")
    (folder / "node_modules" / "x").mkdir(parents=True)
    (folder / "node_modules" / "x" / "i.js").write_text("big")
    return root, folder


def test_client_and_server_agree_on_the_tree_hash(skill_dir):
    root, folder = skill_dir
    bundle, files = sync._skill_bundle(folder / "SKILL.md", root)
    assert sorted(files) == ["SKILL.md", "reference/a.md"]  # no .env, no node_modules
    assert bundle == bundles.tree_hash(bundles.read_zip(sync.bundle_zip(files))) == mc.tree_hash(SKILL)


def test_tree_hash_ignores_how_the_zip_was_made():
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as z:
        for name, data in reversed(list(SKILL.items())):
            z.writestr(name, data)
    assert bundles.tree_hash(bundles.read_zip(stored.getvalue())) == bundles.tree_hash(bundles.read_zip(_zip(SKILL)))


def test_a_loose_skill_md_has_no_bundle(tmp_path):
    (tmp_path / "SKILL.md").write_text("# loose")
    assert sync._skill_bundle(tmp_path / "SKILL.md", tmp_path) == (None, None)


@pytest.mark.parametrize("name", ["../evil.md", "/etc/passwd", "a/../../b", "C:/x"])
def test_unsafe_paths_are_refused(name):
    with pytest.raises(RuleError, match="Unsafe"):
        bundles.read_zip(_zip({"SKILL.md": b"x", name: b"y"}))
    assert not mc._safe(name)


def test_backslashes_are_unsafe():  # zipfile turns them into '/' only on Windows, so check the rule itself
    assert not bundles.safe_name("a\\b") and not mc._safe("..\\x")


def test_save_checks_content_against_name_and_collect_drops_orphans(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "BUNDLE_DIR", tmp_path)
    data, good = _zip(SKILL), bundles.tree_hash(SKILL)
    with pytest.raises(RuleError, match="hashes to"):
        bundles.save("0" * 64, data)
    with pytest.raises(RuleError, match="SKILL.md"):
        bundles.save(bundles.tree_hash({"x.md": b"x"}), _zip({"x.md": b"x"}))
    assert bundles.save(good, data) == 2
    assert bundles.have({good, "1" * 64}) == {good}
    assert bundles.names(good) == ["SKILL.md", "reference/a.md"]
    assert bundles.read_file(good, "reference/a.md") == SKILL["reference/a.md"]
    with pytest.raises(RuleError, match="No file"):
        bundles.read_file(good, "missing.md")
    assert bundles.collect({good}) == 0 and bundles.collect(set()) == 1 and not bundles.have({good})


class FakeClient:
    def __init__(self, files=SKILL):
        self.files, self.downloads = files, 0

    def raw(self, path, query=None, timeout=60):
        self.downloads += 1
        return _zip(self.files)


def test_pull_marks_the_folder_and_session_end_removes_it(tmp_path):
    dest = tmp_path / "skills"
    client = FakeClient()
    r = mc.pull_skill(client, "demo", dest, session="s1", item=(17, "ckm:demo"))
    folder = dest / "ckm-demo"
    assert r["status"] == "pulled" and (folder / "reference" / "a.md").read_bytes() == SKILL["reference/a.md"]
    assert not list(tmp_path.glob(".mc-pull-*"))  # unpacked beside dest, then moved in
    assert mc.pull_skill(client, "demo", dest, session="s2", item=(17, "ckm:demo"))["status"] == "already"
    assert mc.read_marker(folder)["sessions"] == ["s1", "s2"]
    assert mc.release_session("s1", dest) == [] and folder.exists()  # s2 still uses it
    assert mc.release_session("s2", dest) == ["ckm:demo"] and not folder.exists()


def test_a_changed_skill_is_replaced_and_a_hand_installed_one_left_alone(tmp_path):
    dest = tmp_path / "skills"
    mc.pull_skill(FakeClient(), "demo", dest, session="s1", item=(17, "demo"))
    changed = {**SKILL, "SKILL.md": b"# v2"}
    assert mc.pull_skill(FakeClient(changed), "demo", dest, session="s1", item=(17, "demo"))["status"] == "updated"
    assert (dest / "demo" / "SKILL.md").read_bytes() == b"# v2"
    (dest / "mine").mkdir()
    (dest / "mine" / "SKILL.md").write_text("# by hand")
    client = FakeClient()
    assert mc.pull_skill(client, "mine", dest, session="s1", item=(9, "mine"))["status"] == "local"
    assert client.downloads == 0 and (dest / "mine" / "SKILL.md").read_text() == "# by hand"
    with pytest.raises(mc.McError, match="not installed by mc"):
        mc.drop_skill("mine", dest)


def test_kept_pulls_survive_sessions_and_stale_ones_are_swept(tmp_path):
    dest = tmp_path / "skills"
    mc.pull_skill(FakeClient(), "kept", dest, session=None, item=(1, "kept"))
    mc.pull_skill(FakeClient(), "kept", dest, session="s1", item=(1, "kept"))
    mc.pull_skill(FakeClient(), "temp", dest, session="s1", item=(2, "temp"))
    marker = mc.read_marker(dest / "temp")
    marker["touched_at"] = time.time() - mc.STALE_S - 1
    (dest / "temp" / mc.MARKER).write_text(json.dumps(marker))
    assert mc.sweep_stale(dest) == ["temp"]
    assert mc.release_session("s1", dest) == [] and (dest / "kept").exists()
    assert mc.drop_skill("kept", dest) and not (dest / "kept").exists()


def test_local_skill_names_reads_frontmatter_and_folders(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text("---\nname: Alpha-Skill\ndescription: x\n---\n")
    (tmp_path / "plugin" / "skills" / "b").mkdir(parents=True)
    (tmp_path / "plugin" / "skills" / "b" / "SKILL.md").write_text("---\nname: 'beta'\n---\n")
    assert mc.local_skill_names([tmp_path, tmp_path / "missing"]) == {"a", "alpha-skill", "plugin", "beta", "b"} - {"plugin"}
    assert mc.folder_name("stitch::generate-design") == "stitch-generate-design"
