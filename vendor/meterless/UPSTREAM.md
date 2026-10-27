# Vendored upstream: Meterless H-MEM reference engine

| Field | Value |
|---|---|
| Upstream repository | https://github.com/Meterless/Meterless |
| Commit | `1742dec56a66e58eef9dda2d2b865823758ca8cd` (committed 2026-10-19) |
| Path | `engines/hmem/reference/` (plus `engines/hmem/LICENSE`) |
| Licence | Apache-2.0, kept next to the code as `hmem/LICENSE` |

Nothing under `vendor/meterless/hmem/` is edited. Nirvana drives the engine through
`bridges/hmem.ts`, a JSON-lines process the Python service starts once and talks to over
stdin/stdout. Run the engine's own tests with `npm run hmem:test` after
`cd vendor/meterless/hmem && npm install`.
