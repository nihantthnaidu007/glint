"""G3 and patch-rule checks. Uses a local browser and local HTTP server, no model calls."""
from __future__ import annotations

from pathlib import Path

from glint import gates, patch, workspace
from glint.schemas import Candidate, GateResult, Patch

APP = Path(__file__).resolve().parent.parent / "apps" / "todomvc"


async def test_g3_passes_on_unmodified_app() -> None:
    ws = workspace.make_workspace(APP)
    async with workspace.serve(ws) as url:
        assert await gates.monkey_check(url) == []


async def test_g3_fails_when_js_throws() -> None:
    ws = workspace.make_workspace(APP)
    target = ws / "controller.js"
    target.write_text(target.read_text() + "\nthrow new Error('glint injected failure');\n")
    async with workspace.serve(ws) as url:
        errors = await gates.monkey_check(url)
    assert errors
    assert any("glint injected failure" in e for e in errors)


async def test_g3_fails_when_handler_throws_on_interaction() -> None:
    ws = workspace.make_workspace(APP)
    target = ws / "controller.js"
    source = target.read_text()
    marker = "Controller.prototype.addItem = function (title) {"
    assert marker in source
    target.write_text(source.replace(marker, marker + "\n        throw new Error('glint add failure');"))
    async with workspace.serve(ws) as url:
        errors = await gates.monkey_check(url)
    assert any("glint add failure" in e for e in errors)


async def test_patch_touching_new_file_is_rejected_without_raising(tmp_path: Path) -> None:
    c = Candidate(id="c1", source="initial", patch=Patch(files={"brand_new.js": "var a = 1;\n"}))
    spec = None  # never reached: the patch is rejected before any gate that needs the spec
    out = await gates.check_candidate(c, APP, tmp_path / "repro_test.py", None, tmp_path, spec)  # type: ignore[arg-type]
    assert out.survived is False
    assert out.gates and out.gates[0].gate == "G3" and not out.gates[0].passed
    assert "Patch rejected" in out.reject_reason


async def test_candidate_without_patch_is_rejected(tmp_path: Path) -> None:
    c = Candidate(id="c2", source="initial", reject_reason="The model did not answer in time.")
    out = await gates.check_candidate(c, APP, tmp_path / "repro_test.py", None, tmp_path, None)  # type: ignore[arg-type]
    assert out.survived is False and out.reject_reason == "The model did not answer in time."


def test_wire_patch_conversion_and_temperatures() -> None:
    wire = patch.PatchWire(files=[patch.FileEdit(path=" controller.js ", content="x")], rationale=" why ")
    converted = patch.to_patch(wire)
    assert converted.files == {"controller.js": "x"} and converted.rationale == "why"
    assert patch.spread_temperatures(4) == [0.2, 0.43, 0.67, 0.9]
    assert patch.spread_temperatures(1) == [0.2]


def test_repair_feedback_has_only_gate_and_message() -> None:
    failed = Candidate(
        id="c1",
        source="initial",
        patch=Patch(files={"controller.js": "SECRET PATCH CONTENT"}, rationale="SECRET RATIONALE"),
        gates=[GateResult(gate="G2", passed=False, detail="AssertionError: expected 2 items", candidate_id="c1")],
        reject_reason="G2: AssertionError: expected 2 items",
    )
    text = patch.failure_feedback([failed])
    assert "G2" in text and "AssertionError: expected 2 items" in text
    assert "SECRET" not in text


def test_g3_ignores_only_the_favicon_load_error() -> None:
    failed = "Failed to load resource: the server responded with a status of 404 (File not found)"
    assert gates.is_favicon_load_error(failed, "http://127.0.0.1:5000/favicon.ico")
    assert not gates.is_favicon_load_error(failed, "http://127.0.0.1:5000/app.js")
    assert not gates.is_favicon_load_error(failed, "http://127.0.0.1:5000/img/favicon.ico")
    assert not gates.is_favicon_load_error("Uncaught TypeError: x is not a function", "http://127.0.0.1:5000/favicon.ico")
    errors: list[str] = []
    gates._on_console("error", failed, "http://127.0.0.1:5000/favicon.ico", errors)
    gates._on_console("warning", "just a warning", "", errors)
    gates._on_console("error", failed, "http://127.0.0.1:5000/store.js", errors)
    assert errors == [f"console error: {failed}"]


async def test_g3_passes_on_unmodified_app_five_times_in_a_row() -> None:
    ws = workspace.make_workspace(APP)
    async with workspace.serve(ws) as url:
        for attempt in range(5):
            assert await gates.monkey_check(url) == [], f"run {attempt + 1} of 5 reported errors"


async def test_g3_never_leaves_the_app_origin(tmp_path: Path) -> None:
    """External links are not clicked and external requests are aborted without counting as app errors."""
    (tmp_path / "index.html").write_text(
        """<!DOCTYPE html><html><body>
        <a id="out" href="http://external.invalid/away">external link</a>
        <li><a href="https://external.invalid/other">external link inside a list item</a></li>
        <a href="#/local">local link</a>
        <button id="b" onclick="document.title = 'clicked'">button</button>
        <img src="http://external.invalid/pixel.png" width="10" height="10">
        <script>window.addEventListener('beforeunload', () => { localStorage.setItem('left', '1'); });</script>
        </body></html>""",
        encoding="utf-8",
    )
    async with workspace.serve(tmp_path) as url:
        assert await gates.monkey_check(url, actions=40) == []


def test_g3_console_filter_for_blocked_requests() -> None:
    failed = "Failed to load resource: net::ERR_FAILED"
    errors: list[str] = []
    blocked = {"http://external.invalid/pixel.png"}
    gates._on_console("error", failed, "http://external.invalid/pixel.png", errors, blocked)
    assert errors == []
    gates._on_console("error", failed, "http://127.0.0.1:5000/store.js", errors, blocked)
    gates._on_console("error", "Uncaught TypeError: boom", "http://external.invalid/pixel.png", errors, blocked)
    assert errors == [f"console error: {failed}", "console error: Uncaught TypeError: boom"]
