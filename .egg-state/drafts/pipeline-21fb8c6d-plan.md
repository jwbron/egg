## Task: Validate the gateway Confluence read path

Use ONLY the Confluence MCP tools that are exposed in your sandbox environment (discover them — there should be a space-listing tool, a CQL search tool, and a page fetch/execute tool, namespaced like mcp__confluence__*). Do not curl the gateway directly and do not invent tools.

Steps:
1. Call the Confluence space-listing tool (e.g. space_list). Record the HTTP status and the list of space keys returned.
2. Call the Confluence CQL search tool with a query scoped to an allowlisted space: `space = ENG`. Record the HTTP status and the number of results.
3. If step 2 returns results, fetch one returned page via the page-fetch/execute tool (allowed path shape: `api/v2/pages/<id>`). Record the HTTP status.
4. Create a markdown file `PROBE-2970.md` at the repo root summarizing, for EACH call above: the tool name invoked, the HTTP status (e.g. 200 / 401 / 403), and the first line of any error body if it failed.

## Acceptance criteria
- `PROBE-2970.md` exists at the repo root.
- It records the HTTP status of the space-list call and the search call.
- It states plainly whether the Confluence reads succeeded (HTTP 200) or failed with an auth (401) / allowlist (403) error.

## Notes
- Do NOT attempt to bypass the path allowlist via `execute` (only `api/v2/pages/<id>` and `api/v2/spaces/<id>/pages` are permitted; use space_list / search for enumeration).
- No other code changes are needed — this is purely a connectivity probe.