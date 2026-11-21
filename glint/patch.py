"""Fix candidates: Hydrogen writes N patches in parallel, Neon writes the one repair round."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from glint import config, gemini
from glint.perceive import app_source_parts
from glint.schemas import Candidate, Patch, ReproSpec

log = logging.getLogger("glint.patch")

TEMPERATURE_RANGE = (0.2, 0.9)
MAX_REPAIR_CANDIDATES = 2
FEEDBACK_CHARS = 300


# The Gemini Developer API rejects map-typed fields in a response schema, so models answer with a list of
# files and it is converted to the shared Patch (path -> content) here.
class FileEdit(BaseModel):
    path: str
    content: str


class PatchWire(BaseModel):
    files: list[FileEdit]
    rationale: str = ""


class RepairWire(BaseModel):
    patches: list[PatchWire] = Field(default_factory=list)


def _rules() -> str:
    extensions = ", ".join(sorted(config.PATCH_ALLOWED_EXT))
    return f"""Rules for the fix:
- Edit at most {config.PATCH_MAX_FILES} files, and only files that already exist in the source given above.
- Only files with these extensions may be edited: {extensions}.
- For every file you edit, return its path exactly as shown after "===" and its FULL new content, with every
  unchanged line kept exactly as it is. Do not reformat, reorder, or re-indent anything you are not fixing.
- Make the smallest change that makes the expected behavior true. Change at most {config.PATCH_MAX_CHANGED_LINES}
  lines in total, and usually only one or two.
- Fix the cause in the application's logic. Do not special-case the texts or values from the bug report.
- Never create, edit, or mention test files.
- In rationale, say in one sentence what was wrong and what you changed."""


PATCH_PROMPT = """You fix one bug in a web application.

You are given the application's full source code (it currently HAS the bug) and a bug report in JSON that
describes the user's steps, the expected behavior, and the observed wrong behavior.
Find the defect in the source and return a fix.

{rules}

Bug report:
{spec}
"""

REPAIR_PROMPT = """You fix one bug in a web application.

You are given the application's full source code (it currently HAS the bug) and a bug report in JSON.
Earlier fix attempts were all rejected by automated checks. For each rejected attempt you are told only which
check failed and its message. Think about what those failures have in common and what the earlier attempts must
have missed, then write up to {max_patches} new fixes that take different approaches from each other.

{rules}

Bug report:
{spec}

Rejected attempts:
{feedback}
"""


def to_patch(wire: PatchWire) -> Patch:
    """Convert the model's list-of-files answer into the shared Patch."""
    return Patch(files={edit.path.strip(): edit.content for edit in wire.files}, rationale=wire.rationale.strip())


def spread_temperatures(n: int) -> list[float]:
    """n temperatures spread evenly over TEMPERATURE_RANGE."""
    low, high = TEMPERATURE_RANGE
    if n <= 1:
        return [low]
    return [round(low + (high - low) * i / (n - 1), 2) for i in range(n)]


async def _one_candidate(cid: str, parts: list, temperature: float) -> Candidate:
    try:
        async with asyncio.timeout(config.TIMEOUT_S["candidate"]):
            wire = await gemini.hydrogen(parts, schema=PatchWire, temperature=temperature)
    except TimeoutError:
        return Candidate(id=cid, source="initial", reject_reason="The model did not answer in time.")
    except Exception as exc:  # noqa: BLE001 - one failed call must not sink the other candidates
        log.warning("candidate %s failed: %s", cid, type(exc).__name__)
        return Candidate(id=cid, source="initial", reject_reason=f"The model call failed ({type(exc).__name__}).")
    if not isinstance(wire, PatchWire) or not wire.files:
        return Candidate(id=cid, source="initial", reject_reason="The model returned no file changes.")
    return Candidate(id=cid, source="initial", patch=to_patch(wire))


async def candidates(spec: ReproSpec, app_dir: Path, n: int) -> list[Candidate]:
    """n Hydrogen fix candidates in parallel with varied temperature. Ids c1..cn. Never raises."""
    prompt = PATCH_PROMPT.format(rules=_rules(), spec=spec.model_dump_json(indent=2))
    parts: list = [*app_source_parts(app_dir), prompt]
    temperatures = spread_temperatures(n)
    return list(
        await asyncio.gather(*(_one_candidate(f"c{i + 1}", parts, temperatures[i]) for i in range(n)))
    )


def failure_feedback(failures: list[Candidate]) -> str:
    """Sanitized feedback for the repair round: per failed candidate, only the gate name and its message."""
    lines: list[str] = []
    for candidate in failures:
        failed = next((g for g in candidate.gates if not g.passed), None)
        if failed is not None:
            gate, message = failed.gate, failed.detail
        else:
            gate, message = "no gate reached", candidate.reject_reason
        message = " ".join(message.split())[:FEEDBACK_CHARS]
        lines.append(f"- attempt {candidate.id}: failed {gate}: {message}")
    return "\n".join(lines) if lines else "- no details available"


async def repair(spec: ReproSpec, app_dir: Path, failures: list[Candidate]) -> list[Candidate]:
    """One Neon call that returns up to two new candidates (r1, r2). Never raises; returns [] on failure."""
    prompt = REPAIR_PROMPT.format(
        max_patches=MAX_REPAIR_CANDIDATES,
        rules=_rules(),
        spec=spec.model_dump_json(indent=2),
        feedback=failure_feedback(failures),
    )
    parts: list = [*app_source_parts(app_dir), prompt]
    try:
        async with asyncio.timeout(config.TIMEOUT_S["repair"]):
            wire = await gemini.neon(parts, schema=RepairWire, thinking=config.REPAIR_THINKING)
    except TimeoutError:
        log.warning("repair round timed out")
        return []
    except Exception as exc:  # noqa: BLE001 - a failed repair round means no repair candidates
        log.warning("repair round failed: %s", type(exc).__name__)
        return []
    if not isinstance(wire, RepairWire):
        return []
    out: list[Candidate] = []
    for patch_wire in wire.patches:
        if not patch_wire.files:
            continue
        out.append(Candidate(id=f"r{len(out) + 1}", source="repair", patch=to_patch(patch_wire)))
        if len(out) == MAX_REPAIR_CANDIDATES:
            break
    return out
