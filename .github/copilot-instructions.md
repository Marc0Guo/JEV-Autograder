# Copilot

Install before changing grading code. From the repository root:

- Windows: `powershell -ExecutionPolicy Bypass -File .\install.ps1`
- macOS and Linux: `bash install.sh`

That runs `uv sync`, which installs this app, [MarkItDown](https://github.com/microsoft/markitdown) (`pdf,docx,pptx,xlsx,xls`), and [SemIf-OpenJev](https://github.com/TheoLeeCJ/SemIf-OpenJev) (`semif-phase1` at `23cf1f39fc9534fe81437200959b6dfc7106e45a`).

Run with `uv run autograde` at http://127.0.0.1:8010. The first real grade downloads `Qwen/Qwen3.5-4B` revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`. Do not post grades or create LMS tokens unless the user asks. The same steps are in `AGENTS.md`.
