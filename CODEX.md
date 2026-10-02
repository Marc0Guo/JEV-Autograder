# Codex

Codex reads `AGENTS.md`. This file is the same install contract.

This app reads student work from Canvas or Gradescope and scores it on the local machine.

- Scoring is [SemIf](https://github.com/TheoLeeCJ/SemIf-OpenJev) (formerly OpenJev), Python package `semif-phase1`.
- PDF, Word, PowerPoint, and Excel uploads are converted to Markdown with [MarkItDown](https://github.com/microsoft/markitdown).

Set the project up. Do not post grades, create LMS tokens, or download the model unless the user asks you to grade.

## One command

From this repository:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

```bash
bash install.sh
```

From an empty machine:

```powershell
irm https://raw.githubusercontent.com/Marc0Guo/JEV-Autograder/main/install.ps1 | iex
```

```bash
curl -fsSL https://raw.githubusercontent.com/Marc0Guo/JEV-Autograder/main/install.sh | bash
```

The script installs [uv](https://docs.astral.sh/uv/) and Python 3.12 when they are missing, then runs `uv sync`.
That installs this package, MarkItDown extras `pdf`, `docx`, `pptx`, `xlsx`, and `xls`, and `semif-phase1` pinned to SemIf-OpenJev commit `23cf1f39fc9534fe81437200959b6dfc7106e45a`.

If uv and Python 3.12 are already available, `uv sync` alone is the dependency step.

## Run

```bash
uv run autograde
```

Open http://127.0.0.1:8010.

## Check

```bash
uv run python -c "import autograde, markitdown, semif_phase1; print('ready')"
uv run python -m unittest discover -s tests -v
```

The tests do not load the model.

## Runtime

- Entry point: `autograde` → `autograde.app:main`. FastAPI on `127.0.0.1:8010`.
- Scorer: `autograde.semif_backend.SemifGrader` calls `semif_phase1.shared.score_shared`, then `semif_phase1.direct.score` if the shared prefix cannot be reused.
- Model, loaded on the first grade: `Qwen/Qwen3.5-4B` revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`, dtype `bfloat16`. CUDA when a GPU is present, otherwise CPU. The download is several gigabytes and is not part of install.
- Canvas tokens are stored in `data/config.json`. `data/` is gitignored. Gradescope passwords are not written to disk.
- `compare_readonly.py` and `run_course_readonly.py` are local read-only Canvas comparisons. They are not part of setup.
