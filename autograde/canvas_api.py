"""Canvas LMS REST client."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

import httpx

from autograde.documents import bytes_to_markdown


class CanvasError(RuntimeError):
    pass


def canvas_error_detail(response: httpx.Response) -> str:
    text = response.text or ""
    stripped = text.lstrip().lower()
    if response.status_code == 404 or stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        return "That course or assignment was not found. Choose the course again, then the assignment."
    try:
        body = response.json()
        if isinstance(body, dict) and (body.get("message") or body.get("errors")):
            return str(body.get("message") or body.get("errors"))[:300]
    except Exception:
        pass
    return text[:300] or "Canvas request failed"


def html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        match = re.search(r"<([^>]+)>;\s*rel=\"next\"", part)
        if match:
            return match.group(1)
    return None


class CanvasClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        root = base_url.rstrip("/")
        self.base_url = root
        self.client = client or httpx.Client(
            base_url=root,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=True,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        url: str | None = path
        query = params
        rows: list[Any] = []
        while url:
            response = self.client.get(url, params=query)
            self._raise(response)
            payload = response.json()
            if isinstance(payload, list):
                rows.extend(payload)
            else:
                rows.append(payload)
            url = next_link(response.headers.get("link"))
            query = None
        return rows

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        raise CanvasError(f"Canvas {response.status_code}: {canvas_error_detail(response)}")

    def whoami(self) -> dict[str, Any]:
        response = self.client.get("/api/v1/users/self")
        self._raise(response)
        return response.json()

    def courses(self, enrollment_type: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "per_page": 50,
            "include[]": "term",
            "state[]": ["unpublished", "available", "completed"],
        }
        if enrollment_type:
            params["enrollment_type"] = enrollment_type
        rows = self._get_all("/api/v1/courses", params)
        courses = []
        for row in rows:
            if not row.get("name"):
                continue
            term = row.get("term") or {}
            courses.append(
                {
                    "id": str(row["id"]),
                    "name": row.get("name") or row.get("course_code") or str(row["id"]),
                    "code": row.get("course_code") or "",
                    "term": term.get("name") or "",
                    "workflow_state": row.get("workflow_state") or "",
                }
            )
        return courses

    def assignments(self, course_id: str) -> list[dict[str, Any]]:
        rows = self._get_all(
            f"/api/v1/courses/{course_id}/assignments",
            {"per_page": 50, "order_by": "due_at"},
        )
        return [
            {
                "id": str(row["id"]),
                "name": row.get("name") or str(row["id"]),
                "points": row.get("points_possible"),
                "description": html_to_text(row.get("description") or ""),
            }
            for row in rows
        ]

    def assignment_rubric(self, course_id: str, assignment_id: str) -> dict[str, Any]:
        response = self.client.get(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
            params={"include[]": "rubric"},
        )
        self._raise(response)
        body = response.json()
        criteria = [_canvas_criterion(item, index) for index, item in enumerate(body.get("rubric") or [])]
        return {
            "found": bool(criteria),
            "name": body.get("name") or "",
            "points": body.get("points_possible"),
            "description": html_to_text(body.get("description") or ""),
            "criteria": criteria,
        }

    def submissions(self, course_id: str, assignment_id: str) -> list[dict[str, Any]]:
        rows = self._get_all(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
            {"per_page": 50, "include[]": ["user", "rubric_assessment"]},
        )
        found = []
        for row in rows:
            mapped = self._map_submission(row)
            if mapped is not None:
                found.append(mapped)
        return found

    def open_submission(
        self, course_id: str, assignment_id: str, student_id: str = "", anonymous_id: str = ""
    ) -> dict[str, Any] | None:
        """Load the submission open in SpeedGrader, by student id or anonymous id."""
        keys = []
        if student_id:
            keys.append(str(student_id))
        if anonymous_id and anonymous_id not in keys:
            keys.append(str(anonymous_id))
        for key in keys:
            response = self.client.get(
                f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{key}",
                params={"include[]": ["user", "rubric_assessment"]},
            )
            if response.status_code == 404:
                continue
            self._raise(response)
            mapped = self._map_submission(response.json())
            if mapped is not None:
                return mapped
        return None

    @staticmethod
    def _map_submission(row: dict[str, Any]) -> dict[str, Any] | None:
        if row.get("workflow_state") in {"unsubmitted", "deleted"}:
            return None
        if row.get("missing"):
            return None
        user = row.get("user") or {}
        student_id = user.get("id") or row.get("user_id")
        anonymous_id = row.get("anonymous_id") or ""
        if not student_id and not anonymous_id:
            return None
        attachments = []
        for item in row.get("attachments") or []:
            if item.get("url"):
                attachments.append(
                    {
                        "name": item.get("display_name") or item.get("filename") or "file",
                        "url": item["url"],
                        "content_type": item.get("content-type") or item.get("content_type") or "",
                    }
                )
        return {
            "id": str(row.get("id") or student_id or anonymous_id),
            "student_id": str(student_id or ""),
            "anonymous_id": str(anonymous_id),
            "name": user.get("name") or user.get("short_name") or "Student",
            "email": user.get("email") or user.get("login_id") or "",
            "text": html_to_text(row.get("body") or ""),
            "attachments": attachments,
            "posted_grade": row.get("posted_grade") or row.get("grade"),
            "score": row.get("score"),
            "rubric_points": {
                str(key): float((value or {}).get("points"))
                for key, value in (row.get("rubric_assessment") or {}).items()
                if isinstance(value, dict) and value.get("points") is not None
            },
        }

    def download_text(self, url: str, name: str, content_type: str = "") -> str:
        response = self.client.get(url)
        self._raise(response)
        kind = (content_type or response.headers.get("content-type") or "").lower()
        markdown = bytes_to_markdown(response.content, name, kind)
        if markdown is not None:
            return markdown[:200_000]
        if "html" in kind or name.lower().endswith((".html", ".htm")):
            return html_to_text(response.content.decode("utf-8", errors="replace"))[:200_000]
        if kind.startswith("text/") or kind.startswith("application/json") or _looks_textual(name):
            return response.content.decode("utf-8", errors="replace")[:200_000]
        if _mostly_text(response.content):
            return response.content.decode("utf-8", errors="replace")[:200_000]
        return f"[binary file skipped: {name}]"

    def post_grade(self, course_id: str, assignment_id: str, user_id: str, score: float, comment: str) -> dict[str, Any]:
        response = self.client.put(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
            json={
                "submission": {"posted_grade": f"{score:g}"},
                "comment": {"text_comment": comment},
            },
        )
        self._raise(response)
        body = response.json()
        return {"posted_grade": body.get("grade") or body.get("posted_grade"), "user_id": user_id}


def _looks_textual(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(
        (".txt", ".md", ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".json", ".csv", ".html", ".xml", ".r", ".sql")
    )


def _mostly_text(raw: bytes) -> bool:
    if not raw or b"\x00" in raw[:2000]:
        return False
    sample = raw[:2000]
    textish = sum(32 <= byte < 127 or byte in (9, 10, 13) for byte in sample)
    return textish / len(sample) > 0.85


def _canvas_criterion(item: dict[str, Any], index: int) -> dict[str, Any]:
    title = html_to_text(item.get("description") or f"criterion_{index + 1}")
    detail = html_to_text(item.get("long_description") or "")
    ratings = sorted(item.get("ratings") or [], key=lambda rating: float(rating.get("points") or 0))
    levels = []
    for rating in ratings:
        rating_detail = html_to_text(rating.get("long_description") or "")
        label = html_to_text(rating.get("description") or "") or str(rating.get("points"))
        if not rating_detail and float(rating.get("points") or 0) > 0:
            rating_detail = detail or title
        if not rating_detail:
            rating_detail = f"Does not meet {title}"
        levels.append(
            {
                "label": label,
                "points": float(rating.get("points") or 0),
                "description": rating_detail,
            }
        )
    if len(levels) < 2:
        points = float(item.get("points") or 0)
        levels = [
            {"label": "No Marks", "points": 0, "description": f"Does not meet {title}"},
            {"label": "Full Marks", "points": points, "description": detail or title},
        ]
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or f"criterion_{index + 1}"
    return {
        "id": slug,
        "canvas_id": str(item.get("id") or ""),
        "kind": "score",
        "instructions": detail or title,
        "levels": levels,
        "options": [],
        "points_if_yes": 0,
        "points_if_no": 0,
    }
