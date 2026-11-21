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
