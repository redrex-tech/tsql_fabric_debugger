# Contributing

## Development setup

```bash
python -m pip install -e ".[dev]"
```

On non-Fabric machines you also need the **ODBC Driver 18 for SQL Server** to
run anything that opens a real connection.

## Tests

```bash
pytest -m "not integration"          # offline — no warehouse, runs in CI
FABRIC_TSQL_SERVER=... FABRIC_TSQL_DATABASE=... pytest -m integration
mutmut run                           # mutation testing (scanner/parser/engine)
```

The offline suite is fully mocked (a fake pyodbc session, `tests/conftest.py`)
and is what the CI runs. Integration tests need a real Fabric Warehouse and a
valid `az login`, so they are opt-in and not part of CI.

## Pull requests

`main` is protected: open a PR, and the CI (offline tests on Python
3.10–3.12 + a build/metadata check) must pass before merge. Please add or
update tests with any behavior change, and keep the CHANGELOG current.

## Releasing

1. Bump `version` in `pyproject.toml` and update `CHANGELOG.md`.
2. Merge to `main`.
3. Dry-run: run the **Publish** workflow manually targeting TestPyPI, then
   `pip install -i https://test.pypi.org/simple/ tsql-fabric-debugger` in a
   clean venv and smoke-test.
4. Publish a GitHub Release — the workflow uploads to PyPI via Trusted
   Publishing (OIDC, no stored token).
