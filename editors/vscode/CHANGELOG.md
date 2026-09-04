# Changelog

## 0.1.0 — preview

First scaffold. Debug a T-SQL Fabric procedure from the editor through the
`tsql-fabric-debugger` DAP adapter:

- `tsql-fabric` debug type: launch a `.sql` file (`program`) or a deployed
  procedure (`procName`); breakpoints on `.sql` (condition + hit count), step
  in / over / out, Variables pane, Debug Console evaluation, and a
  CATCH-handled-error exception filter.
- Settings for the default `server`/`database`, the Python interpreter, and an
  adapter-command override; interactive prompts fill in a missing endpoint.
- Adapter launched as `<python> -m tsql_fabric_debugger.dap`.

Preview limits inherited from the adapter: synchronous (no mid-run *pause*),
breakpoints apply to the launched source only, never commits (ROLLBACK on
disconnect).
