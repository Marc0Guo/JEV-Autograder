"""Score a System One request with Laya's local decision engine.

Laya (https://github.com/NandhaKishorM/laya) is the open System One model
analogous to JEV: one forward pass returns typed choice, score, and noul
answers. This module never talks to Canvas.
"""

from __future__ import annotations

from typing import Any


class LayaError(RuntimeError):
    pass


def answers_from_laya(questions: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Keep Laya's answer objects. score_answers already reads this shape.

    Laya's noul value is P(true). Score probabilities use string indexes
    "0", "1", ... and choice probabilities use the criteria keys.
    """
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise LayaError("Laya returned no answers object")
    missing = [str(qid) for qid in questions if qid not in answers or not isinstance(answers.get(qid), dict)]
    if missing:
        raise LayaError("Laya omitted criteria: " + ", ".join(missing))
    return {str(qid): answers[qid] for qid in questions}


class LayaGrader:
    """Lazy loader for Laya's router. The checkpoint downloads on the first grade."""

    def __init__(self, model: str = "", device: str | None = None):
        self.model = model
        self.device = device
        self.signature = (model, device or "")
        self.metadata: dict[str, Any] | None = None
        self._router = None

    @property
    def loaded(self) -> bool:
        return self._router is not None

    def grade(self, request: dict[str, Any]) -> dict[str, Any]:
        questions = request.get("questions") or {}
        if not isinstance(questions, dict) or not questions:
            raise LayaError("System One request has no questions")
        state = request.get("state")
        if not isinstance(state, (str, dict, list)) or state == "":
            raise LayaError("System One request has no state")
        router = self._load()
        kwargs: dict[str, Any] = {}
        if self.model.strip():
            kwargs["model"] = self.model.strip()
        try:
            result = router.predict(state, questions, **kwargs)
        except LayaError:
            raise
        except Exception as exc:
            raise LayaError(str(exc)) from exc
        if not isinstance(result, dict):
            raise LayaError("Laya returned a non-object result")
        return {
            "answers": answers_from_laya(questions, result),
            "model": {
                "backend": "laya",
                "model": result.get("model"),
                "routing": result.get("routing"),
                "device": (self.metadata or {}).get("device"),
            },
            "timing": result.get("usage") or {},
        }

    def _load(self):
        if self._router is not None:
            return self._router
        try:
            import torch
            from laya import Router
        except ImportError as exc:
            raise LayaError(
                "Laya scoring needs the laya package. Run uv sync. "
                f"Import failed: {exc}"
            ) from exc
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        try:
            router = Router(device=device)
        except Exception as exc:
            raise LayaError(f"Could not start Laya on {device}: {exc}") from exc
        self._router = router
        self.metadata = {"backend": "laya", "device": device, "model": self.model.strip()}
        return router
