"""Local web app that grades LMS submissions with JEV."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from autograde.canvas_api import CanvasClient, CanvasError
from autograde.gradescope_api import GradescopeClient, GradescopeError, grades_csv
from autograde.jev_client import JevClient, JevError
from autograde.laya_backend import LayaError, LayaGrader
from autograde.llm_backend import PRESETS, LlmError, LlmGrader, env_api_key
from autograde.semif_backend import MODEL, REVISION, SemifError, SemifGrader

from autograde.rubric import (
    Rubric,
    build_state,
    score_answers,
    score_selection,
    shape_canvas_rubric,
    to_systemone,
)

DATA = Path(__file__).resolve().parents[1] / "data"
STATIC = Path(__file__).resolve().parent / "static"
MAX_CHARS = 12_000

app = FastAPI(title="JEV Autograder")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.canvas = None
app.state.gradescope = None
app.state.session = None
app.state.config = {
    "canvas_base": "https://canvas.instructure.com",
    "canvas_token": "",
    "gradescope_base": "https://www.gradescope.com",
    "jev_base": "http://127.0.0.1:8000",
    "backend": "semif",
    "laya_model": "",
    "laya_device": "",
    "llm_provider": "openai",
    "llm_base_url": PRESETS["openai"]["base_url"],
    "llm_model": PRESETS["openai"]["model"],
    "llm_timeout": 60,
    "llm_keys": {},
}


class ConnectBody(BaseModel):
    canvas_base: str = ""
    canvas_token: str = ""
    gradescope_base: str = "https://www.gradescope.com"
    gradescope_email: str = ""
    gradescope_password: str = ""
    jev_base: str = "http://127.0.0.1:8000"
    backend: str | None = None
    laya_model: str | None = None
    laya_device: str | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_timeout: float | None = None


class GradeTextBody(BaseModel):
    text: str
    assignment_name: str = ""
    rubric: Rubric


class GradeOneBody(BaseModel):
    source: Literal["canvas", "gradescope"]
    course_id: str
    assignment_id: str
    assignment_name: str = ""
    submission_id: str
    rubric: Rubric


class PostBody(BaseModel):
    course_id: str
    assignment_id: str
    student_id: str
    score: float
    comment: str


class CsvBody(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class SessionBody(BaseModel):
    source: Literal["canvas", "gradescope"]
    course_id: str
    assignment_id: str
    assignment_name: str = ""
    rubric: Rubric


class ReviewBody(BaseModel):
    source: Literal["canvas", "gradescope"] = "canvas"
    course_id: str = ""
    assignment_id: str = ""
    assignment_name: str = ""
    submission_id: str = ""
    student_id: str = ""
    anonymous_id: str = ""
    rubric: Rubric | None = None


class CommitBody(BaseModel):
    source: Literal["canvas", "gradescope"] = "canvas"
    course_id: str = ""
    assignment_id: str = ""
    student_id: str
    selections: dict[str, str]
    adjusted: bool = False
    rubric: Rubric | None = None


def _load_session() -> None:
    path = DATA / "session.json"
    if not path.exists():
        return
    app.state.session = json.loads(path.read_text(encoding="utf-8"))


def _save_session(payload: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    app.state.session = payload
    (DATA / "session.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_config() -> None:
    path = DATA / "config.json"
    if not path.exists():
        return
    saved = json.loads(path.read_text(encoding="utf-8"))
    app.state.config.update({key: saved.get(key, app.state.config.get(key, "")) for key in app.state.config})
    if app.state.config.get("canvas_token") and app.state.config.get("canvas_base"):
        _replace_canvas(app.state.config["canvas_base"], app.state.config["canvas_token"])


def _save_config() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "config.json").write_text(json.dumps(app.state.config, indent=2), encoding="utf-8")


def _replace_canvas(base: str, token: str) -> None:
    old = app.state.canvas
    if old is not None:
        old.close()
    app.state.canvas = CanvasClient(base, token) if token else None


@app.on_event("startup")
def _startup() -> None:
    _load_config()
    _load_session()


@app.on_event("shutdown")
def _shutdown() -> None:
    if app.state.canvas is not None:
        app.state.canvas.close()
    if app.state.gradescope is not None:
        app.state.gradescope.close()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict[str, Any]:
    cfg = app.state.config
    jev = {"ok": False, "detail": "not checked", "base": cfg.get("jev_base") or ""}
    try:
        health = JevClient(cfg["jev_base"]).health()
        jev = {"ok": True, "detail": health.get("device") or "up", "model": health.get("model"), "base": cfg.get("jev_base") or ""}
    except JevError as exc:
        jev = {"ok": False, "detail": str(exc), "base": cfg.get("jev_base") or ""}
    semif = getattr(app.state, "semif", None)
    return {
        "jev": jev,
        "scoring": _scoring_status(),
        "semif": {
            "model": MODEL,
            "revision": REVISION,
            "loaded": semif is not None and semif.metadata is not None,
            "device": (semif.metadata or {}).get("device") if semif is not None else None,
        },
        "canvas": {
            "connected": app.state.canvas is not None,
            "base": cfg.get("canvas_base") or "",
            "token_saved": bool(cfg.get("canvas_token")),
        },
        "gradescope": {
            "connected": app.state.gradescope is not None,
            "base": cfg.get("gradescope_base") or "",
            "email": getattr(app.state.gradescope, "email", ""),
        },
    }


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict[str, Any]:
    cfg = app.state.config
    _validate_scoring(body)
    if body.jev_base.strip():
        cfg["jev_base"] = body.jev_base.strip().rstrip("/")
    if body.canvas_base.strip():
        cfg["canvas_base"] = body.canvas_base.strip().rstrip("/")
    if body.canvas_token.strip():
        cfg["canvas_token"] = body.canvas_token.strip()
        _replace_canvas(cfg["canvas_base"], cfg["canvas_token"])
        try:
            app.state.canvas.whoami()
        except CanvasError as exc:
            _replace_canvas(cfg["canvas_base"], "")
            cfg["canvas_token"] = ""
            raise HTTPException(401, str(exc)) from exc
    if body.gradescope_base.strip():
        cfg["gradescope_base"] = body.gradescope_base.strip().rstrip("/")
    if body.gradescope_email.strip() or body.gradescope_password:
        if not body.gradescope_email.strip() or not body.gradescope_password:
            raise HTTPException(400, "Gradescope needs both an email and a password")
        old = app.state.gradescope
        client = GradescopeClient(cfg["gradescope_base"])
        try:
            client.login(body.gradescope_email.strip(), body.gradescope_password)
        except GradescopeError as exc:
            client.close()
            raise HTTPException(401, str(exc)) from exc
        if old is not None:
            old.close()
        app.state.gradescope = client
    _apply_scoring(body)
    _save_config()
    return status()


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    snapshot = status()
    account = ""
    teaching: list[dict[str, Any]] = []
    if app.state.canvas is not None:
        try:
            me = app.state.canvas.whoami()
            account = me.get("name") or me.get("short_name") or ""
            teaching = app.state.canvas.courses(enrollment_type="teacher")
        except CanvasError as exc:
            snapshot["canvas"]["error"] = str(exc)
    snapshot["canvas"]["account"] = account
    snapshot["canvas"]["teaching"] = teaching
    return snapshot


@app.get("/api/courses")
def courses(source: Literal["canvas", "gradescope"]) -> dict[str, Any]:
    try:
        if source == "canvas":
            client = _canvas()
            return {"courses": client.courses()}
        return {"courses": _gradescope().courses()}
    except (CanvasError, GradescopeError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/assignments")
def assignments(source: Literal["canvas", "gradescope"], course_id: str) -> dict[str, Any]:
    try:
        if source == "canvas":
            return {"assignments": _canvas().assignments(course_id)}
        return {"assignments": _gradescope().assignments(course_id)}
    except (CanvasError, GradescopeError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/canvas-rubric")
def canvas_rubric(course_id: str, assignment_id: str) -> dict[str, Any]:
    try:
        payload = _canvas().assignment_rubric(course_id, assignment_id)
    except CanvasError as exc:
        raise HTTPException(502, str(exc)) from exc
    return shape_canvas_rubric(payload)


@app.get("/api/submissions")
def submissions(source: Literal["canvas", "gradescope"], course_id: str, assignment_id: str) -> dict[str, Any]:
    rows = _fetch_rows(source, course_id, assignment_id)
    public = []
    for row in rows:
        public.append(
            {
                "id": row["id"],
                "student_id": row["student_id"],
                "name": row["name"],
                "email": row.get("email") or "",
                "posted_grade": row.get("posted_grade"),
                "has_text": bool(row.get("text") or row.get("attachments")),
            }
        )
    return {"submissions": public}


@app.post("/api/grade-text")
def grade_text(body: GradeTextBody) -> dict[str, Any]:
    return _grade_submission(body.rubric, body.text, body.assignment_name)


@app.post("/api/grade-one")
def grade_one(body: GradeOneBody) -> dict[str, Any]:
    cached = getattr(app.state, "submissions", {}).get(
        (body.source, body.course_id, body.assignment_id, body.submission_id)
    )
    if cached is None:
        raise HTTPException(404, "Load submissions before grading")
    try:
        text = _materialize(body.source, body.course_id, body.assignment_id, cached)
    except (CanvasError, GradescopeError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    graded = _grade_submission(body.rubric, text, body.assignment_name)
    graded["submission_id"] = body.submission_id
    graded["student_id"] = cached.get("student_id")
    graded["name"] = cached.get("name")
    graded["email"] = cached.get("email") or ""
    return graded


@app.post("/api/post-canvas")
def post_canvas(body: PostBody) -> dict[str, Any]:
    try:
        return _canvas().post_grade(
            body.course_id, body.assignment_id, body.student_id, body.score, body.comment
        )
    except CanvasError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/gradescope-csv")
def gradescope_csv(body: CsvBody) -> PlainTextResponse:
    return PlainTextResponse(grades_csv(body.rows), media_type="text/csv")


@app.get("/api/session")
def get_session() -> dict[str, Any]:
    session = app.state.session
    if not session:
        return {"armed": False}
    rubric = session.get("rubric") or {}
    return {
        "armed": True,
        "source": session.get("source") or "",
        "course_id": session.get("course_id") or "",
        "assignment_id": session.get("assignment_id") or "",
        "assignment_name": session.get("assignment_name") or "",
        "criteria": len(rubric.get("criteria") or []),
        "saved_at": session.get("saved_at") or "",
    }


@app.post("/api/session")
def save_session(body: SessionBody) -> dict[str, Any]:
    payload = body.model_dump()
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    _save_session(payload)
    return get_session()


@app.post("/api/review")
def review(body: ReviewBody) -> dict[str, Any]:
    rubric, assignment_name = _rubric_for(body.rubric, body.course_id, body.assignment_id, body.assignment_name)
    try:
        row = _lookup(
            body.source,
            body.course_id,
            body.assignment_id,
            body.submission_id,
            body.student_id,
            body.anonymous_id,
        )
        text = _materialize(body.source, body.course_id, body.assignment_id, row)
    except (CanvasError, GradescopeError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    graded = _grade_submission(rubric, text, assignment_name)
    target = rubric.assignment_points if rubric.assignment_points is not None else rubric.max_points()
    return {
        "submission_id": row["id"],
        "student_id": row.get("student_id") or "",
        "name": row.get("name") or "",
        "email": row.get("email") or "",
        "text": text,
        "assignment_points": target,
        "grade": graded,
    }


@app.post("/api/commit")
def commit(body: CommitBody) -> dict[str, Any]:
    rubric, _name = _rubric_for(body.rubric, body.course_id, body.assignment_id, "")
    try:
        graded = score_selection(rubric, body.selections)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    posted = None
    if body.source == "canvas":
        course_id = body.course_id or (app.state.session or {}).get("course_id") or ""
        assignment_id = body.assignment_id or (app.state.session or {}).get("assignment_id") or ""
        if not course_id or not assignment_id or not body.student_id:
            raise HTTPException(400, "Canvas posting needs a course, assignment, and student")
        try:
            posted = _canvas().post_grade(
                course_id, assignment_id, body.student_id, graded.scaled_points, graded.comment
            )
        except CanvasError as exc:
            raise HTTPException(502, str(exc)) from exc
    return {"posted": posted, "grade": graded.model_dump(), "adjusted": body.adjusted}


def _fetch_rows(source: str, course_id: str, assignment_id: str) -> list[dict[str, Any]]:
    try:
        if source == "canvas":
            rows = _canvas().submissions(course_id, assignment_id)
        else:
            rows = _gradescope().submissions(course_id, assignment_id)
    except (CanvasError, GradescopeError) as exc:
        raise HTTPException(502, str(exc)) from exc
    cache = getattr(app.state, "submissions", None)
    if not isinstance(cache, dict):
        cache = {}
    for key in list(cache):
        if key[:3] == (source, course_id, assignment_id):
            del cache[key]
    for row in rows:
        cache[(source, course_id, assignment_id, row["id"])] = row
    app.state.submissions = cache
    return rows


def _match_row(
    rows: list[dict[str, Any]], submission_id: str, student_id: str, anonymous_id: str = ""
) -> dict[str, Any] | None:
    for row in rows:
        if submission_id and row.get("id") == submission_id:
            return row
        if student_id and row.get("student_id") == student_id:
            return row
        if anonymous_id and row.get("anonymous_id") == anonymous_id:
            return row
    return None


def _lookup(
    source: str,
    course_id: str,
    assignment_id: str,
    submission_id: str,
    student_id: str,
    anonymous_id: str = "",
) -> dict[str, Any]:
    if not submission_id and not student_id and not anonymous_id:
        raise HTTPException(400, "A submission or student id is required")
    if source == "canvas" and course_id and assignment_id and (student_id or anonymous_id):
        try:
            opened = _canvas().open_submission(course_id, assignment_id, student_id, anonymous_id)
        except CanvasError as exc:
            raise HTTPException(502, str(exc)) from exc
        if opened is not None:
            return opened
    cache = getattr(app.state, "submissions", None)
    cached = []
    if isinstance(cache, dict):
        cached = [row for key, row in cache.items() if key[:3] == (source, course_id, assignment_id)]
    found = _match_row(cached, submission_id, student_id, anonymous_id)
    if found is None:
        found = _match_row(_fetch_rows(source, course_id, assignment_id), submission_id, student_id, anonymous_id)
    if found is None:
        raise HTTPException(404, "No submission for that student")
    return found


def _same_id(saved: object, current: str) -> bool:
    if not current:
        return True
    return str(saved or "") == str(current)


def _rubric_from_canvas(course_id: str, assignment_id: str) -> tuple[Rubric, str]:
    """Use the rubric attached to the assignment open in SpeedGrader."""
    try:
        payload = shape_canvas_rubric(_canvas().assignment_rubric(course_id, assignment_id))
    except CanvasError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not payload.get("found"):
        raise HTTPException(409, "This assignment has no Canvas rubric.")
    try:
        rubric = Rubric.model_validate(
            {
                "name": payload.get("name") or "Rubric",
                "prompt": payload.get("description") or "",
                "assignment_points": payload.get("points"),
                "criteria": payload.get("criteria") or [],
            }
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    name = payload.get("name") or ""
    _save_session(
        {
            "source": "canvas",
            "course_id": str(course_id),
            "assignment_id": str(assignment_id),
            "assignment_name": name,
            "rubric": rubric.model_dump(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return rubric, name


def _rubric_for(
    rubric: Rubric | None, course_id: str, assignment_id: str, assignment_name: str
) -> tuple[Rubric, str]:
    if rubric is not None:
        return rubric, assignment_name
    session = app.state.session or {}
    if (
        session.get("rubric")
        and _same_id(session.get("course_id"), course_id)
        and _same_id(session.get("assignment_id"), assignment_id)
    ):
        return Rubric.model_validate(session["rubric"]), assignment_name or session.get("assignment_name") or ""
    if course_id and assignment_id:
        return _rubric_from_canvas(course_id, assignment_id)
    raise HTTPException(409, "Open the autograder and click Start review so this assignment has a rubric")


def _canvas() -> CanvasClient:
    if app.state.canvas is None:
        raise HTTPException(400, "Connect a Canvas API token first")
    return app.state.canvas


def _gradescope() -> GradescopeClient:
    if app.state.gradescope is None:
        raise HTTPException(400, "Log in to Gradescope first")
    return app.state.gradescope


def _materialize(source: str, course_id: str, assignment_id: str, row: dict[str, Any]) -> str:
    parts = [row.get("text") or ""]
    if source == "canvas":
        for item in row.get("attachments") or []:
            parts.append(
                _canvas().download_text(item["url"], item.get("name") or "file", item.get("content_type") or "")
            )
    elif source == "gradescope":
        parts.append(
            _gradescope().submission_text(course_id, assignment_id, row["id"])
        )
    text = "\n\n".join(part.strip() for part in parts if part and part.strip())
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[truncated]"
    if not text:
        raise ValueError("This submission has no text JEV can read")
    return text


def _laya_importable() -> bool:
    try:
        import laya  # noqa: F401
    except ImportError:
        return False
    return True


def _llm_key(provider: str) -> tuple[str, str]:
    """Return the key and whether it came from the saved file or the environment."""
    keys = app.state.config.get("llm_keys") or {}
    saved = str(keys.get(provider) or "").strip() if isinstance(keys, dict) else ""
    if saved:
        return saved, "saved"
    if env_api_key(provider):
        return env_api_key(provider), "env"
    return "", "missing"


def _scoring_status() -> dict[str, Any]:
    cfg = app.state.config
    provider = str(cfg.get("llm_provider") or "openai")
    preset = PRESETS.get(provider) or PRESETS["openai"]
    _key, source = _llm_key(provider if provider in PRESETS else "openai")
    laya = getattr(app.state, "laya", None)
    return {
        "backend": cfg.get("backend") or "semif",
        "laya_model": cfg.get("laya_model") or "",
        "laya_device": (laya.metadata or {}).get("device") if laya is not None and laya.metadata else (cfg.get("laya_device") or ""),
        "laya_importable": _laya_importable(),
        "laya_loaded": bool(laya is not None and laya.loaded),
        "llm_provider": provider if provider in PRESETS else "openai",
        "llm_base_url": cfg.get("llm_base_url") or preset["base_url"],
        "llm_model": cfg.get("llm_model") or preset["model"],
        "llm_timeout": cfg.get("llm_timeout") or 60,
        "llm_key_source": source,
    }


def _validate_scoring(body: ConnectBody) -> None:
    if body.backend is not None and body.backend.strip().lower() not in {"semif", "laya", "llm"}:
        raise HTTPException(400, "Scoring backend must be semif, laya, or llm")
    if body.llm_provider is not None and body.llm_provider.strip().lower() not in PRESETS:
        raise HTTPException(400, "LLM provider must be anthropic, openai, ollama, or lmstudio")
    if body.llm_timeout is not None and body.llm_timeout <= 0:
        raise HTTPException(400, "Timeout must be greater than 0")


def _apply_scoring(body: ConnectBody) -> None:
    cfg = app.state.config
    if body.backend is not None:
        cfg["backend"] = body.backend.strip().lower()
    if body.laya_model is not None:
        cfg["laya_model"] = body.laya_model.strip()
    if body.laya_device is not None:
        cfg["laya_device"] = body.laya_device.strip()
    if body.llm_provider is not None:
        cfg["llm_provider"] = body.llm_provider.strip().lower()
    if body.llm_base_url is not None:
        cfg["llm_base_url"] = body.llm_base_url.strip().rstrip("/")
    if body.llm_model is not None:
        cfg["llm_model"] = body.llm_model.strip()
    if body.llm_timeout is not None:
        cfg["llm_timeout"] = body.llm_timeout
    if body.llm_api_key:
        provider = str(cfg.get("llm_provider") or "openai")
        keys = dict(cfg.get("llm_keys") or {})
        keys[provider] = body.llm_api_key.strip()
        cfg["llm_keys"] = keys


def _semif() -> SemifGrader:
    grader = getattr(app.state, "semif", None)
    if grader is None:
        grader = SemifGrader()
        app.state.semif = grader
    return grader


def _laya() -> LayaGrader:
    cfg = app.state.config
    signature = (str(cfg.get("laya_model") or ""), str(cfg.get("laya_device") or ""))
    grader = getattr(app.state, "laya", None)
    if grader is None or grader.signature != signature:
        grader = LayaGrader(model=signature[0], device=signature[1] or None)
        app.state.laya = grader
    return grader


def _llm() -> LlmGrader:
    cfg = app.state.config
    provider = str(cfg.get("llm_provider") or "openai")
    if provider not in PRESETS:
        raise LlmError(f"Unknown LLM provider {provider!r}")
    preset = PRESETS[provider]
    key, _source = _llm_key(provider)
    timeout = float(cfg.get("llm_timeout") or 60)
    return LlmGrader(
        provider=provider,
        base_url=str(cfg.get("llm_base_url") or preset["base_url"]),
        api_key=key,
        model=str(cfg.get("llm_model") or preset["model"]),
        timeout=timeout,
    )


def _grade_submission(rubric: Rubric, text: str, assignment_name: str) -> dict[str, Any]:
    state = build_state(rubric, text, assignment_name)
    request = to_systemone(rubric, state)
    backend = str(app.state.config.get("backend") or "semif")
    try:
        if backend == "semif":
            payload = _semif().grade(request)
        elif backend == "laya":
            payload = _laya().grade(request)
        elif backend == "llm":
            payload = _llm().grade(request)
        else:
            raise HTTPException(400, f"Unknown scoring backend {backend}")
    except (SemifError, LayaError, LlmError) as exc:
        raise HTTPException(502, str(exc)) from exc
    result = score_answers(rubric, payload.get("answers") or {})
    return result.model_dump()


def main() -> None:
    uvicorn.run("autograde.app:app", host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
