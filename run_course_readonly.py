"""Score every published INFO 201 rubric locally and compare with Canvas.

Reads Canvas. Never posts a grade or comment. Safe to re-run: finished
submissions are skipped.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _prefer_local_semif() -> None:
    """Prefer a sibling checkout. `uv sync` already installs semif-phase1."""
    configured = os.environ.get("SEMIF_SRC", "")
    candidates = [Path(configured)] if configured else []
    candidates.extend([ROOT.parent / "SemIf-OpenJev" / "src", ROOT.parent / "SemIf" / "src"])
    for path in candidates:
        if path.is_dir():
            sys.path.insert(0, str(path))
            return


_prefer_local_semif()

from autograde.canvas_api import CanvasClient  # noqa: E402
from autograde.rubric import (  # noqa: E402
    Rubric,
    build_state,
    score_answers,
    score_selection,
    shape_canvas_rubric,
    to_systemone,
)
from autograde.semif_backend import SemifError, SemifGrader  # noqa: E402

COURSE_ID = "1862977"
CONFIGS = [ROOT / "data" / "config.json"]
if os.environ.get("CANVAS_CONFIG"):
    CONFIGS.append(Path(os.environ["CANVAS_CONFIG"]))
OUT = ROOT / "data" / "info201-all.jsonl"
SUMMARY = ROOT / "data" / "info201-all-summary.json"
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".mp3", ".wav")


def _refuse(*_args, **_kwargs):
    raise RuntimeError("This comparison is read-only and cannot post Canvas grades")


def _config() -> dict:
    for path in CONFIGS:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit("No Canvas config.json found")


def _done() -> set[tuple[str, str]]:
    found = set()
    if not OUT.exists():
        return found
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("student_id") and row.get("assignment_id"):
            found.add((str(row["assignment_id"]), str(row["student_id"])))
    return found


def _rubric_assignments(client: CanvasClient) -> list[dict]:
    rows = client._get_all(f"/api/v1/courses/{COURSE_ID}/assignments", {"per_page": 50})
    found = []
    for row in rows:
        rubric = row.get("rubric") or []
        if not row.get("published") or not rubric:
            continue
        points = row.get("points_possible")
        if not points:
            continue
        found.append(
            {
                "id": str(row["id"]),
                "name": row.get("name") or str(row["id"]),
                "points": points,
                "criteria": len(rubric),
            }
        )
    return found


def _readable(name: str) -> bool:
    lower = name.lower()
    return not lower.endswith(SKIP_SUFFIXES)


def _submission_text(client: CanvasClient, row: dict) -> str:
    parts = [(row.get("text") or "").strip()]
    for item in row.get("attachments") or []:
        name = item.get("name") or "file"
        if not _readable(name):
            continue
        try:
            parts.append(client.download_text(item["url"], name, item.get("content_type") or ""))
        except Exception as exc:
            parts.append(f"[{name}: {exc}]")
    kept = []
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]") and "\n" not in text:
            continue
        kept.append(text)
    return _fit("\n\n".join(kept))


def _fit(text: str, limit: int = 16000) -> str:
    """Keep the start and the end when a report is longer than the scorer window.

    A head-only cut drops the later rubric sections, which is where several
    INFO 201 writeups put the second analysis and the explanation.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    marker = "\n\n[... middle of submission omitted ...]\n\n"
    body = limit - len(marker)
    head = body // 2
    tail = body - head
    return text[:head] + marker + text[-tail:]


def _by_canvas(rubric: Rubric, result, human_points: dict) -> list[dict]:
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for item, scored in zip(rubric.criteria, result.criteria):
        key = item.canvas_id or item.id
        group = grouped.get(key)
        if group is None:
            group = {"canvas_id": key, "human": human_points.get(key), "model_points": 0.0, "checks": []}
            grouped[key] = group
            order.append(key)
        group["model_points"] = round(group["model_points"] + scored.points, 2)
        group["checks"].append(item.id)
    return [grouped[key] for key in order]


def _grade_one(grader: SemifGrader, rubric: Rubric, text: str, assignment_name: str) -> dict:
    state = build_state(rubric, text, assignment_name)
    payload = grader.grade(to_systemone(rubric, state))
    result = score_answers(rubric, payload["answers"])
    posted = score_selection(rubric, {item.id: item.model_key for item in result.criteria})
    return {
        "model_points": posted.scaled_points,
        "model_expected": result.scaled_points,
        "seconds": round(float((payload.get("timing") or {}).get("total_seconds") or 0), 2),
        "fallback": (payload.get("timing") or {}).get("fallback"),
        "result": result,
    }


