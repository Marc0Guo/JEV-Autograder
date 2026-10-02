"""Turn a rubric into a JEV System One request and a point score."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Level(BaseModel):
    label: str
    points: float
    description: str = ""


class ChoiceOption(BaseModel):
    name: str
    points: float
    description: str = ""


class Criterion(BaseModel):
    id: str
    canvas_id: str = ""
    instructions: str
    kind: Literal["score", "choice", "noul"]
    levels: list[Level] = Field(default_factory=list)
    options: list[ChoiceOption] = Field(default_factory=list)
    points_if_yes: float = 0
    points_if_no: float = 0
    yes_description: str = "The submission meets this requirement."
    no_description: str = "The submission does not meet this requirement."

    @model_validator(mode="after")
    def _shape(self):
        if self.kind == "score" and len(self.levels) < 2:
            raise ValueError(f"{self.id}: a score criterion needs at least two levels")
        if self.kind == "choice" and len(self.options) < 2:
            raise ValueError(f"{self.id}: a choice criterion needs at least two options")
        return self

    def max_points(self) -> float:
        if self.kind == "score":
            return max(level.points for level in self.levels)
        if self.kind == "choice":
            return max(option.points for option in self.options)
        return max(self.points_if_yes, self.points_if_no)


class Rubric(BaseModel):
    name: str = "Rubric"
    prompt: str = ""
    assignment_points: float | None = None
    min_confidence: float = 0.35
    noul_threshold: float = 0.8
    criteria: list[Criterion] = Field(min_length=1)

    def max_points(self) -> float:
        return sum(item.max_points() for item in self.criteria)


class Rating(BaseModel):
    key: str
    label: str
    points: float
    probability: float = 0


class CriterionScore(BaseModel):
    id: str
    label: str
    points: float
    max_points: float
    confidence: float
    detail: str
    model_key: str
    ratings: list[Rating] = Field(default_factory=list)


class GradeResult(BaseModel):
    points: float
    max_points: float
    scaled_points: float
    needs_review: bool
    comment: str
    criteria: list[CriterionScore]


def build_state(rubric: Rubric, submission: str, assignment_name: str = "") -> str:
    parts = []
    if assignment_name:
        parts.append(f"Assignment:\n{assignment_name}")
    if rubric.prompt.strip():
        parts.append(f"Prompt:\n{rubric.prompt.strip()}")
    parts.append(f"Student submission:\n{submission.strip()}")
    return "\n\n".join(parts)


def to_systemone(rubric: Rubric, state: str) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for item in rubric.criteria:
        if item.kind == "score":
            questions[item.id] = {
                "type": "score",
                "instructions": item.instructions,
                "criteria": [level.description or level.label for level in item.levels],
            }
        elif item.kind == "choice":
            questions[item.id] = {
                "type": "choice",
                "instructions": item.instructions,
                "criteria": {
                    option.name: option.description or option.name for option in item.options
                },
            }
        else:
            statement = (item.instructions or item.yes_description).strip()
            questions[item.id] = {
                "type": "noul",
                "instructions": f"The following statement is true of the submission: {statement}",
                "criteria": {
                    "true": "The statement is true.",
                    "false": "The statement is not true.",
                },
            }
    return {"state": state, "model": "jev-latest", "questions": questions}


def _expected(weights: dict[str, float], values: dict[str, float]) -> float:
    total = 0.0
    mass = 0.0
    for key, points in values.items():
        prob = float(weights.get(key, 0.0))
        total += prob * points
        mass += prob
    if mass <= 0:
        return 0.0
    return total / mass


def _noul_confidence(probability: float) -> float:
    p = min(1.0, max(0.0, probability))
    return abs(2 * p - 1)


def blank_ratings(item: Criterion) -> list[Rating]:
    if item.kind == "score":
        return [
            Rating(key=str(index), label=level.label, points=level.points)
            for index, level in enumerate(item.levels)
        ]
    if item.kind == "choice":
        return [Rating(key=option.name, label=option.name, points=option.points) for option in item.options]
    return [
        Rating(key="yes", label="yes", points=item.points_if_yes),
        Rating(key="no", label="no", points=item.points_if_no),
    ]


def _scale(rubric: Rubric, raw: float) -> tuple[float, float]:
    maximum = rubric.max_points()
    target = rubric.assignment_points if rubric.assignment_points is not None else maximum
    scaled = round(raw / maximum * target, 2) if maximum else 0.0
    return scaled, target


def _finish(rubric: Rubric, rows: list[CriterionScore], header: str) -> GradeResult:
    raw = round(sum(row.points for row in rows), 2)
    maximum = rubric.max_points()
    scaled, target = _scale(rubric, raw)
    review = any(row.confidence < rubric.min_confidence for row in rows)
    lines = [header.format(scaled=f"{scaled:g}", target=f"{target:g}")]
    for row in rows:
        lines.append(
            f"- {row.id}: {row.label} ({row.detail}); "
            f"{row.points:g}/{row.max_points:g} points; confidence {row.confidence:.2f}"
        )
    return GradeResult(
        points=raw,
        max_points=maximum,
        scaled_points=scaled,
        needs_review=review,
        comment="\n".join(lines),
        criteria=rows,
    )


def score_answers(rubric: Rubric, answers: dict[str, Any]) -> GradeResult:
    rows: list[CriterionScore] = []
    for item in rubric.criteria:
        answer = answers.get(item.id)
        if not isinstance(answer, dict):
            raise ValueError(f"JEV did not answer criterion {item.id}")
        if item.kind == "score":
            probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
            values = {str(i): level.points for i, level in enumerate(item.levels)}
            points = _expected(probs, values)
            index = max(range(len(item.levels)), key=lambda i: probs.get(str(i), 0.0))
            label = item.levels[index].label
            confidence = float(answer.get("confidence") or 0.0)
            detail = ", ".join(
                f"{item.levels[i].label} {probs.get(str(i), 0.0):.0%}"
                for i in range(len(item.levels))
            )
            ratings = [
                Rating(
                    key=str(i),
                    label=level.label,
                    points=level.points,
                    probability=round(probs.get(str(i), 0.0), 4),
                )
                for i, level in enumerate(item.levels)
            ]
            model_key = str(index)
        elif item.kind == "choice":
            probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
            values = {option.name: option.points for option in item.options}
            points = _expected(probs, values)
            label = str(answer.get("choice") or max(values, key=lambda name: probs.get(name, 0.0)))
            confidence = float(answer.get("confidence") or 0.0)
            detail = ", ".join(f"{name} {probs.get(name, 0.0):.0%}" for name in values)
            ratings = [
                Rating(
                    key=option.name,
                    label=option.name,
                    points=option.points,
                    probability=round(probs.get(option.name, 0.0), 4),
                )
                for option in item.options
            ]
            model_key = label if any(rating.key == label for rating in ratings) else ratings[0].key
        else:
            probability = float(answer.get("noul") or 0.0)
            awarded = probability >= rubric.noul_threshold
            points = item.points_if_yes if awarded else item.points_if_no
            label = "yes" if awarded else "no"
            confidence = _noul_confidence(probability)
            detail = f"yes {probability:.0%}; award at {rubric.noul_threshold:.0%}"
            ratings = [
                Rating(key="yes", label="yes", points=item.points_if_yes, probability=round(probability, 4)),
                Rating(key="no", label="no", points=item.points_if_no, probability=round(1 - probability, 4)),
            ]
            model_key = label
        rows.append(
            CriterionScore(
                id=item.id,
                label=label,
                points=round(points, 2),
                max_points=item.max_points(),
                confidence=round(confidence, 3),
                detail=detail,
                model_key=model_key,
                ratings=ratings,
            )
        )

    review = any(row.confidence < rubric.min_confidence for row in rows)
    header = "JEV grade {scaled} / {target}" + (" — needs review" if review else "")
    return _finish(rubric, rows, header)


def score_selection(rubric: Rubric, selections: dict[str, str]) -> GradeResult:
    """Turn clicked ratings into the score and comment that get posted."""
    rows: list[CriterionScore] = []
    for item in rubric.criteria:
        ratings = blank_ratings(item)
        chosen_key = selections.get(item.id)
        chosen = next((rating for rating in ratings if rating.key == chosen_key), None)
        if chosen is None:
            raise ValueError(f"Choose a rating for {item.id}")
        rows.append(
            CriterionScore(
                id=item.id,
                label=chosen.label,
                points=round(chosen.points, 2),
                max_points=item.max_points(),
                confidence=1,
                detail=chosen.label,
                model_key=chosen.key,
                ratings=ratings,
            )
        )
    raw = round(sum(row.points for row in rows), 2)
    maximum = rubric.max_points()
    scaled, target = _scale(rubric, raw)
    lines = [f"{scaled:g} / {target:g}"]
    for row in rows:
        lines.append(f"- {row.id}: {row.label} ({row.points:g}/{row.max_points:g})")
    return GradeResult(
        points=raw,
        max_points=maximum,
        scaled_points=scaled,
        needs_review=False,
        comment="\n".join(lines),
        criteria=rows,
    )


MODE_CRITERIA = {
    "score": "Ordered levels of quality, from weaker to stronger, each with its own points.",
    "choice": "Named alternatives that are not an ordered scale and not only yes versus no.",
    "noul": "A yes/no or met/not-met check, including full marks versus no marks.",
}


def heuristic_mode(item: dict) -> str:
    levels = item.get("levels") or []
    labels = " ".join(str(level.get("label") or "").lower() for level in levels)
    binary = {"full marks", "no marks", "yes", "no", "full", "none"}
    if len(levels) <= 2 and any(word in labels for word in binary):
        return "noul"
    if len(levels) >= 3:
        return "score"
    return "choice" if len(levels) > 2 else "noul"


def mode_request(criteria: list[dict]) -> dict:
    questions = {}
    for item in criteria:
        ratings = "\n".join(
            f"- {level.get('label')} ({level.get('points')} points): {level.get('description')}"
            for level in item.get("levels") or []
        )
        questions[item["id"]] = {
            "type": "choice",
            "instructions": (
                f"Criterion: {item['id']}\n"
                f"What the grader checks:\n{(item.get('instructions') or '')[:800]}\n"
                f"Canvas ratings:\n{ratings}\n"
                "Choose the JEV output mode for this criterion."
            ),
            "criteria": MODE_CRITERIA,
        }
    return {
        "state": "Match each Canvas rubric criterion to one JEV output mode.",
        "model": "jev-latest",
        "questions": questions,
    }


def _option_name(label: str, index: int, used: set[str]) -> str:
    name = re_slug(label) or f"option_{index + 1}"
    base = name
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def re_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:40]


_POINT_END = re.compile(
    r"^(?P<body>.*?)\s*[\(\[]\s*(?P<points>\d+(?:\.\d+)?)\s*(?:points?|pts?)\s*[\)\]]\s*[?.]?\s*$",
    re.IGNORECASE,
)
_POINT_START = re.compile(
    r"^(?P<points>\d+(?:\.\d+)?)\s*(?:points?|pts?)\s*[:\-]\s*(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[-*•]\s+")


def _point_line(line: str) -> dict[str, Any] | None:
    text = _BULLET.sub("", line.strip())
    match = _POINT_END.match(text) or _POINT_START.match(text)
    if not match:
        return None
    body = match.group("body").strip().rstrip("?:").strip()
    if not body:
        return None
    return {"body": body, "points": float(match.group("points")), "bullet": bool(_BULLET.match(line.strip()))}


def checklist_items(text: str) -> list[tuple[str, float]]:
    """Pull point-tagged requirements out of a criterion description.

    A heading whose points equal the following bullets is a subtotal, so the
    bullets become the checks and the heading is kept only as context.
    """
    parsed = []
    for line in text.splitlines():
        if not line.strip():
            continue
        found = _point_line(line)
        if found:
            parsed.append(found)
    items: list[tuple[str, float]] = []
    index = 0
    while index < len(parsed):
        row = parsed[index]
        kids: list[dict[str, Any]] = []
        cursor = index + 1
        while cursor < len(parsed) and parsed[cursor]["bullet"]:
            kids.append(parsed[cursor])
            cursor += 1
        if kids and abs(sum(kid["points"] for kid in kids) - row["points"]) < 0.05:
            for kid in kids:
                items.append((f"{row['body']}: {kid['body']}", kid["points"]))
            index = cursor
            continue
        items.append((row["body"], row["points"]))
        index += 1
    return items


def _binary_ratings(item: dict) -> bool:
    levels = item.get("levels") or []
    if len(levels) != 2:
        return False
    labels = " ".join(str(level.get("label") or "").lower() for level in levels)
    return any(word in labels for word in ("full mark", "no mark", "full marks", "no marks", "yes", "no"))


def _noul_check(check_id: str, statement: str, points: float, canvas_id: str = "") -> dict[str, Any]:
    claim = statement if statement.endswith(".") else f"{statement}."
    return {
        "id": check_id,
        "canvas_id": canvas_id,
        "kind": "noul",
        "instructions": claim,
        "levels": [],
        "options": [],
        "points_if_yes": points,
        "points_if_no": 0,
        "yes_description": claim,
        "no_description": "This statement is not true of the submission.",
    }


def expand_canvas_criterion(item: dict, used: set[str]) -> list[dict] | None:
    """Turn one Canvas criterion into checklist nouls, or leave it for mode matching."""
    levels = item.get("levels") or []
    maximum = max((float(level.get("points") or 0) for level in levels), default=0.0)
    checks = checklist_items(item.get("instructions") or "")
    if len(checks) >= 2 and abs(sum(points for _, points in checks) - maximum) < 0.05:
        rows = []
        parent = str(item.get("canvas_id") or "")
        for statement, points in checks:
            rows.append(_noul_check(_option_name(statement, len(used), used), statement, points, parent))
        return rows
    if _binary_ratings(item):
        statement = (item.get("instructions") or item["id"]).strip() or item["id"]
        return [
            _noul_check(
                _option_name(item["id"], 0, used),
                statement,
                maximum,
                str(item.get("canvas_id") or ""),
            )
        ]
    return None


def shape_canvas_rubric(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand checklist and binary Canvas criteria. Does not call a model or Canvas."""
    if not payload.get("found"):
        return payload
    used: set[str] = set()
    final: list[dict[str, Any]] = []
    pending: list[tuple[int, dict[str, Any]]] = []
    for item in payload["criteria"]:
        parts = expand_canvas_criterion(item, used)
        if parts is not None:
            final.extend(parts)
            continue
        pending.append((len(final), item))
        final.append(item)
    matched_by = "checklist"
    if pending:
        modes = {item["id"]: heuristic_mode(item) for _, item in pending}
        matched_by = "heuristic"
        for slot, item in pending:
            final[slot] = apply_mode(item, modes.get(item["id"], "score"))
    shaped = dict(payload)
    shaped["criteria"] = final
    shaped["matched_by"] = matched_by
    return shaped


