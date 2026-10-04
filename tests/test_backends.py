import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from autograde.laya_backend import LayaError, answers_from_laya
from autograde.llm_backend import LlmError, LlmGrader, build_http_request, parse_answers
from autograde.rubric import ChoiceOption, Criterion, Level, Rubric, score_answers, to_systemone


def _rubric() -> Rubric:
    return Rubric(
        assignment_points=10,
        min_confidence=0.2,
        noul_threshold=0.8,
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
                    ChoiceOption(name="unclear", points=1, description="unclear"),
                ],
            ),
            Criterion(
                id="cited",
                kind="noul",
                instructions="Sources are cited.",
                points_if_yes=5,
                points_if_no=0,
            ),
        ],
    )


def _answers_json() -> str:
    return json.dumps(
        {
            "answers": {
                "accuracy": {"probabilities": {"0": 0.25, "1": 0.75}, "confidence": 0.5},
                "tone": {
                    "choice": "clear",
                    "probabilities": {"clear": 0.7, "unclear": 0.3},
                    "confidence": 0.4,
                },
                "cited": {"noul": 0.91},
            }
        }
    )


class LayaAnswerTests(unittest.TestCase):
    def test_laya_answers_score_like_semif(self):
        rubric = _rubric()
        request = to_systemone(rubric, "The essay cites two papers and is clear.")
        payload = {
            "model": "laya-rl-agent",
            "answers": {
                "accuracy": {
                    "type": "score",
                    "score": 0.75,
                    "probabilities": {"0": 0.25, "1": 0.75},
                    "confidence": 0.5,
                },
                "tone": {
                    "type": "choice",
                    "choice": "clear",
                    "probabilities": {"clear": 0.7, "unclear": 0.3},
                    "confidence": 0.4,
                },
                "cited": {"type": "noul", "noul": 0.91, "confidence": 0.82},
            },
            "routing": {"model": "english"},
        }
        result = score_answers(rubric, answers_from_laya(request["questions"], payload))
        self.assertEqual(result.criteria[0].model_key, "1")
        self.assertEqual(result.criteria[1].model_key, "clear")
        self.assertEqual(result.criteria[2].model_key, "yes")
        self.assertEqual(result.criteria[2].points, 5)

    def test_missing_laya_answer_is_an_error(self):
        with self.assertRaises(LayaError):
            answers_from_laya({"cited": {"type": "noul"}}, {"answers": {}})


