# Contributing

## Repository layout

This is a **monorepo** with two published packages:

- **`tsql-fabric-debugger`** — the Python library (debug engine, DAP adapter,
  CLI). Lives at the repo root: [`src/`](src/), [`tests/`](tests/),
  [`docs/`](docs/), `pyproject.toml`. Published to **PyPI**.
- **T-SQL Fabric Debugger** — the VS Code extension, a GUI over the DAP
  adapter. Lives in [`editors/vscode/`](editors/vscode/) with its own
  `package.json`, tests and CHANGELOG. Published to the **VS Code
  Marketplace**.

CI (`.github/workflows/ci.yml`) tests both on every push/PR.

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

The two packages version and ship independently.

### Library → PyPI

1. Bump `version` in `pyproject.toml` **and** `src/tsql_fabric_debugger/__init__.py`,
   update `CHANGELOG.md`.
2. Merge to `main`.
3. Dry-run: run the **Publish to PyPI** workflow manually targeting TestPyPI,
   then `pip install -i https://test.pypi.org/simple/ tsql-fabric-debugger` in
   a clean venv and smoke-test.
4. Publish a GitHub Release (tag `vX.Y.Z`) — the workflow uploads to PyPI via
   Trusted Publishing (OIDC, no stored token). The PyPI trusted publisher must
   match repo `redrex-tech/tsql_fabric_debugger`, workflow `publish.yml`,
   environment `pypi`.

### Extension → VS Code Marketplace

1. Bump `version` in `editors/vscode/package.json`, update its `CHANGELOG.md`,
   merge to `main`.
2. Add the `VSCE_PAT` secret once (Azure DevOps PAT with Marketplace > Manage
   for publisher `redrex-tech`); optionally `OVSX_PAT` for Open VSX.
3. Run the **Publish VS Code extension** workflow from the Actions tab. Use the
   `dry_run` input first to get a `.vsix` artifact without publishing.
