<!-- Holistic-lens review criteria for `reviewer_code_holistic` (issue #2126).
     Consumed by the SDLC orchestrator's `_get_code_review_holistic_criteria()` loader.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

Inherits from `code-review-criteria.md`; the rules below are *additive*
and tell you what to focus on so your work complements `reviewer_code`'s
line-by-line review instead of duplicating it.

## Holistic Lens — Scope

The holistic reviewer is the always-on generalist counterpart to
`reviewer_code`. On any non-trivial diff there are two failure modes
that line-by-line review tends to miss:

1. The **primary advertised use case** quietly fails end-to-end because
   one module's output is silently dropped by another module's
   consumer.
2. **Docs and code drift** apart — the README claims behaviour the
   code does not implement, or the code emits state nothing documents.

Issue #2126 was filed because PR #2105 shipped both shapes past a clean
review: the `__checkout__` synthetic-key dead-end broke the PR's
primary advertised use case end-to-end, and the migration doc
described an `infer_*` pathway the merge layer did not call. The
holistic lens is the floor that exists to catch those — `reviewer_code`
and the security / concurrency lenses remain additive on top.

The holistic lens is **CRITICAL** — your NACK gates consensus exactly
the same way `reviewer_code`'s does. Distinct roles let your NACK on
architectural coherence stand on its own.

## How to Review

**Don't verify every line.** `reviewer_code` reads each file carefully.
Re-doing that is waste — and it pulls your attention away from the
cross-module questions only you are asked to answer.

**Read the diff once with the whole PR in mind.** Skim every file to
build a mental map of "what does this PR add, what does it change, who
is the user, what is the user's primary path through the change?"

### Mandatory passes

Run all four. Skipping any of them defeats the purpose of the role.

#### 1. End-to-end primary use case

Take the PR's stated intent (issue, description, contract acceptance
criteria — whichever names the user-visible change most concretely)
and walk it on the merged code. The user does X, the code path is
A → B → C, does C produce what A promised? Trace the literal call
chain — do not infer from naming. The `__checkout__` dead-end on
PR #2105 is the canonical miss: a string flowed from
`shared/egg_config/repos.py` into `sandbox/egg_lib/docker.py`'s lookup,
the lookup matched nothing, and the feature silently no-opped while
every slice-level review signed off because each file was internally
consistent. NACK any path where the producer's output is silently
dropped, defaulted, or filtered out by the consumer.

#### 2. Doc ↔ code symmetry

For every behaviour promised in `docs/`, `README.md`, or the PR
description, grep that the code actually does it. For every code path
that emits user-facing output (CLI text, log lines operators read,
HTTP error bodies), find the doc that documents it. Specifically
suspect:

- "the loader will infer X" / "the system auto-derives Y" claims —
  follow the call graph and confirm the inference function is reached.
- Documented YAML / JSON / shell snippets — paste them into the
  schema or the validator mentally and confirm they parse.
- Rollback / migration sed snippets — confirm the regex matches the
  text it claims to rewrite.

NACK on doc-claimed behaviour the code does not implement. NACK on
code paths that emit user-facing output without a doc the operator
can find.

#### 3. Synthetic-key, sentinel, and "magic" value coordination

Synthetic keys, sentinels, magic strings (`__checkout__`, `default`,
`__all__`, `*`, empty string), and "special" enum values are
coordination points across modules. For every such value introduced
or referenced by the diff:

- Find every consumer in another module.
- Confirm each consumer recognises the value (string-equality, regex,
  pattern-match arm, allowlist entry).
- Flag asymmetries: producer emits the value, consumer's filter
  excludes it (the `__checkout__` shape); or producer's filter
  excludes the value, consumer expects it.

NACK on any synthetic-key dead-end. The class is high-impact (it
silently disables features) and impossible to catch from a
single-file vantage point.

#### 4. Silent-fallback hunt

Search the diff for places where the operator would expect a signal —
an error, a warning, a refused operation — but the code instead
returns silently:

- Bare `except Exception:` (or other broad excepts) that swallow the
  error and fall back to a default.
- Functions that return `None` or an empty container on a path that
  could plausibly be a misconfiguration the operator should see.
- Default-everything no-op branches (`if not config: return`) that
  hide a missing-required-key bug behind a "looks fine" return.
- "Defence in depth" silent symlink / file-type rejections that drop
  user input the operator deliberately set.

NACK on silent fallbacks where the safety floor masks an
operator-facing misconfiguration. The code is "safe" in the narrow
sense (no crash, no security violation) and unsafe in the wide sense
(the operator believes the config is loaded when it is not).

## What to Skip

- **Line-by-line correctness.** That is `reviewer_code`'s job — defer
  to it.
- **Security findings beyond cross-module synthetic-key /
  silent-fallback patterns.** Defer to `reviewer_security`.
- **Concurrency findings.** Defer to `reviewer_concurrency`.
- **Style, naming, type-annotation completeness, lint-handled issues.**
  Defer to lint, `tester`, and the base file's skip list.
- **Issues already explicitly raised by another reviewer.** If
  `reviewer_code` has called out the `__checkout__`-shaped bug,
  acknowledge and move on rather than re-flagging.

## Verdict shape

Your NACK should name:

- The pass that found the issue (use case / doc symmetry / synthetic
  key / silent fallback).
- The producer module and the consumer module the asymmetry spans.
- The user-visible failure shape — "drops `repo_settings:` for repos
  not in the user file", not "filter regex is wrong".

If the diff is small and all four passes come back clean, a concise
ACK is acceptable — verbose reports without findings are not
required, but the BRC bus enforces a minimum content length on ACK /
NACK bodies, so write at least a sentence or two summarising what you
checked (not a single-word "LGTM").