class LlmRequestTests(unittest.TestCase):
    def test_anthropic_and_openai_shapes_differ(self):
        prompt = "State:\nhello"
        url, headers, body = build_http_request(
            "anthropic", "https://api.anthropic.com", "sk-anthropic", "claude-test", prompt
        )
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-anthropic")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], "claude-test")
        self.assertEqual(body["messages"], [{"role": "user", "content": prompt}])
        self.assertIn("max_tokens", body)
        self.assertNotIn("response_format", body)
        self.assertNotIn("authorization", headers)

        shapes = []
        for provider, base, key in (
            ("openai", "https://api.openai.com/v1", "sk-openai"),
            ("ollama", "http://127.0.0.1:11434/v1", ""),
            ("lmstudio", "http://127.0.0.1:1234/v1", ""),
        ):
            url, headers, body = build_http_request(provider, base, key, "local-model", prompt)
            self.assertTrue(url.endswith("/chat/completions"), url)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertEqual(body["messages"][1], {"role": "user", "content": prompt})
            self.assertNotIn("x-api-key", headers)
            shapes.append({k: body[k] for k in ("temperature", "response_format")})
            shapes[-1]["roles"] = [item["role"] for item in body["messages"]]
        self.assertEqual(shapes[0], shapes[1])
        self.assertEqual(shapes[1], shapes[2])
        self.assertEqual(headers.get("authorization"), None)
        openai_url, openai_headers, _openai_body = build_http_request(
            "openai", "https://api.openai.com/v1/", "sk-openai", "gpt-test", prompt
        )
        self.assertEqual(openai_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(openai_headers["authorization"], "Bearer sk-openai")

    def test_paid_providers_require_a_key(self):
        with self.assertRaises(LlmError) as raised:
            build_http_request("anthropic", "https://api.anthropic.com", "", "claude-test", "x")
        self.assertIn("API key", str(raised.exception))
        build_http_request("ollama", "http://127.0.0.1:11434/v1", "", "llama3.2", "x")

    def test_mocked_calls_parse_into_scores(self):
        rubric = _rubric()
        request = to_systemone(rubric, "Cited and clear.")
        seen = []

        def handler(call: httpx.Request) -> httpx.Response:
            seen.append(call)
            if call.url.path == "/v1/messages":
                return httpx.Response(200, json={"content": [{"type": "text", "text": _answers_json()}]})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "```json\n" + _answers_json() + "\n```"}}]},
            )

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            anthropic = LlmGrader(
                "anthropic",
                "https://api.anthropic.com",
                "sk-anthropic",
                "claude-test",
                timeout=5,
                client=client,
            ).grade(request)
            ollama = LlmGrader(
                "ollama",
                "http://127.0.0.1:11434/v1",
                "",
                "llama3.2",
                timeout=5,
                client=client,
            ).grade(request)
        self.assertEqual(seen[0].url.path, "/v1/messages")
        self.assertEqual(seen[0].headers["x-api-key"], "sk-anthropic")
        anthropic_body = json.loads(seen[0].content)
        self.assertEqual(anthropic_body["messages"][0]["role"], "user")
        self.assertNotIn("response_format", anthropic_body)
        self.assertEqual(seen[1].url.host, "127.0.0.1")
        self.assertEqual(seen[1].url.port, 11434)
        self.assertTrue(seen[1].url.path.endswith("/chat/completions"))
        ollama_body = json.loads(seen[1].content)
        self.assertEqual(ollama_body["response_format"], {"type": "json_object"})
        self.assertEqual([item["role"] for item in ollama_body["messages"]], ["system", "user"])
        self.assertNotIn("x-api-key", seen[1].headers)
        for payload in (anthropic, ollama):
            result = score_answers(rubric, payload["answers"])
            self.assertEqual(result.criteria[0].model_key, "1")
            self.assertEqual(result.criteria[1].model_key, "clear")
            self.assertEqual(result.criteria[2].model_key, "yes")
            self.assertAlmostEqual(result.criteria[0].points, 4.5)

    def test_timeout_and_http_errors_are_visible(self):
        request = to_systemone(_rubric(), "text")

        def timeout(_call: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            grader = LlmGrader("lmstudio", "http://127.0.0.1:1234/v1", "", "local-model", timeout=5, client=client)
            with self.assertRaises(LlmError) as raised:
                grader.grade(request)
        self.assertIn("timed out", str(raised.exception).lower())
        self.assertIn("1234", str(raised.exception))

        def denied(call: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad key sk-openai")

        with httpx.Client(transport=httpx.MockTransport(denied)) as client:
            grader = LlmGrader("openai", "https://api.openai.com/v1", "sk-openai", "gpt-test", client=client)
            with self.assertRaises(LlmError) as raised:
                grader.grade(request)
        message = str(raised.exception)
        self.assertIn("401", message)
        self.assertNotIn("sk-openai", message)

    def test_parse_answers_rejects_a_missing_criterion(self):
        questions = to_systemone(_rubric(), "text")["questions"]
        with self.assertRaises(LlmError):
            parse_answers('{"answers": {"accuracy": {"probabilities": {"0": 1}}}}', questions)


class ScoringApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from autograde.app import app

        self.app = app
        self.tmp = tempfile.TemporaryDirectory()
        self.data_patch = patch("autograde.app.DATA", Path(self.tmp.name))
        self.data_patch.start()
        self.env_patch = patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "AUTOGRADE_LLM_API_KEY": ""},
        )
        self.env_patch.start()
        self.saved = {
            "config": json.loads(json.dumps(app.state.config)),
            "session": app.state.session,
            "canvas": app.state.canvas,
            "gradescope": app.state.gradescope,
            "laya": getattr(app.state, "laya", None),
            "semif": getattr(app.state, "semif", None),
        }
        app.state.canvas = None
        app.state.gradescope = None
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.app.state.config.clear()
        self.app.state.config.update(self.saved["config"])
        self.app.state.session = self.saved["session"]
        self.app.state.canvas = self.saved["canvas"]
        self.app.state.gradescope = self.saved["gradescope"]
        self.app.state.laya = self.saved["laya"]
        self.app.state.semif = self.saved["semif"]
        self.env_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def _rubric_body(self) -> dict:
        return {
            "text": "The essay cites two papers.",
            "assignment_name": "Sample",
            "rubric": {
                "criteria": [
                    {
                        "id": "cited",
                        "kind": "noul",
                        "instructions": "Sources are cited.",
                        "points_if_yes": 5,
                        "points_if_no": 0,
                    }
                ]
            },
        }

    def test_missing_key_and_closed_port_surface_in_the_api(self):
        saved = self.client.post(
            "/api/connect",
            json={
                "backend": "llm",
                "llm_provider": "anthropic",
                "llm_base_url": "https://api.anthropic.com",
                "llm_model": "claude-test",
                "llm_api_key": "sk-test-not-a-real-key",
                "llm_timeout": 5,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn("sk-test-not-a-real-key", saved.text)
        self.assertEqual(saved.json()["scoring"]["llm_key_source"], "saved")
        on_disk = json.loads((Path(self.tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["llm_keys"]["anthropic"], "sk-test-not-a-real-key")
        self.assertNotIn("sk-test-not-a-real-key", self.client.get("/api/status").text)

        empty = self.client.post(
            "/api/connect",
            json={
                "backend": "llm",
                "llm_provider": "openai",
                "llm_base_url": "https://api.openai.com/v1",
                "llm_model": "gpt-test",
                "llm_timeout": 5,
            },
        )
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["scoring"]["llm_key_source"], "missing")
        graded = self.client.post("/api/grade-text", json=self._rubric_body())
        self.assertEqual(graded.status_code, 502)
        self.assertIn("API key", graded.json()["detail"])

        refused = self.client.post(
            "/api/connect",
            json={
                "backend": "llm",
                "llm_provider": "ollama",
                "llm_base_url": "http://127.0.0.1:9/v1",
                "llm_model": "llama3.2",
                "llm_timeout": 2,
            },
        )
        self.assertEqual(refused.status_code, 200, refused.text)
        failed = self.client.post("/api/grade-text", json=self._rubric_body())
        self.assertEqual(failed.status_code, 502)
        self.assertIn("127.0.0.1:9", failed.json()["detail"])

    def test_unknown_backend_is_rejected(self):
        response = self.client.post("/api/connect", json={"backend": "mystery"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("semif", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
