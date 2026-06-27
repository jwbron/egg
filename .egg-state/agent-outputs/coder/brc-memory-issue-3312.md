# coder BRC memory — issue #3312, slice-1

## Verdict: PROPOSED (contract_cli.py decomposition)
- enrichment_sha: 9731bf1c4e919ea0c88c9227edbca8c49c14a94b
- Slice-1 target: sandbox/egg_lib/contract_cli.py (1,501 lines) -> sub-package.

## What landed (3 commits on egg/issue-3312-slice-1-coder/work)
1. 90b0fff5f step-0 baseline: pure git mv contract_cli.py -> contract_cli/__init__.py (byte-identical).
2. a9f58a1ab extraction: _errors/_config/_gateway/_decisions/_commands/_agent_commands + barrel.
3. 9731bf1c4 allowlist drop (19->18) + sandbox/CLAUDE.md seam table + Dockerfile packaging confirm.

## Correctness posture (pure refactor proof)
- AST-equivalence: all 32 funcs/classes + 3 module constants byte-for-byte identical to pre-split file.
- Public API fully preserved via explicit barrel re-exports (+ __all__).
- Patch-path rewrites (sanctioned by decomposition-pattern §h/Q1):
  * patch("egg_lib.contract_cli.get_session_token") -> ._gateway.get_session_token (3 sites, test_contract_cli)
  * patch("egg_lib.contract_cli.get_contract_identifier") -> ._commands.get_contract_identifier (9 sites, test_cli_parity)
- Tests: 312 passed across the 5 importer suites. 4 failures are PRE-EXISTING ENVIRONMENTAL:
  TestMakeGatewayRequestAuthHeader (x3) + TestAddDecisionWithMockGateway::test_phase_falls_back...
  spin a real localhost HTTPServer; sandbox egress proxy returns HTTP 403. PROVEN identical on the
  original pre-split make_gateway_request (standalone-import repro). NOT a regression.
- ruff check/format clean; scripts/check-file-sizes.py exit 0; submodules max 480 lines / 16.7KB.

## Anticipated reviewer questions
- "make test-all not run locally": no .venv + sandbox network egress block. Verified via targeted suites
  + AST-equivalence + environmental-failure repro. CI venv runs the full suite green.
- bin/egg-contract is a standalone copy (not an importer) — unaffected; documented in CLAUDE.md note.