def apply_mode(item: dict, mode: str) -> dict:
    chosen = mode if mode in MODE_CRITERIA else heuristic_mode(item)
    levels = list(item.get("levels") or [])
    shaped = {
        "id": item["id"],
        "canvas_id": str(item.get("canvas_id") or ""),
        "kind": chosen,
        "instructions": item.get("instructions") or item["id"],
        "levels": [],
        "options": [],
        "points_if_yes": 0,
        "points_if_no": 0,
        "yes_description": "",
        "no_description": "",
    }
    if chosen == "noul":
        high = max(levels, key=lambda level: float(level.get("points") or 0))
        low = min(levels, key=lambda level: float(level.get("points") or 0))
        shaped["points_if_yes"] = float(high.get("points") or 0)
        shaped["points_if_no"] = float(low.get("points") or 0)
        shaped["yes_description"] = high.get("description") or high.get("label") or "Meets the criterion"
        shaped["no_description"] = low.get("description") or low.get("label") or "Does not meet the criterion"
        return shaped
    if chosen == "choice":
        used: set[str] = set()
        shaped["options"] = [
            {
                "name": _option_name(str(level.get("label") or ""), index, used),
                "points": float(level.get("points") or 0),
                "description": level.get("description") or level.get("label") or "",
            }
            for index, level in enumerate(levels)
        ]
        return shaped
    shaped["levels"] = [
        {
            "label": level.get("label") or str(level.get("points")),
            "points": float(level.get("points") or 0),
            "description": level.get("description") or "",
        }
        for level in levels
    ]
    return shaped
