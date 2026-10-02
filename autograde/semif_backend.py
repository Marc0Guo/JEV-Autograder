"""Score a System One request with SemIf's shared Qwen3.5 readout.

This is the local grader. It never talks to Canvas.
"""

from __future__ import annotations

from typing import Any

MODEL = "Qwen/Qwen3.5-4B"
REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"


class SemifError(RuntimeError):
    pass


def systemone_rows(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one System One payload into SemIf shared-scoring rows."""
    state = request.get("state")
    questions = request.get("questions") or {}
    if not isinstance(questions, dict) or not questions:
        raise SemifError("System One request has no questions")
    rows = []
    for qid, question in questions.items():
        if not isinstance(question, dict):
            raise SemifError(f"Question {qid} is not an object")
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "score":
            if not isinstance(criteria, list):
                raise SemifError(f"Question {qid} score criteria must be a list")
            options = [{"id": str(index), "description": str(text)} for index, text in enumerate(criteria)]
        elif kind in {"choice", "noul"}:
            if not isinstance(criteria, dict):
                raise SemifError(f"Question {qid} {kind} criteria must be an object")
            options = [{"id": str(name), "description": str(text)} for name, text in criteria.items()]
        else:
            raise SemifError(f"Question {qid} has unsupported type {kind!r}")
        rows.append(
            {
                "id": str(qid),
                "state": state,
                "question": str(question.get("instructions") or ""),
                "options": options,
            }
        )
    return rows


def answers_from_results(questions: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Map SemIf option probabilities back to the fields score_answers reads."""
    answers: dict[str, Any] = {}
    for result in results:
        probabilities = {
            str(option_id): float(probability)
            for option_id, probability in zip(result["option_ids"], result["probabilities"])
        }
        question = questions.get(result["id"]) or {}
        top = max(probabilities.values()) if probabilities else 0.0
        if question.get("type") == "noul":
            yes = float(probabilities.get("true", 0.0))
            answers[result["id"]] = {
                "noul": yes,
                "probabilities": probabilities,
                "confidence": abs(2 * yes - 1),
            }
        elif question.get("type") == "choice":
            choice = max(probabilities, key=probabilities.get) if probabilities else ""
            answers[result["id"]] = {
                "choice": choice,
                "probabilities": probabilities,
                "confidence": top,
            }
        else:
            answers[result["id"]] = {"probabilities": probabilities, "confidence": top}
    return answers


def _score_direct(model, tokenizer, rows, metadata, max_tokens, exc: Exception):
    """Score each decision from a full forward when the shared prefix cannot be reused."""
    from semif_phase1.direct import score as score_direct

    results = []
    seconds = 0.0
    try:
        for row in rows:
            result = score_direct(model, tokenizer, row, metadata, max_tokens)
            seconds += float(result.get("total_seconds") or 0.0)
            results.append(result)
    except Exception as single:
        raise SemifError(str(single)) from single
    return results, {
        "fallback": "direct",
        "batch_error": str(exc),
        "total_seconds": round(seconds, 3),
    }


class SemifGrader:
    """Lazy loader for the pinned direct-option model."""

    def __init__(
        self,
        source: str = MODEL,
        revision: str = REVISION,
        device: str | None = None,
        dtype: str | None = None,
        max_tokens: int = 8192,
    ):
        self.source = source
        self.revision = revision
        self.device = device
        self.dtype = dtype
        self.max_tokens = max_tokens
        self.metadata: dict[str, Any] | None = None
        self._model = None
        self._tokenizer = None

    def grade(self, request: dict[str, Any]) -> dict[str, Any]:
        questions = request.get("questions") or {}
        rows = systemone_rows(request)
        model, tokenizer, metadata = self._load()
        try:
            from semif_phase1.shared import score_shared

            results, timing = score_shared(model, tokenizer, rows, metadata, self.max_tokens)
        except Exception as exc:
            results, timing = _score_direct(model, tokenizer, rows, metadata, self.max_tokens, exc)
        return {
            "answers": answers_from_results(questions, results),
            "model": metadata,
            "timing": timing,
        }

    def _load(self):
        if self._model is not None:
            return self._model, self._tokenizer, self.metadata
        try:
            import torch
            from semif_phase1.core import load_causal_model
        except ImportError as exc:
            raise SemifError(
                "SemIf scoring needs torch and semif_phase1 on PYTHONPATH. "
                f"Import failed: {exc}"
            ) from exc
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        # The checkpoint is bfloat16. float32 does not fit beside the OS on a 32GB machine.
        dtype = self.dtype or "bfloat16"
        try:
            model, tokenizer, metadata = load_causal_model(self.source, self.revision, device, dtype)
        except Exception as exc:
            raise SemifError(f"Could not load {self.source}@{self.revision}: {exc}") from exc
        self._model = model
        self._tokenizer = tokenizer
        self.metadata = metadata
        return model, tokenizer, metadata
