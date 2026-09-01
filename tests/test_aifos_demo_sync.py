"""AIFOS 单文件 Demo 同步、种子隐私与浏览器标准中心契约。"""

import base64
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_sync_module():
    path = ROOT / "docs" / "sync_aifos_demo.py"
    spec = importlib.util.spec_from_file_location("aifos_demo_sync", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_from_html(html):
    prefix = "window.__SEED__ = "
    start = html.index(prefix) + len(prefix)
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def _walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def test_sync_is_idempotent_and_tracks_formal_ui(tmp_path):
    sync = _load_sync_module()
    work = tmp_path / "repo"
    (work / "docs").mkdir(parents=True)
    (work / "aifos" / "web" / "static").mkdir(parents=True)
    for source, relative in (
        (ROOT / "docs" / "index.html", "docs/index.html"),
        (ROOT / "aifos" / "web" / "static" / "style.css",
         "aifos/web/static/style.css"),
        (ROOT / "aifos" / "web" / "static" / "app.js",
         "aifos/web/static/app.js"),
        (ROOT / "aifos" / "web" / "static" / "index.html",
         "aifos/web/static/index.html"),
    ):
        shutil.copy2(source, work / relative)
    sync.ROOT = work
    sync.DOC = work / "docs" / "index.html"
    sync.CSS = work / "aifos" / "web" / "static" / "style.css"
    sync.APP = work / "aifos" / "web" / "static" / "app.js"
    sync.SHELL = work / "aifos" / "web" / "static" / "index.html"

    sync.main()
    first = sync.DOC.read_text(encoding="utf-8")
    sync.main()
    assert sync.DOC.read_text(encoding="utf-8") == first

    assert '<html lang="zh-CN">' in first
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in first
    assert "AIFOS V3.2" in first
    assert sync._formal_header() in first
    assert sync.CSS.read_text(encoding="utf-8").rstrip() in first
    assert sync.APP.read_text(encoding="utf-8").rstrip() in first
    for marker in (
        sync.STYLE_START, sync.STYLE_END, sync.SHELL_START, sync.SHELL_END,
        sync.SEED_START, sync.SEED_END, sync.APP_START, sync.APP_END,
    ):
        assert first.count(marker) == 1


def test_sync_fails_loudly_when_a_stable_marker_is_lost(tmp_path):
    sync = _load_sync_module()
    target = tmp_path / "index.html"
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    target.write_text(html.replace(sync.SHELL_END, ""), encoding="utf-8")
    sync.DOC = target
    with pytest.raises(RuntimeError, match="同步标记异常"):
        sync.main()


def test_fresh_seed_has_standard_state_and_no_temporary_path_leaks():
    sync = _load_sync_module()
    seed = sync._fresh_seed()
    standard = seed["standards"]["history"][0]
    assert seed["standards"]["active_version_id"] == standard["version_id"]
    assert standard["content"] == sync.DEFAULT_STANDARD
    assert len(standard["fingerprint"]) == 64

    forbidden = (b"file:///private/var", b"aifos-docs-seed-", b"/private/var/")
    serialized = json.dumps(seed, ensure_ascii=False).encode()
    assert not any(token in serialized for token in forbidden)
    for value in _walk_strings(seed):
        if value.startswith("data:") and ";base64," in value:
            decoded = base64.b64decode(value.split(",", 1)[1])
            assert not any(token in decoded for token in forbidden)


def test_demo_exposes_standard_api_migration_and_episode_snapshot_contract():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    seed = _seed_from_html(html)
    assert seed["standards"]["history"][0]["active"] is True
    required = (
        'const KEY = "aifos.demo.state.v3"',
        'const LEGACY_KEY = "aifos.demo.state.v2"',
        'key === "/api/standards"',
        'key.startsWith("/api/standards/export")',
        'key === "/api/standards/save"',
        'key === "/api/standards/activate"',
        'key === "/api/standards/reset"',
        'key === "/api/standards/import"',
        "const productionStandard = clone(activeStandard())",
        "production_standard: productionStandard",
        "standard_fingerprint:",
        'version: "3.2.0-browser-demo"',
    )
    for text in required:
        assert text in html
    assert 'payload.storyboard.profile = { ...INDUSTRIAL_PROFILE }' not in html
    assert 'payload.production_profile = { ...INDUSTRIAL_PROFILE }' not in html


def test_all_demo_inline_javascript_parses():
    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    script = r'''
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1]);
if (scripts.length !== 3) throw new Error(`expected 3 scripts, got ${scripts.length}`);
scripts.forEach((source, index) =>
  new vm.Script(source, { filename: `demo-inline-${index}.js` }));
'''
    subprocess.run(
        ["node", "-e", script, str(ROOT / "docs" / "index.html")],
        check=True, capture_output=True, text=True)


def test_frontend_routes_only_after_all_dependencies_are_initialized():
    """直达标准中心或单集画布时不能触发 const/class 的暂时性死区。"""
    app = (ROOT / "aifos" / "web" / "static" / "app.js").read_text(
        encoding="utf-8")
    startup = app.index('window.addEventListener("hashchange", route);')
    assert startup > app.index("const STANDARD_SECTIONS = [")
    assert startup > app.index("class StoryboardCanvas")
    assert app.index("route();", startup) > startup


def test_demo_standard_api_persists_versions_and_snapshots_new_episode():
    if shutil.which("node") is None:
        pytest.skip("node 不可用")
    script = r'''
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1]);
const store = new Map();
const sandbox = {
  console, JSON, Math, Date, Map, Set, URL, Blob, TextEncoder, Uint8Array,
  encodeURIComponent, decodeURIComponent, escape, unescape, atob, btoa,
  setTimeout, clearTimeout, setInterval, clearInterval,
  localStorage: {
    getItem: (key) => store.has(key) ? store.get(key) : null,
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  },
  location: { href: "https://example.test/docs/index.html#/", reload() {} },
  document: { getElementById() { return null; } },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
new vm.Script(scripts[0]).runInContext(sandbox);
new vm.Script(scripts[1]).runInContext(sandbox);

(async () => {
  const json = async (path, opts) => {
    const response = await sandbox.fetch(path, opts);
    const body = await response.json();
    if (!response.ok) throw new Error(`${response.status}: ${JSON.stringify(body)}`);
    return body;
  };
  const initial = await json("/api/standards");
  if (initial.active.version !== 1 || initial.history.length !== 1)
    throw new Error("default standard missing");
  const content = JSON.parse(JSON.stringify(initial.active.content));
  content.description = "浏览器测试自定义标准";
  content.rules.dialogue.max_chars_per_shot = 20;
  const saved = await json("/api/standards/save", { method: "POST",
    body: JSON.stringify({ content, change_note: "测试保存", activate: true,
      expected_active_id: initial.active.version_id }) });
  if (saved.standard.version !== 2 || !saved.standard.active ||
      !saved.standard.fingerprint.startsWith("demo-"))
    throw new Error("save did not create active immutable version");

  await json("/api/produce", { method: "POST", body: JSON.stringify({
    sentence: "开始制作《标准快照测试》第1集", premise: "都市悬疑", review: true,
  }) });
  const persisted = JSON.parse(store.get("aifos.demo.state.v3"));
  const episodeId = persisted.episodes.find((item) =>
    persisted.projects.find((project) => project.id === item.project_id)?.title ===
      "标准快照测试").id;
  const production = persisted.fullResults[episodeId];
  if (production.production_standard.version !== 2 ||
      production.production_profile.standard_fingerprint !== saved.standard.fingerprint)
    throw new Error("episode did not snapshot active standard");
  const gateIds = production.preflight.gates.map((gate) => gate.id);
  if (gateIds.length !== 11 ||
      !["dialogue", "performance", "camera", "audio"].every((id) => gateIds.includes(id)))
    throw new Error(`demo did not apply the 11-gate contract: ${gateIds.join(",")}`);

  await json("/api/standards/activate", { method: "POST",
    body: JSON.stringify({ version_id: 1 }) });
  const afterActivate = JSON.parse(store.get("aifos.demo.state.v3"));
  if (afterActivate.fullResults[episodeId].production_standard.version !== 2)
    throw new Error("activating another standard mutated an existing episode");
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
'''
    subprocess.run(
        ["node", "-e", script, str(ROOT / "docs" / "index.html")],
        check=True, capture_output=True, text=True, timeout=10)
