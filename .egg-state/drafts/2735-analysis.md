# Analysis: Claude Code substrate — reconstruct gateway + sandbox enforcement from native primitives

> Issue: #2735 | Phase: refine

## Problem Statement

The Claude Code substrate landed by #2623 (spike) and rolled out by #2717 (slices 1–2) ships **only one** piece of enforcement: a PreToolUse hook that gates file-write tool calls (Write / Edit / MultiEdit / NotebookEdit / Bash redirections / `cp` / `mv` / `git mv` / etc.) against the role-pattern checker in `shared/egg_restrictions/patterns.py:768 build_agent_patterns`. The substrate's installed template — `orchestrator/substrate/claude_code/settings.template.json` — registers exactly that one hook and nothing else (file is 15 lines; no `sandbox` block, no `permissions` block, no network policy, no git/gh subcommand filtering, no self-protection on the enforcement config itself).

The ADR (`docs/architecture/claude-code-substrate.md`) records three security deltas vs the k3s gateway as **"accepted, scoped to trusted repos"** rather than closed:

- **R1 — trust-context shift** (ADR §lines 199–212): the gateway proxies all Anthropic-API traffic and the sandbox sees only a `sk-ant-oat01-PROXY-INJECTED-*` placeholder (`gateway/anthropic_credentials.py`); on this substrate the parent Claude session holds the real key and every spawned subagent inherits it.
- **No network isolation**: the gateway's public/private modes + per-pipeline domain allowlist have no substrate equivalent.
- **REC5 — no cost cap** (ADR §lines 242–248; tracked separately in #2736).

These deltas are exactly what blocks autonomous (unattended) operation per #2736 — the blast radius of any tool call inside the parent session is unbounded, and "scoped to trusted repos" is the only thing standing in for real enforcement. The primitives to close this exist natively in Claude Code (the `sandbox.*` block in `settings.json` + PreToolUse hooks dispatched on the `Bash` matcher); the substrate just isn't using them yet.

The five **Done when** clauses from the issue body are the concrete scope:

1. Substrate config (the installed `settings.template.json`) enables Claude Code's OS sandbox with `filesystem.allowWrite` / `denyWrite` and `network.allowedDomains` scoped to what an SDLC pipeline run legitimately needs.
2. A gateway-equivalent git/gh-operation PreToolUse hook is installed (subcommand allowlist, force-push / destructive-flag / `--no-verify` blocking, restricted-path push rejection in parity with the gateway's `403 restricted_path_modified`, `.git/hooks` neutralization via `git -c core.hooksPath=/dev/null`, credential injection scoped to network operations) and **reuses** `shared/egg_restrictions` rather than forking it.
3. Self-protection: `denyWrite` covers `.claude/settings.json` and the hook scripts so the running agent cannot rewrite its own enforcement config.
4. The enforcement layer sits on the **actual subagent dispatch path**. Today `ClaudeCodeSpawner` re-hosts `shared/egg_harness` in-process with its own `ToolRegistry` (`orchestrator/substrate/claude_code/spawner.py:9–27`, deliberately documented as a scope decision), which **bypasses PreToolUse hooks entirely** — ADR §R2 (lines 214–226) is explicit about this. Whatever dispatch model #2717's R15 work settles on, this issue owns the guarantee that subagent tool calls route through the enforcement layer; do not duplicate the dispatch-model work.
5. The ADR moves R1, network isolation, and restricted-path enforcement from "accepted delta" to "covered by native primitives" and documents the residual gaps honestly — the parent session still holds the API key, and an allowlisted-egress model still permits `git push` to arbitrary `github.com/owner/repo` targets.

The Khan/agent-settings pattern reproduced verbatim in the issue body is the proof-of-mechanism; the SDLC pipeline working this issue does not have access to that internal repo, so the issue body is the canonical reference for the JSON shape and the per-subcommand filter logic.

## Current Behavior

### Enforcement seam today

The substrate's `PreToolUseHookPolicy.install()` (`orchestrator/substrate/claude_code/policy.py:70–166`) writes `.claude/settings.json` from the 15-line template at `orchestrator/substrate/claude_code/settings.template.json`. The template registers exactly one hook entry under `hooks.PreToolUse`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [{
          "type": "command",
          "command": "python3 -m orchestrator.substrate.claude_code.hook_entry"
        }]
      }
    ]
  }
}
```

No `sandbox` block. No `permissions` block. The hook script itself (`orchestrator/substrate/claude_code/hook_entry.py`, 780 lines) does heavy lifting on the Bash matcher — it parses redirects, `cp`/`mv`/`install`/`rsync`/`tee`/`dd of=`/`sed -i`/`ln`/`rm`/`chmod`/`chown`/`truncate`/`awk -i`/`perl -i`, network-fetch-to-disk verbs (`wget -O`, `curl -o`), git-mutation verbs that write the worktree (`git mv`, `git rm`, `git apply`, `git checkout --`, `git restore`), archive extraction (`tar -x`, `unzip`), nested shells (`bash -c`, `sh -c`, `zsh -c`), and `python -c` write heuristics. Each extracted write path is checked against `build_agent_patterns` via `check_agent_file_access(role, paths, repo=None)` and denied if outside the caller's role's allow-list.

What `hook_entry.py` does **not** do today:
- No `git push` / `git fetch` / `git clone` / `gh` subcommand inspection — those are not file-write verbs and pass through.
- No flag denials (`--force`, `--force-with-lease`, `-f`, `--no-verify`, `--mirror`, `--delete`, `reset --hard`, `clean -f`, `branch -D`, `config --global` / `--system`, `rebase --exec`, `--upload-pack`, `--exec`, `--receive-pack`, `--config <kv>`).
- No restricted-path push rejection (diff the push range against the role's blocked patterns).
- No credential-helper injection.
- No `.git/hooks` neutralization.
- No SSH-URL handling (rewrite vs deny).

### Gateway's git/gh policy categories (the "parity" target)

Listing here so the plan phase has a checklist:

1. **Per-role file-write blocks on push** — `gateway/phase_filter.py:61–138` (`FileRestriction.is_file_blocked`) + `gateway/gateway.py:1603–1650` (commit-attribution-aware partition of the push range into own-authored vs pulled-from-other-role files). Response: `403 restricted_path_modified`. Disabled by `EGG_AGENT_RESTRICTIONS_ENFORCE=false`.
2. **Force-push** — `gateway/gateway.py:1100–1250`. Accepts `force` / `force_with_lease` flags in the request body; **no hardcoded main/master protection** — relies on branch ownership downstream.
3. **Push-target enforcement (consensus)** — `gateway/gateway.py:1477–1507`. Consensus pushes must match `session.assigned_branch`; 403 on mismatch.
4. **Pipeline-push enforcement** — `gateway/gateway.py:1430–1469`. `EGG_PIPELINE_PUSH_ENFORCEMENT` gates per-session pipeline ownership; 403 on mismatch.
5. **Credential injection** — `gateway/anthropic_credentials.py` reads `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` from `~/.config/egg/secrets.env` and injects the Anthropic header server-side; sandbox sees only the proxy placeholder. **API-level only — not git credentials.** Git auth on the gateway side is via per-session GitHub-App tokens; that path is gateway-internal.
6. **Subcommand allowlist** — there is **no** subcommand allowlist on the gateway side today. `gateway/git_client.py:900–980` carries short-flag → long-flag abbreviation maps per command, but the gateway does not reject `git <novel-subcommand>` outright. This is a **net-new** policy the substrate would introduce.

### Dispatch path bypass (R2 today)

`ClaudeCodeSpawner` (`orchestrator/substrate/claude_code/spawner.py:1–236`) re-hosts `shared/egg_harness` in-process: the spawner imports `client.run_agent(...)` and the harness drives the AnthropicProvider directly through its own `ToolRegistry.set_permission_callback(...)`. Tool calls inside this loop **never** transit the Claude Code parent's PreToolUse hooks — the harness's tool dispatch is a parallel pipeline. ADR §R2 acknowledges this explicitly: "production dispatch under cq-3 remains on `ClaudeCodeSpawner` (the harness re-host model) — `shared/egg_harness/client.py:60–150` uses its own `ToolRegistry.set_permission_callback(...)` and does NOT invoke the PreToolUse hook. R2 therefore validates hook *logic* given accurate `EGG_AGENT_ROLE` propagation; it does **not** validate that Claude Code itself propagates `EGG_AGENT_ROLE` correctly under real nested Agent-tool dispatch."

The slice-1 R2 nested-dispatch test (`integration_tests/regression/test_pretooluse_hook_nested.py`, TASK-1-5) writes a verdict to a per-test `tmp_path` r2-verdict.json that gates slice 5 of #2717's contingent R15 model-(b) migration. **The verdict is *latently* load-bearing, not *currently* load-bearing**: ADR §R2 (line 224) explicitly states "the R2 result becomes load-bearing only if cq-3 flips to Agent-tool dispatch in a future issue." Today's production dispatch (the harness re-host) bypasses the PreToolUse hook regardless of the verdict's shape; the verdict becomes load-bearing only if `cq-8` opt-2/opt-3 or a future cq-3 flip routes subagents through the Agent-tool surface.

### Trust-context shift today (R1)

The gateway's `anthropic_credentials.py` reads the key from disk on first tool request, mtime-caches it, and injects the `x-api-key` / `Authorization: Bearer` header into outbound HTTPS to `api.anthropic.com`. The sandbox container sees only the proxy placeholder.

Under the substrate, the parent Claude session boots with the operator's real API key in env / OAuth context. Every subagent the orchestrator spawns inherits this. The four mitigations the spike shipped (ADR §lines 199–212):

1. *"Skill imports never log credentials."*
2. *"The PreToolUse hook entry script does not exfiltrate environment"* — only emits `deny` / `allow` + `message`.
3. *Install docs name the trust-context shift explicitly* (SKILL.md).
4. *Future-work credentialed-proxy mode* — deferred.

The acceptance argument is "trusted-repo SDLC streams only" + "k3s remains available indefinitely (cq-9) for any caller that needs credential isolation."

## Constraints

- **No new third-party deps** (feedback Q3 from #2623): the hook entry script must stay pure-stdlib Python or call only into `shared/egg_restrictions/` and gateway-side modules already imported elsewhere.
- **Single source of truth for role→path patterns** (Goal #2, cq-6, ADR §line 81): the substrate hook calls the same `build_agent_patterns` / `check_agent_file_access` symbols the gateway calls. The new git subcommand allowlist needs a similar SoT story (registered in `cq-4`).
- **Out of scope for this issue** (issue body explicit): cost cap (REC5 / #2736), the implement/pr phase rollout (#2717 slices 3–4), the k3s substrate's own enforcement (cq-9 says k3s stays as-is).
- **k3s substrate stays co-equal** (cq-9): nothing this issue does should break `EGG_SUBSTRATE=k3s`. Where logic is shared (e.g. `shared/egg_restrictions/` extensions for the subcommand allowlist), the gateway must continue to work unchanged.
- **Coordinate with #2717's R15** (Goal #4): this issue does not decide between Claude Code subagent model (a) and (b). It must ship enforcement that lands on the dispatch path **regardless** of which model #2717's slice 5 picks — or sequence itself to land after the R15 decision. See `cq-8`.
- **Claude Code's sandbox sandboxes Bash only**: per the Claude Code docs (`code.claude.com/docs/en/settings`), the `sandbox.*` block applies to processes spawned by the `Bash` tool. The Edit / Write / Read / MultiEdit / NotebookEdit tools are gated by `permissions.allow / deny`, not by `sandbox.filesystem`. The issue body is correct on this distinction ("Two distinct permission layers — easy to confuse"). The implementation must split path-write enforcement across both layers.
- **Khan setting names may not be current**: the issue body's `enableWeakerNetworkIsolation: true` does not match the current Claude Code settings docs, which describe `enableWeakerNestedSandbox` (macOS-only). Plan phase must verify which exact key name the substrate writes; `feedback-1 Q3` collects the macOS-support context that determines whether the flag is needed at all.
- **Claude Code's `if:` conditional matcher in the Khan JSON snippet** (`"matcher": "Bash", "hooks": [{ "if": "Bash(git *)", ... }]`) is not in mainline Claude Code's documented hook contract. Egg's existing `hook_entry.py` already routes Bash commands by parsing the command itself; the natural extension is to keep that single-hook-script shape rather than rely on a Claude-Code-extension matcher field. This is the trade-off behind `cq-7`.
- **Hook script execution context**: the hook script runs as a subprocess of the parent Claude Code session (not as a subagent). The parent's environment / cwd is what the hook sees. Role resolution today uses `EGG_AGENT_ROLE` env + the live-PID-stamped sentinel at `~/.claude/egg-active-role.json` (`hook_entry.py:677–742`); the new git/gh logic must reuse this so the role-resolution surface stays single.
- **`core.hooksPath=/dev/null` is the substrate default**: every allowed `git` invocation has `-c core.hooksPath=/dev/null` injected after the `git` keyword. This neutralizes any `.git/hooks/*` that an attacker (or unhygienic upstream) can land in the cloned repo — a known RCE vector when checking out untrusted commits. The substrate does not surface this as an HITL because the cost of skipping it (RCE on every clone of a hostile repo) outweighs every legitimate use case for repo-supplied hooks during an SDLC pipeline run. The planner should treat this injection as non-negotiable.
- **File-size cap on the hook entry script**: `orchestrator/substrate/claude_code/hook_entry.py` is 780 lines today; the git/gh extension adds an estimated 400–800 lines, putting the file in the 1200–1600 range — within striking distance of the 1500-line cap in `scripts/file-size-allowlist.yaml` (`orchestrator/CLAUDE.md` cites this). If `cq-7` resolves to opt-1 (extend `hook_entry.py`), the planner should pre-allocate a decomposition under `orchestrator/substrate/claude_code/_hook_entry/` per `docs/guides/decomposition-pattern.md` rather than fight the cap mid-implementation.

## Options Considered

The implementation scope is essentially settled by the issue body (the JSON shape, the per-rule list, the SoT reuse mandate). The variability is in *how* to slice the work, *which* dispatch-model assumption to bake in, and *which* per-rule defaults to ship. Most of this is captured in the registered HITL decisions; the high-level options below summarise the structurally different approaches.

### Option A: Single PreToolUse hook script extension (one slice)

**Approach**: Extend the existing `orchestrator/substrate/claude_code/hook_entry.py` to add git/gh-command parsing (subcommand allowlist, flag denials, restricted-path push diff, credential-helper / `core.hooksPath` rewrite, SSH-URL handling per `cq-5`). Add a `sandbox` block to `settings.template.json` with `filesystem.allowWrite` / `denyWrite` (self-protection per `cq-2`) and `network.allowedDomains` (egress scope per `cq-1`). The new git subcommand allowlist lives in `shared/egg_restrictions/git_policy.py` (per `cq-4`) and is imported by both `gateway/git_client.py` (for parity with the gateway path) and the substrate hook. The R15 question is parked: assume model (a) (`cq-8` opt-1), revisit if the slice-1 R2 verdict file says `fail`. The ADR is updated in the same PR.

**Pros**:
- One PR, one review cycle, one ADR commit — minimum coordination overhead.
- The hook keeps a single role-resolution surface (Khan's two-script split would duplicate the role/sentinel-file logic).
- File-write enforcement and git/gh enforcement live in one process, so cross-cutting policy decisions (e.g. "this path is restricted, also reject pushes that touch it") share state.

**Cons**:
- A single PR touching `orchestrator/substrate/claude_code/`, `shared/egg_restrictions/`, the ADR, and tests is large — slows review and increases conflict surface with #2717's in-flight slices 3–4.
- Couples the sandbox-block work (purely declarative JSON) with the git-filter work (substantive Python + a heap of new tests). A bug in either delays the other.

### Option B: Two parallel slices — [sandbox-block + denyWrite] || [git/gh-filter + restricted-path-push]

**Approach**: Sibling slices. Slice A: the declarative `sandbox` block in `settings.template.json` (filesystem + network + self-protection per `cq-1` / `cq-2`); updates ADR §R1 and the "no network isolation" delta. Slice B: extends `hook_entry.py` with git/gh subcommand allowlist + flag denials + restricted-path push + credential-helper / `core.hooksPath` rewrite + SSH-URL handling; updates ADR's restricted-path delta. ADR final-consolidation lands as the trailing edit on whichever slice merges second (or as a tiny child slice). The slice-DAG runs A and B in parallel per `docs/architecture/slice-dag.md`.

**Pros**:
- Parallel landing: review concurrency cuts wall-clock time roughly in half versus Option A. Sibling slices in the same wave run side-by-side under the slice scheduler.
- Decoupled review surfaces — sandbox-block reviewers see only JSON + ADR; git-filter reviewers see only Python + tests. Each PR is smaller and easier to reason about.
- Slice A is genuinely independent: the `sandbox` block has no dependency on the git-filter implementation; it just lands the JSON and the self-protection denyWrite list.

**Cons**:
- ADR R1/network/restricted-path delta is split across two PRs — the operator may see partial coverage claims while only one of the two slices has landed. Mitigate by phrasing each PR's ADR edit as "*this PR adds X; companion PR Y adds Z*" rather than as a coverage claim.
- Slice B is the substantive risk; slice A landing first may give the operator a false sense of done-ness.

### Option C: Three slices with a multi-parent ADR-consolidation child (Option B + dedicated ADR slice)

**Approach**: Same as Option B for slices A and B, plus a third slice C that owns the ADR R1/network/restricted-path delta update and depends on both A and B. Slice C is a multi-parent node in the slice DAG, which `docs/architecture/slice-dag.md` rejects — the planner would have to serialise A and B into a chain or merge C into one of them. So this option is functionally equivalent to "Option B + the ADR edit goes on the second-to-merge of A/B."

**Pros**:
- Cleanly isolates the ADR coverage claim to one PR.

**Cons**:
- Multi-parent slice is rejected by the slice DAG; the operator would have to either serialise A → B → C or fold C into A or B. The serialisation costs the parallelism that motivated Option B.
- Reviewer surface for slice C is documentation-only, which is hard to justify as a standalone PR.

### Option D: Two slices with a dependency — [hook + sandbox] → [ADR consolidation]

**Approach**: Slice A is the full code + JSON change (sandbox block + denyWrite + git/gh-filter + restricted-path-push). Slice B is a follow-up ADR-only PR that consolidates the coverage claim once the code has landed and the operator has observed it working.

**Pros**:
- ADR coverage claim lands after empirical evidence — the operator sees the new substrate behave correctly before signing off on the language that says R1 is now "covered."

**Cons**:
- Two sequential PRs: same wall-clock cost as Option A, plus a delay before the ADR catches up.
- The bulk of Option A's review surface remains in slice A — no parallelism gain.

## Recommended Approach

**Recommend Option B, conditioned on `cq-8`**: two parallel slices — [A] sandbox-block + denyWrite + partial ADR update || [B] git/gh-filter hook + restricted-path-push + partial ADR update. The slice contents adapt to `cq-8`'s answer:

- **If `cq-8` = opt-1 (assume model (a), proceed in parallel with #2717)** — Option B as described. The new sandbox block + git/gh-filter cover the parent Claude Code session's direct Bash/tool calls. **`ClaudeCodeSpawner`'s in-process harness re-host continues to bypass the PreToolUse hook**: ADR §R2 already documents this; Slice A's ADR edit must explicitly call out that Goal #4 ("subagent tool calls route through the enforcement layer") becomes a **documented residual gap** for subagents until `cq-8` resolves to opt-2 or opt-3, or until #2717's R15 work flips the dispatch model. This is honest scope — better than implying enforcement coverage the substrate cannot deliver under opt-1.
- **If `cq-8` = opt-2 (wait for slice 5 of #2717's R15 verdict)** — this issue parks on #2717. Option B does not start until R15 decides. If R15 keeps model (a), Option B proceeds as opt-1 above; if R15 flips to model (b), Option B proceeds as opt-3 below.
- **If `cq-8` = opt-3 (build for both — hook + agent-side enforcement)** — Option B expands to a third workstream in slice B: agent-side enforcement at `sandbox/egg_agent_tools/handlers/restrictions.py` that re-validates the caller's role + tool input against `build_agent_patterns(...)` whenever the harness dispatches a tool call. This adds ~150–250 lines and a parallel test surface (`shared/tests/test_agent_side_restrictions.py`-style). The result: Goal #4 is fully covered regardless of whether the dispatch path is the parent's PreToolUse hook or the in-process harness.

Rationale for Option B over A/C/D:

1. The work is substantively two independent surfaces. The `sandbox` block is declarative JSON + the merge logic already in `PreToolUseHookPolicy._merge_hooks(...)` (which dedupes idempotently per reviewer_code v2 blocker #2). The git/gh-filter is substantive Python — subcommand parsing, push-range diffing, credential-helper rewriting — with a real test surface. Mixing them in one PR (Option A) inflates the review surface and delays both ends of the work.
2. The slice scheduler runs siblings in parallel per `docs/architecture/slice-dag.md`. Two parallel PRs ≈ half the wall-clock of one big PR or two sequential PRs.
3. Option C's multi-parent ADR slice is rejected by the slice DAG, and a doc-only standalone PR is hard to justify on its own.
4. Option D buys an empirical-evidence-first ADR update but costs the parallelism that motivates the decomposition in the first place; the operator can review the ADR edits inline with the code in the second-to-merge slice.

**Goal #5 ADR language under Option B**: the ADR edit moves R1 from "accepted delta" to **"mitigated; residual gap = parent session still holds the real Anthropic API key"** — not to "covered." The substrate's sandbox network allowlist + denyWrite on the credential file family + hook-script-no-exfiltrate property close the easy exfiltration paths, but an in-process subagent in the parent's own address space can still read the env. Calling this "covered" would mis-state the security posture. `feedback-1 Q4` asks the operator to confirm the exact language; default to "mitigated; residual" until they reply.

The final slice-shape decision is registered as `cq-9` so the operator can override. The other open questions — most importantly the network-allowlist scope (`cq-1`), the deny-vs-ask policy (`cq-3`), the SoT location for the subcommand allowlist (`cq-4`), the SSH-URL handling (`cq-10` — see "cq-5 superseded" note below), the push-target enforcement (`cq-6`), the dispatch-path coordination (`cq-8`), the `permissions.deny` mirror (`cq-11`), and the SessionStart bootstrap (`cq-13` — see "cq-12 superseded" note below) — feed into the *content* of slices A and B regardless of how the slice DAG is shaped.

## Open Questions

The following decisions and feedback items are registered against this contract and visible to the operator via the issue's review surface. Every question below requires an answer before the plan phase begins.

> **Errata** (read before answering):
>
> - **`cq-5` is superseded by `cq-10`.** The original `cq-5` question text lost its example URLs (`git@github.com:owner/repo`, `ssh://git@github.com/owner/repo`) to shell escaping when the decision was registered. `cq-10` is the same question with the URLs preserved verbatim. Disregard `cq-5`; answer `cq-10` instead. The options are identical between the two.
> - **`cq-11` is superseded by `cq-14`.** The original `cq-11` option-1 label lost the literal backticked `permissions.deny` token to shell escaping; the option reads "mirror into a parallel  list" in the contract UI, ambiguous against option-3. `cq-14` is the same question with the token preserved. Disregard `cq-11`; answer `cq-14` instead.
> - **`cq-12` is superseded by `cq-13`.** The original `cq-12` SessionStart question accidentally executed `gh auth token` in a shell context and substituted a **live GitHub Apps token value** into option-1's label text. An `OVERSEER_ALERT` has been broadcast asking the operator to rotate the token and scrub `.egg-state/contracts/issue-2735.json`. `cq-13` is the same question with safer escaping. Disregard `cq-12` entirely; answer `cq-13` instead.
>
> **Shell-escaping incident summary.** The above three supersessions all stem from the same root cause: `egg-contract add-decision --options` is invoked via Bash, and Bash interprets unescaped backticks and certain `:`-followed tokens before the CLI sees them. The plan/implement phases should standardize on `mcp__sdlc__register_open_question` (or shell single-quotes throughout) for any option text containing shell metacharacters. A follow-up issue should consider hardening `egg-contract add-decision` to reject unquoted backticks in `--options` arguments.

### Multiple-choice decisions

<!-- egg-hitl-decision id=cq-1 -->

**Network egress allowlist for the substrate's OS sandbox — which set of domains should the substrate ship by default?**

- [ ] Minimal: localhost, github.com, api.github.com, api.anthropic.com (Claude API + GitHub only — pulls/installs blocked, deny if pip/uv needed mid-run)
- [ ] Standard: minimal + pypi.org, *.pythonhosted.org, registry.npmjs.org (covers Python/Node dep install during agent runs)
- [ ] Permissive: standard + ghcr.io, docker.io, *.githubusercontent.com, sentry/telemetry endpoints used by tooling (closer to default-Claude-Code dev session)
- [ ] No network allowlist (omit the sandbox.network block; rely on hook-side gates only)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-2 -->

**Self-protection scope: which paths should the substrate add to sandbox.filesystem.denyWrite so the running agent cannot rewrite its own enforcement config?**

- [ ] Minimal: .claude/settings.json, .claude/settings.local.json, .claude/hooks/ (project-level config only)
- [ ] Standard: minimal + ~/.claude/settings.json, ~/.claude/hooks/, orchestrator/substrate/claude_code/hook_entry.py, orchestrator/substrate/claude_code/settings.template.json, shared/egg_restrictions/ (project + user-scope + the hook scripts + the role-pattern source-of-truth)
- [ ] Maximum: standard + .git/hooks/, gateway/, the entire orchestrator/substrate/ tree (every file the parent or any subagent could conceivably weaponize)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-3 -->

**Deny-vs-ask policy: what should a PreToolUse hook do when it sees a disallowed git/gh operation under the autonomous (#2736) execution model? Pick one verdict shape for the autonomous-safe default.**

- [ ] Hard-deny everything off the allowlist (block + structured deny message; never surface to a human)
- [ ] Hard-deny operations, but route a small explicitly-named subset (e.g. push to a brand-new remote, gh issue close on a non-egg repo) to a HITL escalation channel via egg-contract
- [ ] Mirror Khan's 'ask' verbatim (pause for human approval at runtime) — accept that this turns 'autonomous' runs into supervised runs whenever a guarded op is hit
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-4 -->

**Where should the git/gh subcommand allowlist + per-subcommand flag-deny list live so the gateway and the substrate hook stay in sync (single source of truth)?**

- [ ] Extend shared/egg_restrictions/ with a new git_policy.py module that exports the allowlist + flag-deny set; gateway/git_client.py and the substrate hook both import from it
- [ ] Fork the list: substrate keeps its own copy in orchestrator/substrate/claude_code/git_policy.py and accepts drift with the gateway; reconciliation is a future-issue concern
- [ ] Hardcode the list inside the existing hook_entry.py module (cheapest, but couples policy to the hook implementation)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-5 -->

**[SUPERSEDED by cq-10 — disregard cq-5; the original question text lost its example URLs to shell escaping. Answer cq-10 below.]**

<!-- egg-hitl-decision id=cq-10 -->

**SSH GitHub URL handling: how should the substrate hook handle SSH-style GitHub remotes of the form `git@github.com:owner/repo` or `ssh://git@github.com/owner/repo`? (Replaces cq-5.)**

- [ ] Rewrite SSH URLs to HTTPS so the injected credential helper resolves (mirrors the Khan pattern)
- [ ] Hard-deny SSH URLs (forces operator to configure HTTPS remotes; eliminates the rewrite-and-inject vector entirely)
- [ ] Allow SSH URLs as-is; rely on the developer's existing ssh-agent / GitHub Apps credentials and skip credential-helper injection on those commands
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-6 -->

**Push-target enforcement: should the substrate require an explicit per-pipeline push allowlist (owner/repo), or accept ADR's 'allowlisted-egress still permits push to arbitrary github.com repos' residual gap?**

- [ ] No per-repo allowlist: accept the residual gap, document it in the ADR (cheapest; the gateway also does not enforce this today — gateway/gateway.py:1430-1469 only enforces per-container pipeline ownership, not per-target-repo)
- [ ] Per-pipeline push allowlist: the orchestrator computes {origin owner/repo} from the active session and the hook rejects pushes to any other repo (parity goal becomes 'gateway + something more')
- [ ] Per-org push allowlist: hook reads an EGG_ALLOWED_PUSH_ORGS env var (default jwbron); rejects pushes outside it (catches typo-squatting and accidental cross-org pushes without per-pipeline plumbing)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-7 -->

**Hook entry-point implementation language for the new git/gh logic**

- [ ] Extend the existing Python hook_entry.py (already used for file-write gating; everything stays in one process so role-resolution + restriction-checker imports are reused)
- [ ] Add separate Bash/Python git-filter and gh-filter scripts wired in via two new settings.template.json hook entries (closer to Khan's two-script shape; cleaner separation but duplicates the role-resolution + sentinel-file logic)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-8 -->

**Dispatch-path integration with #2717's R15 (model-(a) vs model-(b)): which dispatch model does this issue assume — and does this issue's hook-layer work block on the R15 verdict, or proceed in parallel?**

- [ ] Assume model (a) (PreToolUse hook is the primary seam, matching today's #2717 default) — proceed in parallel with #2717's slice 5; revisit if R2 verdict flips
- [ ] Wait for slice 5 of #2717 to settle the R15 decision before starting; this issue lands only after model (a) vs (b) is final
- [ ] Build for both: ship the PreToolUse hook AND the agent-side enforcement at sandbox/egg_agent_tools/handlers/restrictions.py (model (b) fallback), so the issue is correct regardless of R15 outcome
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-9 -->

**How should this work be decomposed into slices?**

- [ ] Single slice: sandbox-block + denyWrite + git/gh-filter + ADR update ship together (1 PR)
- [ ] Two slices in parallel: [A] sandbox-block + denyWrite + ADR §R1/network update || [B] git/gh-filter hook + restricted-path-push + ADR restricted-path update (2 PRs)
- [ ] Three slices, parallel: [A] sandbox-block + denyWrite || [B] git/gh-filter hook + restricted-path-push || [C] ADR R1/network/restricted-path delta consolidation depending on A and B (3 PRs; C is multi-parent — planner must serialise upstream)
- [ ] Two slices with dependency: [A] git/gh-filter hook + restricted-path-push + sandbox-block + denyWrite -> [B] ADR R1/network/restricted-path delta update (2 PRs)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-11 -->

**[SUPERSEDED by cq-14 — disregard cq-11; option-1's label lost the literal `permissions.deny` token to shell escaping. Answer cq-14 below.]**

<!-- egg-hitl-decision id=cq-14 -->

**Self-protection — `permissions.deny` mirror: Claude Code has two enforcement layers — `sandbox.filesystem.denyWrite` gates Bash-spawned subprocesses, `permissions.deny` gates the Write/Edit/MultiEdit/NotebookEdit tools. `cq-2` names paths for the sandbox layer only. Should the substrate ALSO populate a `permissions.deny` list with the same paths so the tool-layer cannot rewrite the enforcement config either? (Replaces cq-11.)**

- [ ] Yes — mirror cq-2 selection into a parallel `permissions.deny` list (close both layers — `sandbox.filesystem.denyWrite` AND `permissions.deny` carry the same path set)
- [ ] Yes, but only for `.claude/settings.json` and the hook-script files (narrower mirror — covers self-protection but not the role-pattern source-of-truth)
- [ ] No — accept that Write/Edit/MultiEdit tool calls can rewrite the substrate's enforcement config (sandbox-block layer is sufficient because the tool layer is gated by the orchestrator-side role-pattern checker on the hook anyway)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-12 -->

**[SUPERSEDED by cq-13 — disregard cq-12 entirely; the original question's option-1 text accidentally executed `gh auth token` and leaked a live GitHub Apps token. An OVERSEER_ALERT has been raised. Answer cq-13 below.]**

<!-- egg-hitl-decision id=cq-13 -->

**SessionStart credential bootstrap: the issue body's Reference §3 names a `SessionStart` hook that reads a GitHub token and exports `GH_TOKEN` / `GITHUB_TOKEN` to `$CLAUDE_ENV_FILE`. This is load-bearing for the credential-helper rewrite in Goal #2 — without it, the rewritten `git push` command resolves `$GITHUB_TOKEN` to empty and fails at runtime. How should the substrate satisfy this? (Replaces cq-12.)**

- [ ] Ship a SessionStart hook mirroring the Khan pattern: a Python entry script at `orchestrator/substrate/claude_code/session_start.py` that reads `~/.config/egg/secrets.env`, then falls back to the `gh` CLI auth-token surface and the GH config hosts file; writes `$CLAUDE_ENV_FILE`; `settings.template.json` registers it under `hooks.SessionStart`
- [ ] Document that `GITHUB_TOKEN` must be exported by the operator before launching Claude Code; the git-filter PreToolUse hook fail-closes (deny + actionable message) when `GITHUB_TOKEN` is unset
- [ ] Reuse the gateway-side `~/.config/egg/secrets.env` reader via a small shared extension and inject the token literal directly into the rewritten command rather than relying on env-var propagation (no SessionStart hook needed)
- [ ] Other (explain in reply)

### Open-ended feedback

<!-- egg-hitl-feedback id=feedback-1 -->

The six free-form clarification questions are registered as `feedback-1` (see the issue's feedback comment). They cover: (1) cq-5/cq-10 example-URL clarification (now redundant since cq-10 is the canonical SSH question — operator may ignore Q1), (2) `allowUnsandboxedCommands: false` + `failIfUnavailable: true` defaults, (3) macOS support / `enableWeakerNestedSandbox` need, (4) ADR §R1 coverage-claim language given the residual API-key gap (default recommendation: "mitigated; residual gap = parent session still holds the key"), (5) restricted-path-push coarse-vs-attribution-aware enforcement, (6) bare-shell git/gh fail-closed semantics when no `EGG_AGENT_ROLE` is set.

---

## Complexity Assessment

**high** — architectural change across the substrate config (`settings.template.json` + sandbox block + denyWrite + (conditionally) `SessionStart` hook), a substantial extension of the PreToolUse hook (`hook_entry.py` grows by ~400–800 lines for git/gh parsing, push-range diffing, credential rewriting — likely requiring a decomposition under `orchestrator/substrate/claude_code/_hook_entry/` to stay under the 1500-line cap), a new single-source-of-truth module (`shared/egg_restrictions/git_policy.py`) consumed by both `gateway/git_client.py` and the substrate hook, dispatch-path coordination with #2717's R15 work (and a third agent-side-enforcement workstream if `cq-8` = opt-3), and an ADR rewrite covering R1, network isolation, restricted-path enforcement, and the residual-gap honesty. The two slices recommended in Option B are independently sized at roughly "medium" each; the combined surface is "high" by the rubric's "cross-cutting concern, many independent phases that could be parallelized" definition.

---

*Authored-by: egg*
