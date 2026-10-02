"""Gradescope instructor session.

Gradescope does not publish a grade-write REST API. This client logs in the
same way the website does, reads courses, assignments, and submission files,
and builds a CSV an instructor can import. Canvas remains the grade write path.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from bs4 import BeautifulSoup

from autograde.documents import bytes_to_markdown


class GradescopeError(RuntimeError):
    pass


class GradescopeClient:
    def __init__(self, base_url: str = "https://www.gradescope.com", client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=60, follow_redirects=True)
        self._owns_client = client is None
        self.email = ""

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def login(self, email: str, password: str) -> None:
        home = self.client.get(self.base_url + "/")
        if home.status_code >= 400:
            raise GradescopeError(f"Gradescope {home.status_code} on login page")
        token = _auth_token(home.text)
        response = self.client.post(
            f"{self.base_url}/login",
            data={
                "utf8": "✓",
                "session[email]": email,
                "session[password]": password,
                "session[remember_me]": "0",
                "session[remember_me_sso]": "0",
                "commit": "Log In",
                "authenticity_token": token,
            },
        )
        if response.status_code >= 400 or "Invalid email/password" in response.text:
            raise GradescopeError("Gradescope rejected the email or password")
        if not _csrf(response.text) and "/login" in str(response.url):
            raise GradescopeError("Gradescope login did not open an account session")
        csrf = _csrf(response.text)
        if csrf:
            self.client.headers["X-CSRF-Token"] = csrf
        self.email = email

    def courses(self) -> list[dict[str, Any]]:
        response = self._get("/account")
        return parse_courses(response.text)

    def assignments(self, course_id: str) -> list[dict[str, Any]]:
        response = self._get(f"/courses/{course_id}/assignments")
        rows = parse_assignments(response.text)
        if rows:
            return rows
        fallback = self._get(f"/courses/{course_id}")
        return parse_assignments(fallback.text)

    def submissions(self, course_id: str, assignment_id: str) -> list[dict[str, Any]]:
        response = self._get(f"/courses/{course_id}/assignments/{assignment_id}/review_grades")
        return parse_review_grades(response.text)

    def submission_text(self, course_id: str, assignment_id: str, submission_id: str) -> str:
        response = self._get(
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}.json",
            params={"content": "react", "only_keys[]": ["text_files"]},
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise GradescopeError("Gradescope did not return submission files as JSON") from exc
        chunks = []
        for item in payload.get("text_files") or []:
            file_info = item.get("file") or {}
            url = file_info.get("url")
            name = file_info.get("filename") or item.get("name") or "file"
            if not url:
                continue
            downloaded = self.client.get(url)
            if downloaded.status_code >= 400:
                chunks.append(f"[{name}: download failed {downloaded.status_code}]")
                continue
            kind = (downloaded.headers.get("content-type") or "").lower()
            markdown = bytes_to_markdown(downloaded.content, str(name), kind)
            if markdown is not None:
                chunks.append(f"--- {name} ---\n" + markdown[:200_000])
                continue
            chunks.append(f"--- {name} ---\n" + downloaded.content.decode("utf-8", errors="replace")[:200_000])
        if not chunks:
            return "[no text files on this Gradescope submission]"
        return "\n\n".join(chunks)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = self.client.get(self.base_url + path, params=params)
        if response.status_code in {401, 403}:
            raise GradescopeError("Gradescope session is not authorized. Log in again.")
        if response.status_code == 404:
            raise GradescopeError(f"Gradescope page not found: {path}")
        if response.status_code >= 400:
            raise GradescopeError(f"Gradescope {response.status_code} for {path}")
        return response


def _auth_token(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one('form[action="/login"] input[name="authenticity_token"]')
    if node is None or not node.get("value"):
        raise GradescopeError("Could not find the Gradescope login token")
    return str(node["value"])


def _csrf(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one('meta[name="csrf-token"]')
    return str(node["content"]) if node and node.get("content") else ""


def parse_courses(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div#account-show")
    if root is None:
        return []
    role = "instructor"
    courses = []
    for node in root.find_all(["h2", "a"]):
        classes = node.get("class") or []
        if node.name == "h2" and "pageHeading" in classes and node.get_text(strip=True) == "Student Courses":
            role = "student"
            continue
        if node.name != "a":
            continue
        href = str(node.get("href") or "")
        if "/courses/" not in href:
            continue
        course_id = href.rstrip("/").split("/")[-1]
        short = node.find("h3", class_="courseBox--shortname")
        full = node.find("div", class_="courseBox--name")
        courses.append(
            {
                "id": course_id,
                "name": (short.get_text(strip=True) if short else course_id),
                "code": full.get_text(strip=True) if full else "",
                "role": role,
            }
        )
    return courses


def parse_assignments(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("div", {"data-react-class": "AssignmentsTable"})
    if node is None or not node.get("data-react-props"):
        return []
    payload = json.loads(str(node["data-react-props"]))
    rows = []
    for item in payload.get("table_data") or []:
        if item.get("type") != "assignment":
            continue
        url = str(item.get("url") or "")
        rows.append(
            {
                "id": url.rstrip("/").split("/")[-1],
                "name": item.get("title") or "Assignment",
                "points": item.get("total_points"),
                "description": "",
            }
        )
    return rows


def parse_review_grades(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for link in soup.select("td.table--primaryLink a"):
        href = str(link.get("href") or "")
        submission_id = href.rstrip("/").split("/")[-1]
        parent = link.find_parent("tr")
        cells = [cell.get_text(" ", strip=True) for cell in parent.find_all("td")] if parent else []
        email = next((cell for cell in cells if "@" in cell), "")
        score = next((cell for cell in cells if cell.replace(".", "", 1).isdigit()), "")
        rows.append(
            {
                "id": submission_id,
                "student_id": submission_id,
                "name": link.get_text(strip=True) or "Student",
                "email": email,
                "text": "",
                "attachments": [],
                "posted_grade": score or None,
            }
        )
    return rows


def grades_csv(rows: list[dict[str, Any]]) -> str:
    lines = ["Name,Email,Score,Comment"]
    for row in rows:
        comment = str(row.get("comment") or "").replace('"', "'")
        name = str(row.get("name") or "").replace('"', "'")
        email = str(row.get("email") or "")
        lines.append(f'"{name}","{email}",{row.get("score")},"{comment}"')
    return "\n".join(lines) + "\n"
