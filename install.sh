#!/usr/bin/env bash
# One-command setup for JEV Autograder.
# Installs uv and Python 3.12 when missing, then syncs MarkItDown and SemIf.
# Does not download the model, ask for a token, or post a grade.

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH. Open a new terminal and run this script again." >&2
  exit 1
fi

ROOT=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "${CANDIDATE}/pyproject.toml" ]] && grep -q 'name = "autograde"' "${CANDIDATE}/pyproject.toml"; then
    ROOT="${CANDIDATE}"
  fi
fi
if [[ -z "${ROOT}" && -f pyproject.toml ]] && grep -q 'name = "autograde"' pyproject.toml; then
  ROOT="$(pwd)"
fi
if [[ -z "${ROOT}" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "git is required to clone https://github.com/Marc0Guo/JEV-Autograder" >&2
    exit 1
  fi
  ROOT="$(pwd)/JEV-Autograder"
  if [[ -e "${ROOT}" ]]; then
    echo "${ROOT} already exists and is not this project." >&2
    exit 1
  fi
  git clone https://github.com/Marc0Guo/JEV-Autograder.git "${ROOT}"
fi

cd "${ROOT}"
echo "Configuring ${ROOT}"
uv python install 3.12
uv sync
uv run python -c "import autograde, markitdown, semif_phase1; print('ready', semif_phase1.__version__)"
echo
echo "Configured. Start the app with:"
echo "  uv run autograde"
echo "Then open http://127.0.0.1:8010"
