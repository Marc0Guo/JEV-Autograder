# Contributing

Configure the checkout with one command, then run the tests.

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

macOS and Linux:

```bash
bash install.sh
```

```bash
uv run python -m unittest discover -s tests -v
uv run autograde
```

The app listens on http://127.0.0.1:8010. Scoring uses [SemIf-OpenJev](https://github.com/TheoLeeCJ/SemIf-OpenJev). Document conversion uses [MarkItDown](https://github.com/microsoft/markitdown).

Do not commit `data/`. It holds Canvas tokens. Gradescope passwords are not stored.

`compare_readonly.py` and `run_course_readonly.py` read Canvas and never post grades. They are local comparison tools, not the app.
