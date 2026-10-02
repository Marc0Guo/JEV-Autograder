import unittest

import httpx

from autograde.canvas_api import CanvasClient, html_to_text, next_link
from autograde.gradescope_api import grades_csv, parse_assignments, parse_courses, parse_review_grades
from autograde.rubric import ChoiceOption, Criterion, Level, Rubric, score_answers


class RubricTests(unittest.TestCase):
    def test_expected_points_and_review_flag(self):
        rubric = Rubric(
            assignment_points=10,
            min_confidence=0.4,
            criteria=[
                Criterion(
                    id="accuracy",
                    kind="score",
                    instructions="How complete?",
                    levels=[
                        Level(label="Missing", points=0, description="missing"),
                        Level(label="Complete", points=6, description="complete"),
                    ],
                ),
                Criterion(
                    id="tone",
                    kind="choice",
                    instructions="Tone?",
                    options=[
                        ChoiceOption(name="clear", points=4, description="clear"),
                        ChoiceOption(name="unclear", points=0, description="unclear"),
                    ],
                ),
            ],
        )
        result = score_answers(
            rubric,
            {
                "accuracy": {
                    "type": "score",
                    "score": 0.8,
                    "confidence": 0.7,
                    "probabilities": {"0": 0.2, "1": 0.8},
                },
                "tone": {
                    "type": "choice",
                    "choice": "clear",
                    "confidence": 0.2,
                    "probabilities": {"clear": 0.6, "unclear": 0.4},
                },
            },
        )
        self.assertEqual(result.points, 7.2)
        self.assertEqual(result.max_points, 10)
        self.assertEqual(result.scaled_points, 7.2)
        self.assertTrue(result.needs_review)
        self.assertIn("clear", result.comment)
        self.assertEqual(result.criteria[0].model_key, "1")
        self.assertEqual(result.criteria[1].model_key, "clear")
        from autograde.rubric import score_selection

        kept = score_selection(rubric, {"accuracy": "1", "tone": "clear"})
        self.assertEqual(kept.scaled_points, 10)
        self.assertNotIn("adjusted", kept.comment)
        changed = score_selection(rubric, {"accuracy": "0", "tone": "unclear"})
        self.assertEqual(changed.points, 0)
        self.assertIn("Missing", changed.comment)
        self.assertNotIn("confidence", changed.comment)

    def test_noul_expected_value(self):
        rubric = Rubric(
            min_confidence=0,
            criteria=[
                Criterion(
                    id="cited",
                    kind="noul",
                    instructions="Are sources cited?",
                    points_if_yes=5,
                    points_if_no=0,
                )
            ],
        )
        result = score_answers(rubric, {"cited": {"type": "noul", "noul": 0.25}})
        self.assertEqual(result.points, 0)
        self.assertEqual(result.criteria[0].label, "no")
        self.assertEqual(result.criteria[0].model_key, "no")
        self.assertEqual(result.criteria[0].ratings[0].key, "yes")
        awarded = score_answers(rubric, {"cited": {"type": "noul", "noul": 0.91}})
        self.assertEqual(awarded.points, 5)
        self.assertEqual(awarded.criteria[0].label, "yes")


