"""Grade INFO 201 submissions locally and compare with Canvas human scores.

Reads Canvas. Never posts a grade or comment.
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
from autograde.rubric import Rubric, build_state, score_answers, score_selection, shape_canvas_rubric, to_systemone  # noqa: E402
from autograde.semif_backend import SemifError, SemifGrader  # noqa: E402

COURSE_ID = "1862977"
# Final Project -- Final Submission: text answers, a rubric, and human scores
# that are not all identical.
ASSIGNMENT_ID = "11168610"
LIMIT = 6
CONFIGS = [ROOT / "data" / "config.json"]
if os.environ.get("CANVAS_CONFIG"):
    CONFIGS.append(Path(os.environ["CANVAS_CONFIG"]))


def _refuse(*_args, **_kwargs):
    raise RuntimeError("This comparison is read-only and cannot post Canvas grades")


def _config() -> dict:
    for path in CONFIGS:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit("No Canvas config.json found")


READABLE = (
    ".html",
    ".htm",
    ".qmd",
    ".md",
    ".txt",
    ".r",
    ".rmd",
    ".ipynb",
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
)


def _readable_files(row: dict) -> list[dict]:
    found = []
    for item in row.get("attachments") or []:
        name = (item.get("name") or "").lower()
        if name.endswith(READABLE):
            found.append(item)
    return found


def _spread(rows: list[dict], limit: int, attachments: bool) -> list[dict]:
    """One submission from each human score, uncommon scores first."""
    buckets: dict[float, list[dict]] = {}
    for row in rows:
        if row.get("score") is None:
            continue
        if attachments:
            if not _readable_files(row):
                continue
        else:
            if len((row.get("text") or "").strip()) < 60 or not row.get("rubric_points"):
                continue
        buckets.setdefault(float(row["score"]), []).append(row)
    ordered = sorted(buckets, key=lambda score: (len(buckets[score]), score))
    picked: list[dict] = []
    seen: set[str] = set()
    for score in ordered:
        group = list(buckets[score])
        if attachments:
            group.sort(key=lambda row: (0 if row.get("rubric_points") else 1, row["student_id"]))
            choice = group[0]
        else:
            group.sort(key=lambda row: len(row["text"]))
            choice = group[len(group) // 2]
        if choice["student_id"] in seen:
            continue
        picked.append(choice)
        seen.add(choice["student_id"])
        if len(picked) >= limit:
            return picked
    return picked


def _body(client: CanvasClient, row: dict, attachments: bool) -> str:
    parts = [(row.get("text") or "").strip()]
    if attachments:
        for item in _readable_files(row):
            parts.append(client.download_text(item["url"], item.get("name") or "file", item.get("content_type") or ""))
    text = "\n\n".join(part.strip() for part in parts if part and part.strip())
    if text.startswith("[PDF") or text.startswith("[binary"):
        return ""
    return text[:12000]


def _human_by_canvas(row: dict, rubric: Rubric) -> dict[str, float | None]:
    points = row.get("rubric_points") or {}
    grouped: dict[str, float | None] = {}
    for item in rubric.criteria:
        key = item.canvas_id or item.id
        grouped[key] = points.get(item.canvas_id) if item.canvas_id else None
    return grouped


def main() -> None:
    assignment_id = ASSIGNMENT_ID
    limit = LIMIT
    attachments = False
    args = sys.argv[1:]
    if "--attachments" in args:
        attachments = True
        args.remove("--attachments")
    if args:
        assignment_id = args[0]
    if len(args) > 1:
        limit = int(args[1])
    cfg = _config()
    client = CanvasClient(cfg["canvas_base"], cfg["canvas_token"])
    client.post_grade = _refuse  # type: ignore[method-assign]
    try:
        raw = client.assignment_rubric(COURSE_ID, assignment_id)
        shaped = shape_canvas_rubric(raw)
        rubric = Rubric.model_validate(
            {
                "name": shaped.get("name") or "Rubric",
                "prompt": (shaped.get("description") or "")[:4000],
                "assignment_points": shaped.get("points"),
                "criteria": shaped["criteria"],
            }
        )
        rows = _spread(client.submissions(COURSE_ID, assignment_id), limit, attachments)
        for row in rows:
            row["text"] = _body(client, row, attachments)
        rows = [row for row in rows if len(row["text"]) >= 60]
    finally:
        client.close()

    print(
        f"{rubric.name}: {len(rubric.criteria)} criteria, "
        f"{rubric.max_points():g} rubric points, matched_by={shaped.get('matched_by')}"
    )
    for item in rubric.criteria:
        print(f"  {item.id} canvas={item.canvas_id or '-'} {item.kind} max={item.max_points():g}")
    print(f"scoring {len(rows)} submissions locally; Canvas is not updated")
    if not rows:
        raise SystemExit("No graded text submissions with a rubric assessment")

    grader = SemifGrader(dtype="bfloat16", max_tokens=16384)
    report = []
    for index, row in enumerate(rows, start=1):
        state = build_state(rubric, row["text"], rubric.name)
        print(f"[{index}/{len(rows)}] {row['name']} chars={len(row['text'])} human={row['score']}", flush=True)
        try:
            payload = grader.grade(to_systemone(rubric, state))
        except SemifError as exc:
            print(f"  skipped: {exc}", flush=True)
            report.append(
                {
                    "student_id": row["student_id"],
                    "name": row["name"],
                    "chars": len(row["text"]),
                    "human_score": row["score"],
                    "error": str(exc),
                }
            )
            continue
        result = score_answers(rubric, payload["answers"])
        posted = score_selection(rubric, {item.id: item.model_key for item in result.criteria})
        human = _human_by_canvas(row, rubric)
        criteria = []
        for item, scored in zip(rubric.criteria, result.criteria):
            yes = next((rating.probability for rating in scored.ratings if rating.key == "yes"), None)
            criteria.append(
                {
                    "id": item.id,
                    "canvas_id": item.canvas_id,
                    "model_points": scored.points,
                    "model_label": scored.label,
                    "yes_probability": yes,
                }
            )
        by_canvas = []
        index_by_id: dict[str, dict] = {}
        for item, scored in zip(rubric.criteria, criteria):
            key = item.canvas_id or item.id
            group = index_by_id.get(key)
            if group is None:
                group = {
                    "canvas_id": key,
                    "human": human.get(key),
                    "model_points": 0.0,
                    "checks": [],
                }
                index_by_id[key] = group
                by_canvas.append(group)
            group["model_points"] = round(group["model_points"] + scored["model_points"], 2)
            group["checks"].append(item.id)
        timing = payload.get("timing") or {}
        entry = {
            "student_id": row["student_id"],
            "name": row["name"],
            "chars": len(row["text"]),
            "human_score": row["score"],
            "human_posted": row.get("posted_grade"),
            "model_points": posted.scaled_points,
            "model_expected": result.scaled_points,
            "seconds": round(float(timing.get("total_seconds") or 0), 1),
            "by_canvas": by_canvas,
            "criteria": criteria,
        }
        report.append(entry)
        print(
            f"  model={posted.scaled_points:g} expected={result.scaled_points:g} "
            f"seconds={entry['seconds']}",
            flush=True,
        )

    errors = [
        abs(float(item["human_score"]) - float(item["model_points"]))
        for item in report
        if "model_points" in item
    ]
    if not errors:
        raise SystemExit("No submission was scored")
    mean_abs = sum(errors) / len(errors)
    exact = sum(1 for error in errors if error < 0.05)
    out = ROOT / "data" / f"info201-{assignment_id}-compare.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "course_id": COURSE_ID,
                "assignment_id": assignment_id,
                "assignment": rubric.name,
                "read_only": True,
                "mean_abs_error": round(mean_abs, 3),
                "exact": exact,
                "n": len(report),
                "rows": report,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"mean abs error {mean_abs:.2f} on {len(report)}; exact {exact}; wrote {out}")


if __name__ == "__main__":
    main()
