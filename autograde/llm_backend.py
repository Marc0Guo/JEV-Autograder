"""Score a System One request with a hosted or local chat model.

Anthropic uses the Messages API. OpenAI, Ollama, and LM Studio use Chat
Completions. The model must return the same answer object score_answers reads.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


class LlmError(RuntimeError):
    pass


PRESETS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-5",
        "style": "anthropic",
        "key_required": True,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "style": "openai",
        "key_required": True,
    },
    "ollama": {
        "label": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "llama3.2",
        "style": "openai",
        "key_required": False,
    },
    "lmstudio": {
        "label": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "style": "openai",
        "key_required": False,
    },
}

_SYSTEM = (
    "You grade one student submission against a rubric. "
    "Reply with one JSON object and no markdown. "
    "The object has an answers field keyed by question id. "
    "For type score, probabilities uses string indexes \"0\", \"1\", ... in criteria order, "
    "plus confidence from 0 to 1. "
    "For type choice, include choice (one criteria key) and probabilities for every criteria key, "
    "plus confidence from 0 to 1. "
    "For type noul, noul is the probability from 0 to 1 that the statement is true."
)


def env_api_key(provider: str) -> str:
    """Key from the environment when data/config.json has none for this provider."""
    specific = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "")
    if specific:
        value = os.environ.get(specific, "").strip()
        if value:
            return value
    return os.environ.get("AUTOGRADE_LLM_API_KEY", "").strip()


def grading_prompt(request: dict[str, Any]) -> str:
    questions = request.get("questions") or {}
    state = request.get("state") or ""
    return (
        "State:\n"
        f"{state}\n\n"
        "Questions:\n"
        f"{json.dumps(questions, ensure_ascii=False, indent=2)}"
    )


def build_http_request(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Return the URL, headers, and JSON body for one grading call."""
    preset = PRESETS.get(provider)
    if preset is None:
        known = ", ".join(PRESETS)
        raise LlmError(f"Unknown LLM provider {provider!r}. Use one of: {known}")
    root = (base_url or preset["base_url"]).strip().rstrip("/")
    if not root:
        raise LlmError(f"Set a base URL for {preset['label']}")
    chosen = (model or preset["model"]).strip()
    if not chosen:
        raise LlmError(f"Set a model name for {preset['label']}")
    key = api_key.strip()
    if preset["key_required"] and not key:
        raise LlmError(
            f"{preset['label']} needs an API key. Save one in the app, or set "
            f"{'ANTHROPIC_API_KEY' if provider == 'anthropic' else 'OPENAI_API_KEY'} "
            "or AUTOGRADE_LLM_API_KEY."
        )
    if preset["style"] == "anthropic":
        url = root + "/v1/messages"
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if key:
            headers["x-api-key"] = key
        body: dict[str, Any] = {
            "model": chosen,
            "max_tokens": 2048,
            "temperature": 0,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }
        return url, headers, body
    url = root + "/chat/completions"
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    body = {
        "model": chosen,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    return url, headers, body


def _clip(text: str, secret: str = "", limit: int = 800) -> str:
    cleaned = " ".join(text.split())
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    raise LlmError("The model response did not include text")


def response_text(provider: str, payload: Any) -> str:
    preset = PRESETS.get(provider)
    label = preset["label"] if preset else provider
    if not isinstance(payload, dict):
        raise LlmError(f"{label} returned a non-object response")
    if provider == "anthropic":
        return _message_text(payload.get("content"))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LlmError(f"{label} returned no choices")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise LlmError(f"{label} returned no message")
    return _message_text(message.get("content"))


def parse_answers(text: str, questions: dict[str, Any]) -> dict[str, Any]:
    raw = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise LlmError("The model did not return JSON. " + _clip(text))
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError("The model returned invalid JSON. " + _clip(text)) from exc
    if not isinstance(payload, dict):
        raise LlmError("The model returned JSON that is not an object")
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else payload
    if not isinstance(answers, dict):
        raise LlmError("The model JSON has no answers object")
    normalized: dict[str, Any] = {}
    for qid, question in questions.items():
        answer = answers.get(qid)
        if not isinstance(answer, dict):
            raise LlmError(f"The model omitted criterion {qid}")
        kind = question.get("type")
        try:
            if kind == "noul":
                if "noul" not in answer:
                    raise LlmError(f"Criterion {qid} is missing noul")
                probability = float(answer["noul"])
                if probability < 0 or probability > 1:
                    raise LlmError(f"Criterion {qid} noul must be between 0 and 1")
                normalized[qid] = {"type": "noul", "noul": probability}
            elif kind == "choice":
                probabilities = answer.get("probabilities")
                if not isinstance(probabilities, dict) or not probabilities:
                    raise LlmError(f"Criterion {qid} is missing choice probabilities")
                normalized[qid] = {
                    "type": "choice",
                    "choice": str(answer.get("choice") or ""),
                    "probabilities": {str(key): float(value) for key, value in probabilities.items()},
                    "confidence": float(answer.get("confidence") or 0.0),
                }
            elif kind == "score":
                probabilities = answer.get("probabilities")
                if not isinstance(probabilities, dict) or not probabilities:
                    raise LlmError(f"Criterion {qid} is missing score probabilities")
                normalized[qid] = {
                    "probabilities": {str(key): float(value) for key, value in probabilities.items()},
                    "confidence": float(answer.get("confidence") or 0.0),
                }
            else:
                raise LlmError(f"Criterion {qid} has unsupported type {kind!r}")
        except LlmError:
            raise
        except (TypeError, ValueError) as exc:
            raise LlmError(f"Criterion {qid} has a non-numeric score") from exc
    return normalized


class LlmGrader:
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60,
        client: httpx.Client | None = None,
    ):
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.client = client

    def grade(self, request: dict[str, Any]) -> dict[str, Any]:
        questions = request.get("questions") or {}
        if not isinstance(questions, dict) or not questions:
            raise LlmError("System One request has no questions")
        if self.timeout <= 0:
            raise LlmError("Timeout must be greater than 0")
        prompt = grading_prompt(request)
        url, headers, body = build_http_request(
            self.provider, self.base_url, self.api_key, self.model, prompt
        )
        owns = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout)
        label = PRESETS.get(self.provider, {}).get("label", self.provider)
        try:
            try:
                response = client.post(url, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                raise LlmError(f"{label} timed out after {self.timeout:g}s at {url}") from exc
            except httpx.HTTPError as exc:
                raise LlmError(f"{label} request failed at {url}: {exc}") from exc
            if response.status_code >= 400:
                raise LlmError(
                    f"{label} {response.status_code} at {url}: {_clip(response.text, self.api_key)}"
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise LlmError(
                    f"{label} returned non-JSON. {_clip(response.text, self.api_key)}"
                ) from exc
            text = response_text(self.provider, payload)
            answers = parse_answers(text, questions)
        finally:
            if owns:
                client.close()
        return {
            "answers": answers,
            "model": {"backend": "llm", "provider": self.provider, "model": body["model"]},
            "timing": {},
        }