class CanvasTests(unittest.TestCase):
    def test_html_and_link(self):
        self.assertEqual(html_to_text("<p>Hello<br>there</p>"), "Hello\nthere")
        self.assertEqual(
            next_link('<https://canvas/api?page=2>; rel="next", <https://canvas/api?page=1>; rel="prev"'),
            "https://canvas/api?page=2",
        )

    def test_lists_submissions_and_posts_grade(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/courses/9/assignments/3/submissions" and request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 1,
                            "workflow_state": "submitted",
                            "body": "<p>My answer</p>",
                            "user": {"id": 42, "name": "Ada", "email": "ada@school.edu"},
                            "attachments": [],
                        },
                        {"id": 2, "workflow_state": "unsubmitted", "user": {"id": 7, "name": "Skip"}},
                    ],
                )
            if request.method == "PUT":
                body = request.read()
                self.assertIn(b"posted_grade", body)
                return httpx.Response(200, json={"grade": "8"})
            return httpx.Response(404)

        client = CanvasClient("https://canvas.example", "token", httpx.Client(transport=httpx.MockTransport(handler), base_url="https://canvas.example"))
        rows = client.submissions("9", "3")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "My answer")
        posted = client.post_grade("9", "3", "42", 8, "nice")
        self.assertEqual(posted["posted_grade"], "8")
        client.close()

    def test_opens_the_speedgrader_student(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/courses/1862977/assignments/11029642/submissions/4479160")
            return httpx.Response(
                200,
                json={
                    "id": 88,
                    "user_id": 4479160,
                    "anonymous_id": "ixn77",
                    "workflow_state": "submitted",
                    "body": "<p>Heights</p>",
                    "user": {"id": 4479160, "name": "Student"},
                    "attachments": [],
                },
            )

        client = CanvasClient(
            "https://canvas.example",
            "token",
            httpx.Client(transport=httpx.MockTransport(handler), base_url="https://canvas.example"),
        )
        row = client.open_submission("1862977", "11029642", "4479160", "ixn77")
        self.assertEqual(row["student_id"], "4479160")
        self.assertEqual(row["anonymous_id"], "ixn77")
        self.assertEqual(row["text"], "Heights")
        client.close()


class GradescopeParseTests(unittest.TestCase):
    def test_courses_assignments_and_grades(self):
        courses = parse_courses(
            """
            <div id="account-show">
              <h2 class="pageHeading">Instructor Courses</h2>
              <a href="/courses/55"><h3 class="courseBox--shortname">CS 1</h3><div class="courseBox--name">Intro</div></a>
              <h2 class="pageHeading">Student Courses</h2>
              <a href="/courses/77"><h3 class="courseBox--shortname">ART</h3></a>
            </div>
            """
        )
        self.assertEqual(courses[0]["role"], "instructor")
        self.assertEqual(courses[1]["id"], "77")
        assignments = parse_assignments(
            '<div data-react-class="AssignmentsTable" data-react-props='
            '\'{"table_data":[{"type":"assignment","title":"HW1","url":"/courses/55/assignments/99","total_points":"10.0","submission_window":{}}]}\'>'
            "</div>"
        )
        self.assertEqual(assignments[0]["id"], "99")
        rows = parse_review_grades(
            '<tr><td class="table--primaryLink"><a href="/courses/55/assignments/99/submissions/5">Ada</a></td>'
            "<td>ada@school.edu</td><td>8.5</td></tr>"
        )
        self.assertEqual(rows[0]["email"], "ada@school.edu")
        csv = grades_csv([{"name": "Ada", "email": "ada@school.edu", "score": 8.5, "comment": "good"}])
        self.assertIn("ada@school.edu", csv)


class CanvasRubricTests(unittest.TestCase):
    def test_maps_full_and_no_marks(self):
        from autograde.canvas_api import _canvas_criterion

        item = _canvas_criterion(
            {
                "description": "Story Pitch",
                "points": 7,
                "long_description": "Describes a clear data-driven story idea",
                "ratings": [
                    {"description": "Full Marks", "points": 7},
                    {"description": "No Marks", "points": 0},
                ],
            },
            0,
        )
        self.assertEqual(item["id"], "story_pitch")
        self.assertEqual(item["kind"], "score")
        self.assertEqual([level["points"] for level in item["levels"]], [0, 7])
        self.assertIn("data-driven", item["levels"][1]["description"])


class ModeMatchTests(unittest.TestCase):
    def test_full_marks_becomes_yes_no(self):
        from autograde.rubric import apply_mode

        shaped = apply_mode(
            {
                "id": "story_pitch",
                "instructions": "Describes a clear story",
                "levels": [
                    {"label": "No Marks", "points": 0, "description": "Does not meet Story Pitch"},
                    {"label": "Full Marks", "points": 7, "description": "Describes a clear story"},
                ],
            },
            "noul",
        )
        self.assertEqual(shaped["kind"], "noul")
        self.assertEqual(shaped["points_if_yes"], 7)
        self.assertEqual(shaped["points_if_no"], 0)
        self.assertIn("clear story", shaped["yes_description"])


class ChecklistTests(unittest.TestCase):
    def test_point_lines_become_separate_checks(self):
        from autograde.canvas_api import _canvas_criterion
        from autograde.rubric import expand_canvas_criterion

        item = _canvas_criterion(
            {
                "description": "Project Setup",
                "points": 2,
                "long_description": (
                    "Clear and well-formatted writing (grammar, appropriate lists, hyperlinks) (1 point)<br/><br/>"
                    "Quarto / Jupyter Notebook Structure (1 point)"
                ),
                "ratings": [
                    {"description": "Full Marks", "points": 2},
                    {"description": "No Marks", "points": 0},
                ],
            },
            0,
        )
        parts = expand_canvas_criterion(item, set())
        self.assertEqual([part["points_if_yes"] for part in parts], [1, 1])
        self.assertTrue(all(part["kind"] == "noul" for part in parts))
        self.assertIn("Quarto", parts[1]["instructions"])
        self.assertNotIn("Quarto", parts[0]["instructions"])

    def test_subtotal_heading_is_not_scored_twice(self):
        from autograde.canvas_api import _canvas_criterion
        from autograde.rubric import expand_canvas_criterion

        item = _canvas_criterion(
            {
                "description": "Action on joined datasets",
                "points": 9,
                "long_description": (
                    "Action #1 (3 points)<br/>- Performing the action (2 points)<br/>- Reasoning (1 point)<br/>"
                    "Action #2 (3 points)<br/>- Performing the action (2 points)<br/>- Reasoning (1 point)<br/>"
                    "Action #3 (3 points)<br/>- Performing the action (2 points)<br/>- Reasoning (1 point)"
                ),
                "ratings": [
                    {"description": "Full Marks", "points": 9},
                    {"description": "No Marks", "points": 0},
                ],
            },
            0,
        )
        parts = expand_canvas_criterion(item, set())
        self.assertEqual(len(parts), 6)
        self.assertEqual(sum(part["points_if_yes"] for part in parts), 9)
        self.assertIn("Action #1", parts[0]["instructions"])
        self.assertNotIn("(3 points)", parts[0]["instructions"])


def _tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 72 Td ({text}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


class DocumentTests(unittest.TestCase):
    def test_pdf_becomes_markdown(self):
        from autograde.documents import bytes_to_markdown

        text = bytes_to_markdown(_tiny_pdf("Grade distribution notes"), "a1.pdf", "application/pdf")
        self.assertIsNotNone(text)
        self.assertIn("Grade distribution notes", text)
        self.assertIsNone(bytes_to_markdown(b"print(1)\n", "work.r", "text/plain"))


class SemifRequestTests(unittest.TestCase):
    def test_noul_probabilities_round_trip(self):
        from autograde.semif_backend import answers_from_results, systemone_rows
        from autograde.rubric import to_systemone

        rubric = Rubric(
            criteria=[
                Criterion(
                    id="cited",
                    kind="noul",
                    instructions="Sources are cited.",
                    points_if_yes=5,
                    points_if_no=0,
                )
            ]
        )
        request = to_systemone(rubric, "The essay cites two papers.")
        rows = systemone_rows(request)
        self.assertEqual([option["id"] for option in rows[0]["options"]], ["true", "false"])
        answers = answers_from_results(
            request["questions"],
            [{"id": "cited", "option_ids": ["true", "false"], "probabilities": [0.91, 0.09]}],
        )
        result = score_answers(rubric, answers)
        self.assertEqual(result.points, 5)
        self.assertEqual(result.criteria[0].model_key, "yes")


class SpeedGraderRubricTests(unittest.TestCase):
    def test_open_assignment_replaces_a_different_course(self):
        from unittest.mock import patch

        from autograde.app import _rubric_for, app
        from autograde.canvas_api import CanvasClient, _canvas_criterion

        previous = app.state.session
        app.state.session = {
            "course_id": "1",
            "assignment_id": "9",
            "assignment_name": "Test 1",
            "rubric": {
                "name": "Old",
                "criteria": [
                    {
                        "id": "old",
                        "instructions": "Old check.",
                        "kind": "noul",
                        "points_if_yes": 1,
                        "points_if_no": 0,
                    }
                ],
            },
        }
        app.state.canvas = CanvasClient("https://canvas.example", "token")
        payload = {
            "found": True,
            "name": "A1 OkCupid Height",
            "points": 6,
            "description": "Read the file.",
            "criteria": [
                _canvas_criterion(
                    {
                        "id": "_7709",
                        "description": "Glimpse at the data",
                        "points": 6,
                        "ratings": [
                            {"description": "Full Marks", "points": 6},
                            {"description": "No Marks", "points": 0},
                        ],
                    },
                    0,
                )
            ],
        }
        try:
            with patch.object(app.state.canvas, "assignment_rubric", return_value=payload) as fetch:
                with patch("autograde.app._save_session") as save:
                    rubric, name = _rubric_for(None, "1862977", "11168610", "")
            fetch.assert_called_once_with("1862977", "11168610")
            saved = save.call_args.args[0]
        finally:
            app.state.session = previous
        self.assertEqual(name, "A1 OkCupid Height")
        self.assertEqual(rubric.criteria[0].id, "glimpse_at_the_data")
        self.assertEqual(rubric.criteria[0].kind, "noul")
        self.assertEqual(saved["course_id"], "1862977")
        self.assertEqual(saved["assignment_id"], "11168610")


if __name__ == "__main__":
    unittest.main()