def _write(row: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _summarize() -> None:
    rows = [json.loads(line) for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip()]
    scored = [row for row in rows if row.get("model_points") is not None and row.get("human_score") is not None]
    by_name: dict[str, list] = {}
    for row in scored:
        by_name.setdefault(row["assignment"], []).append(row)
    assignments = []
    for name, group in by_name.items():
        errors = [abs(float(item["human_score"]) - float(item["model_points"])) for item in group]
        assignments.append(
            {
                "assignment": name,
                "n": len(group),
                "mean_abs_error": round(sum(errors) / len(errors), 3),
                "exact": sum(error < 0.05 for error in errors),
            }
        )
    assignments.sort(key=lambda item: item["mean_abs_error"], reverse=True)
    errors = [abs(float(item["human_score"]) - float(item["model_points"])) for item in scored]
    summary = {
        "course_id": COURSE_ID,
        "read_only": True,
        "n": len(scored),
        "skipped_or_failed": len(rows) - len(scored),
        "mean_abs_error": round(sum(errors) / len(errors), 3) if errors else None,
        "exact": sum(error < 0.05 for error in errors),
        "assignments": assignments,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"scored {summary['n']} mean abs error {summary['mean_abs_error']} "
        f"exact {summary['exact']} wrote {SUMMARY}",
        flush=True,
    )


def main() -> None:
    only = None
    limit_students = None
    limit_assignments = None
    args = sys.argv[1:]
    if "--assignment" in args:
        only = args[args.index("--assignment") + 1]
    if "--limit-students" in args:
        limit_students = int(args[args.index("--limit-students") + 1])
    if "--limit-assignments" in args:
        limit_assignments = int(args[args.index("--limit-assignments") + 1])

    cfg = _config()
    client = CanvasClient(cfg["canvas_base"], cfg["canvas_token"])
    client.post_grade = _refuse  # type: ignore[method-assign]
    done = _done()
    grader = SemifGrader(dtype="bfloat16", max_tokens=16384)
    try:
        assignments = _rubric_assignments(client)
        if only:
            assignments = [item for item in assignments if item["id"] == only]
        if limit_assignments is not None:
            assignments = assignments[:limit_assignments]
        print(f"{len(assignments)} rubric assignments; Canvas is not updated", flush=True)
        for index, assignment in enumerate(assignments, start=1):
            shaped = shape_canvas_rubric(client.assignment_rubric(COURSE_ID, assignment["id"]))
            if not shaped.get("criteria"):
                print(f"[{index}] {assignment['name']}: no usable rubric", flush=True)
                continue
            rubric = Rubric.model_validate(
                {
                    "name": shaped.get("name") or assignment["name"],
                    "prompt": (shaped.get("description") or "")[:4000],
                    "assignment_points": shaped.get("points"),
                    "criteria": shaped["criteria"],
                }
            )
            rows = [
                row
                for row in client.submissions(COURSE_ID, assignment["id"])
                if row.get("score") is not None
            ]
            if limit_students is not None:
                rows = rows[:limit_students]
            print(
                f"[{index}/{len(assignments)}] {rubric.name}: {len(rows)} graded submissions, "
                f"{len(rubric.criteria)} checks",
                flush=True,
            )
            for row in rows:
                key = (assignment["id"], row["student_id"])
                if key in done:
                    continue
                text = _submission_text(client, row)
                record = {
                    "assignment_id": assignment["id"],
                    "assignment": rubric.name,
                    "student_id": row["student_id"],
                    "name": row["name"],
                    "human_score": row["score"],
                    "chars": len(text),
                }
                if len(text) < 40:
                    record["skipped"] = "no readable text"
                    _write(record)
                    done.add(key)
                    continue
                try:
                    graded = _grade_one(grader, rubric, text, rubric.name)
                except SemifError as exc:
                    record["error"] = str(exc)[:500]
                    _write(record)
                    done.add(key)
                    print(f"  {row['name']} error {record['error'][:180]}", flush=True)
                    continue
                record.update(
                    {
                        "model_points": graded["model_points"],
                        "model_expected": graded["model_expected"],
                        "seconds": graded["seconds"],
                        "fallback": graded["fallback"],
                        "by_canvas": _by_canvas(rubric, graded["result"], row.get("rubric_points") or {}),
                    }
                )
                _write(record)
                done.add(key)
                print(
                    f"  {row['name']} human={row['score']} model={graded['model_points']} "
                    f"chars={len(text)} seconds={graded['seconds']}",
                    flush=True,
                )
    finally:
        client.close()
    if OUT.exists():
        _summarize()


if __name__ == "__main__":
    main()
