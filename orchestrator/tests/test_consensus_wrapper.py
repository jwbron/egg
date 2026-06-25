"""Tests for the consensus wrapper module.

Issue #3164 retired the in-pod BRC event-loop "wait arm": the wrapper
rendered by ``build_consensus_wrapped_command`` (and its delegate
``build_event_pump_wrapped_command``) is now ONE-SHOT ONLY. The
orchestrator spawns the wrapper once per actionable BRC event with
``EGG_EVENT_ACTION`` injected; the wrapper handles that single event and
exits. There is no blocking ``while true`` wait-loop, no background
heartbeat emitter, no idle-budget machinery, and no agent-fail-streak
machinery in the wrapper — those were either deleted or re-homed to the
orchestrator event loop.

The test classes that pinned the deleted in-pod-pump surface (the
``while true`` loop, ``egg-orch message wait-loop``, the 30s background
heartbeat, the idle-budget overseer alert, the wait-filter construction,
the role-complete/consensus-confirmed loop arm, the agent-fail-streak
escalation, the ownership-flag accessor) went with it. What survives:
the shared helper functions (``cw_log`` / ``emit_heartbeat`` /
``fetch_next_action`` / ``sync_to_proposals`` / ``invoke_agent_for_event``),
the one-shot event handler, the per-event prompt composer wiring, the
sync-to-proposal banner contract, and the ``--effort`` thread-through.
"""

import os
import shlex
import subprocess
import sys

from consensus_wrapper import build_consensus_wrapped_command


class TestEventPumpInvokesComposer:
    """Pin the wrapper template's ``invoke_agent_for_event`` invocation
    shape (reviewer_contract NACK v1, plan TASK-3-2 acceptance "Wrapper
    template emits expected ``compose_event_prompt`` invocation").

    The wrapper composes the per-event prompt by invoking
    ``orchestrator/routes/event_prompt.py`` via the script CLI, with
    env-var contract: ``EGG_AGENT_ROLE``, ``EGG_BASE_BRANCH``,
    ``EGG_REPO_PATH``, ``EGG_BRC_MEMORY`` (all four explicitly
    re-exported on the ``python3`` invocation per the
    reviewer_holistic v2 follow-up so the script sees a deterministic
    env regardless of which vars the parent shell happens to export).
    These tests fail if a future refactor drops the function, changes
    the script path, breaks the env-var pass-through, or removes the
    ``python3 "$script_path"`` call shape.
    """

    def test_template_defines_invoke_agent_for_event(self) -> None:
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        assert "invoke_agent_for_event()" in script, (
            "Wrapper template must define `invoke_agent_for_event` so the "
            "per-event prompt composition is in place."
        )

    def test_template_references_event_prompt_script(self) -> None:
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # Either the hard-coded production path OR the override env var
        # must be present so tests can swap the script for fakes.
        assert (
            "/opt/egg-runtime/orchestrator/routes/event_prompt.py" in script
            or "EGG_EVENT_PROMPT_SCRIPT" in script
        ), (
            "Wrapper template must reference the event_prompt CLI script "
            "by path or by env-var indirection."
        )
        # The actual script-path env var name is the documented seam.
        assert "EGG_EVENT_PROMPT_SCRIPT" in script

    def test_template_re_exports_memory_env_var(self) -> None:
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The re-export line must be present so the env-var contract
        # to ``event_prompt.py::_cli`` is locked in. All four documented
        # env vars (``EGG_AGENT_ROLE`` / ``EGG_BASE_BRANCH`` /
        # ``EGG_REPO_PATH`` / ``EGG_BRC_MEMORY``) must be re-exported
        # on the python3 invocation per the reviewer_holistic v2
        # follow-up.
        assert "EGG_BRC_MEMORY=" in script
        assert "EGG_AGENT_ROLE=" in script
        assert "EGG_BASE_BRANCH=" in script
        assert "EGG_REPO_PATH=" in script

    def test_template_env_prefix_attaches_to_python3_not_printf(self) -> None:
        """The env-var prefix must attach to ``python3`` (RHS of the
        pipe), not ``printf`` (LHS). The earlier form attached only to
        ``printf`` and ``python3`` inherited from the parent shell — a
        latent bug that worked in production today only because the
        agent-pod shell already exports the vars, and would silently
        break if the parent shell didn't (reviewer_contract NACK v1
        finding #1 + reviewer_code NACK v1 finding #1).
        """
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The pipe must run printf first (LHS) without env-var prefix,
        # then env-vars decorate the python3 invocation (RHS).
        # Pin by checking the textual order of the key tokens.
        printf_idx = script.find("printf '%s' \"$event_payload\"")
        env_role_idx = script.find('EGG_AGENT_ROLE="$role"')
        python3_idx = script.find('python3 "$script_path"')
        assert printf_idx > 0
        assert env_role_idx > 0
        assert python3_idx > 0
        assert printf_idx < env_role_idx < python3_idx, (
            f"Expected order: printf '%s' "
            f"(idx={printf_idx}) | EGG_AGENT_ROLE=...="
            f"(idx={env_role_idx}) python3 (idx={python3_idx}); "
            "this confirms the env-var prefix attaches to python3 "
            "(RHS of the pipe) per the v1 NACK fix."
        )

    def test_template_invokes_python3_with_script_path_and_action(self) -> None:
        cmd = build_consensus_wrapped_command("hello")
        script = cmd[2]
        # The call shape: ``python3 "$script_path" "$action"``.
        assert 'python3 "$script_path" "$action"' in script, (
            "Wrapper template must invoke python3 with the script path "
            "and action argument. A future refactor that drops the "
            "action argv would silently break the CLI contract."
        )


class TestEffortFlag:
    """``effort`` threads into the agent command prefix as ``--effort``.

    The decision's effort (AgentModelDecision.effort — currently pinned
    only for fable-routed agents) must reach the ``python3 -m egg_agent``
    invocation; omitting it must leave the flag off entirely so every
    other model keeps inheriting Claude Code's per-model default.
    """

    def test_effort_appends_flag_to_agent_prefix(self):
        cmd = build_consensus_wrapped_command("Prompt", model="fable", effort="high")
        script = cmd[2]
        assert "--model fable --max-turns 1000 --effort high" in script

    def test_no_effort_omits_flag(self):
        cmd = build_consensus_wrapped_command("Prompt", model="opus")
        script = cmd[2]
        assert "--effort" not in script


class TestSyncToProposals:
    """Wrapper-performed sync-to-proposal on review actions (#3076 /
    #3077 clause 2).

    The designed mid-phase artifact flow used to live as fetch/merge
    prose in spawn prompts the event pump provably discards
    (``del prompt_text``, #3033) — so reviewers that must RUN a
    proposal (tester) never had the producer's commits in their
    worktree. The wrapper now performs that sync deterministically:
    before an ``ack``/``nack`` invocation it merges each pending
    producer's ``proposal_commit_sha`` into the reviewer worktree,
    fail-soft at every step. Under the one-shot handler (#3164) the
    review-action dispatch is gated on ``ONE_SHOT_ACTION``.
    """

    def _script(self) -> str:
        return build_consensus_wrapped_command("Prompt")[2]

    def test_template_defines_sync_to_proposals(self):
        script = self._script()
        assert "sync_to_proposals() {" in script

    def test_sync_runs_only_for_review_actions(self):
        """The sync call must be gated on ack/nack — a producer's own
        ``propose`` invocation must NOT merge peers' commits into its
        worktree (R11a: propose own work first, peer state irrelevant).
        Under the one-shot handler (#3164) the gate reads
        ``ONE_SHOT_ACTION``.
        """
        script = self._script()
        guard = 'if [ "$ONE_SHOT_ACTION" = "ack" ] || [ "$ONE_SHOT_ACTION" = "nack" ]; then'
        assert guard in script
        # The call rides inside that guard, with the event payload.
        guarded_block = script.split(guard, 1)[1].split("fi", 1)[0]
        assert 'sync_to_proposals "$ONE_SHOT_PAYLOAD"' in guarded_block

    def test_sync_precedes_agent_invocation(self):
        """Ordering invariant: the worktree must be synced BEFORE the
        one-shot agent runs, or the tester still reviews a stale tree.
        """
        script = self._script()
        sync_pos = script.index('sync_to_proposals "$ONE_SHOT_PAYLOAD"')
        invoke_pos = script.index('invoke_agent_for_event "$ONE_SHOT_ACTION" "$ONE_SHOT_PAYLOAD"')
        assert sync_pos < invoke_pos

    def test_sha_extraction_is_hex_validated(self):
        """The producer-supplied SHA is interpolated into git argv;
        the extractor must hex-validate (7-64 chars) so shell
        metacharacters and non-hex sentinels (RECONSTRUCTED_NO_SHA)
        never reach git.
        """
        script = self._script()
        assert "[0-9a-fA-F]{7,64}" in script
        assert "fullmatch" in script

    def test_merge_failure_is_fail_soft(self):
        """A conflicting merge must abort and continue — the per-event
        prompt's ``git show`` reads (#3078) remain the fallback; the
        agent invocation must never be blocked on the sync.
        """
        script = self._script()
        assert "merge --abort" in script
        # The function never propagates failure into the action arm.
        fn_body = script.split("sync_to_proposals() {", 1)[1]
        # Take through the function's closing `return 0`.
        assert "return 0" in fn_body.split("\n}\n", 1)[0]

    def _extract_sync_harness(self, script: str, repo: str, payload: str) -> str:
        """Build a runnable bash harness: cw_log + sync_to_proposals."""
        import re

        cw_match = re.search(r"cw_log\(\) \{.*?\n\}", script, flags=re.DOTALL)
        assert cw_match is not None
        sync_match = re.search(r"sync_to_proposals\(\) \{.*?\n\}", script, flags=re.DOTALL)
        assert sync_match is not None
        return (
            "#!/bin/bash\nset -uo pipefail\n"
            f"EGG_REPO_PATH={shlex.quote(repo)}\n"
            # #3216 gates the working-tree merge to execution-needing
            # reviewers; the merge-behavior assertions below run as the
            # tester (the role that executes the proposal). The role-skip
            # path has its own coverage in
            # test_sync_to_proposals_role_gate.py.
            "EGG_AGENT_ROLE=tester\n"
            + cw_match.group(0)
            + "\n"
            + sync_match.group(0)
            + "\nsync_to_proposals "
            + shlex.quote(payload)
            + '\necho "SYNC_RC=$?"\n'
        )

    def test_behavioral_merge_and_metachar_filter(self, tmp_path):
        """End-to-end: a real proposal SHA on a producer branch is
        merged into the reviewer's checkout (the proposed artifact
        becomes Read-able); a shell-metachar SHA is filtered before
        any git command; the function exits 0 regardless.
        """
        import json as _json

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )

        git("init", "-q", "-b", "main")
        (repo / "f.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        git("checkout", "-qb", "producer")
        (repo / "plan.md").write_text("the plan\n")
        git("add", ".")
        git("commit", "-qm", "plan draft")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-qb", "reviewer", "main")

        payload = _json.dumps(
            {
                "pending_reviews": [
                    {"producer": "architect", "proposal_commit_sha": sha},
                    {"producer": "evil", "proposal_commit_sha": "abc; rm -rf /"},
                    {"producer": "noop", "proposal_commit_sha": ""},
                ]
            }
        )
        script = self._script()
        harness = self._extract_sync_harness(script, str(repo), payload)
        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        assert "SYNC_RC=0" in result.stdout, (
            f"sync_to_proposals must exit 0; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # The proposed artifact is now a real file in the reviewer tree.
        assert (repo / "plan.md").read_text() == "the plan\n"
        # The metachar SHA was filtered, not executed/attempted.
        assert "rm -rf" not in result.stderr

    def test_behavioral_unresolvable_sha_logs_and_continues(self, tmp_path):
        """A well-formed but unknown SHA logs the git-show fallback and
        exits 0 — never fails the action arm."""
        import json as _json

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        payload = _json.dumps(
            {"pending_reviews": [{"producer": "x", "proposal_commit_sha": "a" * 40}]}
        )
        script = self._script()
        harness = self._extract_sync_harness(script, str(repo), payload)
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        assert "SYNC_RC=0" in result.stdout
        assert "unresolvable" in result.stderr


class TestSyncOutcomesAndBanner:
    """R1 non-silent sync banner (#3077 slice-1 TASK-1-3).

    The fail-soft skip points in ``sync_to_proposals()`` — unresolvable
    SHA, conflicting merge — used to log and continue, leaving a
    reviewer whose worktree silently failed to sync to trust a stale
    local diff. Slice-1 closes that silence: ``sync_to_proposals()``
    records a per-SHA outcome (one of ``merged``, ``already-ancestor``,
    ``unresolvable``, ``merge-failed``) and the wrapper prepends a
    "worktree NOT synced to <sha> (<reason>); treat your local diff as
    unreliable — use the rendered ``git log`` / ``git show`` fallback
    commands in this prompt instead." banner to the fetched event
    prompt BEFORE the agent is invoked, on any failure outcome.
    Successful sync paths leave the agent-visible prompt byte-identical.

    Sync semantics (fail-soft, exit 0, merge --abort on conflict) are
    unchanged — only reporting is new. These tests pin the new R1
    contract:

    * outcome values are observable at the wrapper level for all four
      branches (``merged``, ``already-ancestor``, ``unresolvable``,
      ``merge-failed``);
    * the agent-visible prompt contains the banner on a failure outcome
      and contains the SHA + reason verbatim;
    * the agent-visible prompt is byte-identical to the composed prompt
      on a successful outcome (no banner).
    """

    # The four outcome tokens the wrapper records at the per-SHA level.
    # Plan slice-1 acceptance (line 342): "All four outcome values
    # covered at the wrapper level."
    _OUTCOMES = ("merged", "already-ancestor", "unresolvable", "merge-failed")

    def _script(self) -> str:
        return build_consensus_wrapped_command("Prompt")[2]

    def test_template_records_all_four_outcomes(self):
        """Each of the four per-SHA outcome tokens (``merged``,
        ``already-ancestor``, ``unresolvable``, ``merge-failed``) is
        present in the wrapper template. A future refactor that drops
        an outcome — e.g. collapses ``already-ancestor`` back into the
        ``merged`` log line — would re-orphan a path the banner needs
        to distinguish.
        """
        script = self._script()
        missing = [o for o in self._OUTCOMES if o not in script]
        assert not missing, (
            "Slice-1 outcome contract requires all four per-SHA outcome "
            "tokens in the wrapper template; missing: "
            f"{missing}. Plan TASK-1-1 acceptance: "
            "merged, already-ancestor, unresolvable, merge-failed."
        )

    def test_template_emits_not_synced_banner_text(self):
        """The banner string the agent sees on a failure outcome is
        pinned to the architect's wording ("worktree NOT synced ...")
        AND wired into the ``SYNC_FAILURE_BANNERS`` accumulator. The
        substring presence alone would survive a refactor that
        downgrades the banner to a dead comment; pinning the
        ``SYNC_FAILURE_BANNERS+=`` append site catches that too.
        """
        script = self._script()
        assert "NOT synced" in script, (
            "Wrapper template must emit the 'worktree NOT synced' "
            "banner so a reviewer whose sync silently failed cannot "
            "trust a stale local diff. Plan slice-1 banner wording: "
            "'worktree NOT synced to `<sha>` (`<reason>`); treat your "
            "local diff as unreliable — use the rendered `git log` / "
            "`git show` fallback commands in this prompt instead.'"
        )
        # The banner must be appended to the per-event accumulator the
        # invoke arm consumes, not just mentioned in a comment block.
        # A `SYNC_FAILURE_BANNERS+=` line carrying the literal banner
        # text is the load-bearing append site; the accumulator is
        # what reaches ``invoke_agent_for_event``'s prompt prepend.
        import re as _re

        append_sites = _re.findall(
            r'SYNC_FAILURE_BANNERS\+="[^"]*NOT synced[^"]*"',
            script,
        )
        assert append_sites, (
            "Banner text must be appended to the ``SYNC_FAILURE_BANNERS`` "
            'accumulator via ``SYNC_FAILURE_BANNERS+="...NOT synced..."`` '
            "— that is the wire from sync_to_proposals() to the prompt "
            "prepend in invoke_agent_for_event(). A bare 'NOT synced' "
            "string anywhere else in the script is decorative."
        )
        # Both failure branches (unresolvable + merge-failed) must wire
        # in — the four-outcome contract requires both reach the agent.
        assert any("unresolvable" in s for s in append_sites), (
            "``unresolvable`` outcome must append its banner to "
            "SYNC_FAILURE_BANNERS. Found append sites: "
            f"{append_sites}"
        )
        assert any("merge-failed" in s for s in append_sites), (
            "``merge-failed`` outcome must append its banner to "
            "SYNC_FAILURE_BANNERS. Found append sites: "
            f"{append_sites}"
        )

    def test_template_references_git_show_fallback_in_banner(self):
        """The banner steers reviewers at the rendered ``git show``
        delta commands (#3078 served reads) — that is the live
        replacement channel R1 is non-silently surfacing. The
        substring ``git show`` must appear inside the banner-bearing
        region so the agent has a next step beyond "sync failed".
        """
        script = self._script()
        # The banner-bearing region runs from the start of
        # ``invoke_agent_for_event`` / ``sync_to_proposals`` (where the
        # banner is produced and consumed) to the one-shot event handler
        # section that dispatches the actions. The exact placement of the
        # prepend logic — sync function vs invoke function vs a small
        # helper — is the coder's call; we only require both regions,
        # taken together, to carry the banner-and-fallback text.
        invoke_start = script.index("invoke_agent_for_event() {")
        sync_start = script.index("sync_to_proposals() {")
        body_lo = min(invoke_start, sync_start)
        body_hi = script.index("# --- one-shot event handler (#3164)")
        body = script[body_lo:body_hi]
        assert "NOT synced" in body, (
            "Banner text must live inside the sync/invoke region — "
            "where ``$prompt`` is built or where outcomes are recorded "
            "— so a future refactor cannot hide the banner in a "
            "comment-only block far from the producing/consuming code."
        )
        assert "git show" in body, (
            "Slice-1 banner must point reviewers at the rendered "
            "``git show`` fallback (#3078 served reads) — that is "
            "their next step when the local worktree diff is "
            "unreliable. Plan slice-1 banner wording references "
            "'the `git show` commands below.'"
        )

    # ------------------------------------------------------------------
    # End-to-end harness: real bash + real git + a stubbed event_prompt
    # composer + a captured agent invocation. The four scenarios below
    # exercise each per-SHA outcome and assert presence/absence of the
    # banner in the agent-visible prompt — the slice-1 acceptance.
    # ------------------------------------------------------------------

    _STUB_PROMPT_BODY = "STUB_PROMPT_BODY_FOR_SLICE_1_TESTS"

    def _build_harness(self, script: str, repo: str, payload: str, capture: str, stub: str) -> str:
        """Compose a runnable bash harness that links the wrapper's
        ``cw_log`` / ``sync_to_proposals`` / ``invoke_agent_for_event``
        functions, stubs the event_prompt composer (``stub``), and
        captures the prompt handed to the agent into ``capture``.

        The wrapper's substituted agent prefix
        (``python3 -m egg_agent --model ... "$prompt"``) is rewritten to
        a single-line capture sink that writes ``$prompt`` to ``capture``
        verbatim so the test can byte-inspect what reached the agent.
        """
        import re as _re

        cw_match = _re.search(r"cw_log\(\) \{.*?\n\}", script, flags=_re.DOTALL)
        sync_match = _re.search(r"sync_to_proposals\(\) \{.*?\n\}", script, flags=_re.DOTALL)
        invoke_match = _re.search(r"invoke_agent_for_event\(\) \{.*?\n\}", script, flags=_re.DOTALL)
        assert cw_match is not None
        assert sync_match is not None
        assert invoke_match is not None

        # Swap the substituted ``python3 -m egg_agent ... "$prompt"``
        # call for a capture sink. The trailing ``"$prompt"`` is what
        # carries the agent-visible text; printing it byte-for-byte
        # gives the test a faithful surface for the banner-presence
        # assertions.
        invoke_body = invoke_match.group(0)
        agent_call = _re.compile(r'python3 -m egg_agent[^\n]*"\$prompt"')
        new_invoke, n_subs = agent_call.subn(
            f'printf "%s" "$prompt" > {shlex.quote(capture)}',
            invoke_body,
        )
        assert n_subs == 1, (
            'Expected exactly one ``python3 -m egg_agent ... "$prompt"`` '
            "call inside ``invoke_agent_for_event``; the test harness "
            "rewrites that call to a capture sink so the agent-visible "
            "prompt can be byte-inspected."
        )

        return (
            "#!/bin/bash\nset -uo pipefail\n"
            f"export EGG_REPO_PATH={shlex.quote(repo)}\n"
            f"export EGG_EVENT_PROMPT_SCRIPT={shlex.quote(stub)}\n"
            "export EGG_AGENT_ROLE=tester\n"
            "export EGG_BASE_BRANCH=main\n"
            "export EGG_BRC_MEMORY=off\n"
            "export EGG_PIPELINE_ID=test-pipeline\n"
            "export EGG_SLICE_ID=slice-1\n"
            + cw_match.group(0)
            + "\n"
            + sync_match.group(0)
            + "\n"
            + new_invoke
            + "\n"
            + 'sync_to_proposals "$1"\n'
            + 'invoke_agent_for_event "ack" "$1"\n'
            + 'echo "HARNESS_RC=$?"\n'
        )

    def _stub_composer(self, tmp_path) -> str:
        """A minimal stand-in for ``orchestrator/routes/event_prompt.py``
        that emits a known-good body so the test can pin banner-vs-no-
        banner byte equality without the full composer's churn.
        """
        stub = tmp_path / "event_prompt_stub.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "_ = sys.stdin.read()\n"
            f"sys.stdout.write({self._STUB_PROMPT_BODY!r})\n",
            encoding="utf-8",
        )
        os.chmod(str(stub), 0o755)  # nosec B103 — test fixture
        return str(stub)

    def _init_repo(self, tmp_path):
        """Initialise a tiny git repo with a base commit so producer /
        reviewer branches can diverge. Returns the repo path."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )

        git("init", "-q", "-b", "main")
        (repo / "f.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-qm", "base")
        return repo, git

    def _run_harness(self, tmp_path, payload, capture, stub):
        script = self._script()
        harness = self._build_harness(script, str(tmp_path / "repo"), payload, str(capture), stub)
        result = subprocess.run(
            ["bash", "-c", harness, "harness", payload],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        # Sync function and invoke function must each exit zero; the
        # slice-1 banner mechanism rides on that invariant (banner is a
        # prompt prefix, not a hard failure).
        assert "HARNESS_RC=0" in result.stdout, (
            "Harness must exit 0; the slice-1 banner is a prompt "
            "prefix, not a hard failure. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return result

    def test_behavioral_merged_outcome_no_banner(self, tmp_path):
        """Successful merge → ``merged`` outcome recorded, agent-visible
        prompt is byte-identical to the composed prompt (no banner).

        Plan TASK-1-1 acceptance: "``merged`` and ``already-ancestor``
        outcomes produce no banner and a byte-identical prompt."
        """
        import json as _json

        repo, git = self._init_repo(tmp_path)
        # Producer branch with a real commit that merges cleanly into the
        # reviewer's worktree on main (no conflict).
        git("checkout", "-qb", "producer")
        (repo / "plan.md").write_text("the plan\n")
        git("add", ".")
        git("commit", "-qm", "plan draft")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-q", "main")

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, payload, capture, stub)

        assert "merged" in result.stderr, (
            f"``merged`` outcome must be observable at the wrapper level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert prompt == self._STUB_PROMPT_BODY, (
            "Successful merge MUST leave the agent-visible prompt "
            "byte-identical to the composer's output — no banner, no "
            f"prefix. Got: {prompt!r}"
        )

    def test_behavioral_already_ancestor_outcome_no_banner(self, tmp_path):
        """SHA already in HEAD ancestry → ``already-ancestor`` outcome,
        agent-visible prompt unchanged."""
        import json as _json

        repo, git = self._init_repo(tmp_path)
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, payload, capture, stub)

        assert "already-ancestor" in result.stderr, (
            "``already-ancestor`` outcome must be observable at the "
            f"wrapper level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert prompt == self._STUB_PROMPT_BODY, (
            "Already-ancestor sync MUST leave the agent-visible prompt "
            f"byte-identical. Got: {prompt!r}"
        )

    def test_behavioral_unresolvable_outcome_emits_banner(self, tmp_path):
        """Well-formed but unknown SHA → ``unresolvable`` outcome AND
        the "NOT synced" banner reaches the agent prompt.

        Plan TASK-1-1 acceptance: "An unresolvable SHA yields
        ``unresolvable`` + banner." Plan TASK-1-3 acceptance: "All four
        outcome values covered at the wrapper level."
        """
        import json as _json

        repo, _git = self._init_repo(tmp_path)
        unresolvable_sha = "a" * 40
        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": unresolvable_sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, payload, capture, stub)

        assert "unresolvable" in result.stderr, (
            "``unresolvable`` outcome must be observable at the "
            f"wrapper level. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        # Banner contract: SHA + reason word + key phrase.
        assert "NOT synced" in prompt, (
            "Banner must be prepended to the agent-visible prompt on "
            "an ``unresolvable`` outcome — that is the slice-1 R1 "
            f"closing-the-silence behaviour. Prompt: {prompt!r}"
        )
        assert unresolvable_sha in prompt, (
            f"Banner must carry the failed SHA so the agent can correlate. Prompt: {prompt!r}"
        )
        assert "unresolvable" in prompt, (
            "Banner must name the reason (``unresolvable``) so the "
            "agent knows why their worktree is untrustworthy. "
            f"Prompt: {prompt!r}"
        )
        # Composed body still reaches the agent — the banner is a
        # prefix, not a replacement.
        assert self._STUB_PROMPT_BODY in prompt, (
            "Composed prompt body must still reach the agent after the "
            "banner is prepended; the banner is a prefix, not a "
            f"substitution. Prompt: {prompt!r}"
        )

    def test_behavioral_merge_failed_outcome_emits_banner(self, tmp_path):
        """Conflicting merge → ``merge-failed`` outcome AND the
        "NOT synced" banner reaches the agent prompt.

        Plan TASK-1-1 acceptance: "A conflicting merge yields a
        ``merge-failed`` outcome and the banner (with SHA and reason)
        in the prompt handed to the agent." Plan TASK-1-3 acceptance
        (the R1 acceptance from refine): "Simulated conflicting merge
        ⇒ banner present in the agent-visible prompt."
        """
        import json as _json

        repo, git = self._init_repo(tmp_path)
        # Reviewer side mutates f.txt on main first; producer mutates
        # the same line on their branch. The merge into the (mutated)
        # main HEAD then conflicts.
        (repo / "f.txt").write_text("reviewer-side\n")
        git("add", ".")
        git("commit", "-qm", "reviewer mutation")
        git("checkout", "-qb", "producer", "HEAD~1")
        (repo / "f.txt").write_text("producer-side\n")
        git("add", ".")
        git("commit", "-qm", "producer mutation")
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git("checkout", "-q", "main")

        payload = _json.dumps(
            {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": sha}]}
        )
        capture = tmp_path / "agent_prompt.txt"
        stub = self._stub_composer(tmp_path)
        result = self._run_harness(tmp_path, payload, capture, stub)

        assert "merge-failed" in result.stderr, (
            "``merge-failed`` outcome must be observable at the "
            f"wrapper level on a conflicting merge. stderr={result.stderr!r}"
        )
        prompt = capture.read_text(encoding="utf-8")
        assert "NOT synced" in prompt, (
            "Banner must be prepended on a ``merge-failed`` outcome — "
            "the slice-1 R1 acceptance from refine. "
            f"Prompt: {prompt!r}"
        )
        assert sha in prompt, f"Banner must carry the failed SHA. Prompt: {prompt!r}"
        assert "merge-failed" in prompt, (
            "Banner must name the reason (``merge-failed``) so the "
            "agent can distinguish conflict from unresolvable-SHA. "
            f"Prompt: {prompt!r}"
        )
        assert self._STUB_PROMPT_BODY in prompt, (
            f"Composed prompt body must still reach the agent. Prompt: {prompt!r}"
        )


# ===========================================================================
# One-shot wrapper (#3164): the in-pod BRC event-loop wait arm was retired.
# ``build_consensus_wrapped_command`` now renders a single rendering — a
# one-shot event handler driven by the injected ``EGG_EVENT_ACTION`` — with
# no blocking wait-loop, no background heartbeat, and no idle-budget /
# fail-streak machinery (those re-homed to the orchestrator event loop).
# The rendering is IDENTICAL regardless of any env var: there is no
# ownership flag and no ``EGG_EVENT_LOOP_OWNER`` mode.
# ===========================================================================

_GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
_GOLDEN_PATH = os.path.join(_GOLDEN_DIR, "event_pump_wrapper.sh.golden")


def _read_golden() -> str:
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestWrapperGoldenSnapshot:
    """Byte-for-byte guard on the one-shot wrapper rendering (#3164).

    A golden-file snapshot fails on ANY drift of the rendered wrapper so
    a change to the one-shot path can never regress the production path
    silently.

    The fixture deliberately carries the ``.sh.golden`` suffix (not
    ``.sh``) so ``make lint-shell`` does not shellcheck it: the rendered
    wrapper begins with a leading blank line before ``#!/bin/bash`` (the
    template's ``r\"\"\"`` opener), which is inert at runtime — the
    wrapper runs as ``bash -c \"$script\"`` — but trips SC1128 as a
    standalone file. Keeping the suffix lets the golden hold the exact
    bytes (leading newline and all).

    To regenerate after an INTENTIONAL change, run from the repo root::

        PYTHONPATH=orchestrator:shared .venv/bin/python -c \\
          "import consensus_wrapper as c; \\
open('orchestrator/tests/golden/event_pump_wrapper.sh.golden','w')\\
.write(c.build_consensus_wrapped_command('x')[2])"

    and review the diff — an unreviewed regeneration defeats the guard.
    """

    def test_wrapper_matches_golden_byte_for_byte(self):
        golden = _read_golden()
        cmd = build_consensus_wrapped_command("Prompt")
        assert cmd[:2] == ["bash", "-c"]
        assert cmd[2] == golden, (
            "wrapper rendering drifted from the golden snapshot. If the "
            "change to the wrapper was intentional, regenerate "
            "tests/golden/event_pump_wrapper.sh.golden (see this class's "
            "docstring) and review the diff; otherwise this is the "
            "#3164 guard catching an unintended drift of the one-shot "
            "wrapper rendering."
        )

    def test_wrapper_is_prompt_independent(self):
        """The wrapper emits its own per-event prompts, so the initial
        prompt text never reaches the bash — the golden must hold for any
        prompt argument."""
        golden = _read_golden()
        assert build_consensus_wrapped_command("totally different prompt")[2] == golden

    def test_wrapper_is_env_independent(self, monkeypatch):
        """There is no ownership flag after #3164: any value of the
        retired ``EGG_EVENT_LOOP_OWNER`` / ``EGG_BRC_EVENT_PUMP`` env vars
        must produce the identical rendering."""
        golden = _read_golden()
        for var in ("EGG_EVENT_LOOP_OWNER", "EGG_BRC_EVENT_PUMP"):
            for val in ("pod", "orchestrator", "true", "false", "0"):
                monkeypatch.setenv(var, val)
                assert build_consensus_wrapped_command("Prompt")[2] == golden, (
                    f"{var}={val!r} must not change the wrapper rendering "
                    "(no ownership flag after #3164)."
                )
            monkeypatch.delenv(var, raising=False)

    def test_golden_snapshot_is_the_one_shot_wrapper(self):
        """Sanity-check the committed golden really is the one-shot
        wrapper (#3164), not the retired in-pod event-pump, so a
        stale/empty golden can't make the byte-equality test vacuous."""
        golden = _read_golden()
        # New one-shot markers.
        assert "one-shot event handler (#3164)" in golden
        assert 'ONE_SHOT_ACTION="${EGG_EVENT_ACTION:-}"' in golden
        # Retired in-pod-pump markers MUST be gone.
        assert "while true; do" not in golden
        assert "egg-orch message wait-loop" not in golden
        assert "start_background_heartbeat" not in golden
        assert "# --- main event-pump loop ---" not in golden


class TestOneShotArmStructure:
    """Structural pins on the one-shot wrapper (#3164). The wrapper is
    one-shot only: it reads the injected ``EGG_EVENT_ACTION``, re-derives
    next-action once as a stale-event backstop, optionally syncs, invokes
    the agent once, and exits — no blocking loop, no background heartbeat.
    """

    def _script(self) -> str:
        cmd = build_consensus_wrapped_command("Prompt")
        assert cmd[:2] == ["bash", "-c"]
        return cmd[2]

    def test_has_no_main_event_pump_loop_marker(self):
        """The retired in-pod pump's ``# --- main event-pump loop ---``
        marker and its ``while true`` loop must be gone."""
        script = self._script()
        assert "# --- main event-pump loop ---" not in script
        assert "while true; do" not in script

    def test_does_not_block_on_wait_loop(self):
        """The wrapper never blocks on the bus: there is no
        ``wait_for_event`` / ``egg-orch message wait-loop`` call. The
        stale-event backstop is a single ``brc next-action`` re-check."""
        script = self._script()
        assert "wait_for_event" not in script, (
            "one-shot wrapper must not call wait_for_event (blocking bus poll)."
        )
        assert "egg-orch message wait-loop" not in script, (
            "one-shot wrapper must not block on the message bus."
        )

    def test_starts_no_background_heartbeat(self):
        """A one-shot pod is short-lived, so the wrapper must not start a
        background heartbeat emitter (a single foreground
        ``emit_heartbeat`` ping is fine and expected)."""
        script = self._script()
        assert "start_background_heartbeat" not in script, (
            "one-shot wrapper must not spawn the background heartbeat emitter."
        )
        # A single foreground ping is still expected.
        assert 'emit_heartbeat "WORKING"' in script

    def test_reads_injected_event_action(self):
        script = self._script()
        assert 'ONE_SHOT_ACTION="${EGG_EVENT_ACTION:-}"' in script, (
            "one-shot wrapper is driven by the injected EGG_EVENT_ACTION env."
        )

    def test_has_backstop_exit_paths(self):
        """The stale-event backstop exit codes are pinned:
        - exit 64 (EX_USAGE) on a missing / terminal / unknown action;
        - exit 75 (EX_TEMPFAIL) on an inconclusive next-action re-check;
        - exit 0 on a confirmed-stale event (no agent invocation)."""
        script = self._script()
        assert "exit 64" in script
        assert "exit 75" in script
        assert "exit 0" in script

    def test_rechecks_next_action_once(self):
        script = self._script()
        assert "fetch_next_action" in script, (
            "one-shot wrapper must re-derive next-action once as the stale/dedupe backstop."
        )

    def test_retains_agent_invocation_path(self):
        """The one-shot wrapper reuses the existing
        ``invoke_agent_for_event`` composer path and ``sync_to_proposals``
        rather than inventing a parallel invocation."""
        script = self._script()
        assert "invoke_agent_for_event" in script
        assert "sync_to_proposals" in script


class TestOneShotArmBehavior:
    """Behavioural tests of the one-shot wrapper, driving the rendered
    bash against PATH stubs (#3164). Covers the one-shot behaviors:
    confirmed-stale exit 0, inconclusive-recheck exit 75 (EX_TEMPFAIL),
    exactly-one-invocation, agent exit-code passthrough, and loud
    rejection of injected confirm/complete.
    """

    def _render(self) -> list[str]:
        return build_consensus_wrapped_command("Prompt")

    @staticmethod
    def _stub_bin(tmp_path, next_action_json: str, agent_exit: int = 0, next_action_rc: int = 0):
        """Lay down PATH stubs:
          - ``egg-orch`` — logs every call; ``brc get-state`` → incomplete,
            ``brc next-action`` → the supplied JSON exiting ``next_action_rc``
            (default 0; a non-zero value simulates a 409/5xx/transport blip,
            which ``fetch_next_action`` collapses to its ``wait`` fallback);
            everything else 0.
          - ``python3`` — forwards inline ``-c``/``-`` JSON parsing to the
            real interpreter; the ``-m egg_agent`` invocation records one
            line per call and exits ``agent_exit``.
        Returns ``(bin_dir, general_log, agent_log)``.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        general_log = tmp_path / "egg_orch.log"
        agent_log = tmp_path / "agent_invocations.log"

        mock_orch = bin_dir / "egg-orch"
        mock_orch.write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> {shlex.quote(str(general_log))}\n'
            'sub="$1 $2"\n'
            'case "$sub" in\n'
            '    "brc get-state")\n'
            '        echo \'{"consensus":{"agents":{},"is_complete":false}}\'\n'
            "        ;;\n"
            '    "brc next-action")\n'
            f"        printf '%s' {shlex.quote(next_action_json)}\n"
            f"        exit {int(next_action_rc)}\n"
            "        ;;\n"
            "    *)\n"
            "        ;;\n"
            "esac\n"
            "exit 0\n"
        )
        os.chmod(str(mock_orch), 0o755)  # nosec B103

        real_python = sys.executable
        mock_python = bin_dir / "python3"
        mock_python.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-c" ] || [ "$1" = "-" ]; then\n'
            f'    exec {shlex.quote(real_python)} "$@"\n'
            "fi\n"
            'if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n'
            f'    echo "invoke" >> {shlex.quote(str(agent_log))}\n'
            f"    exit {int(agent_exit)}\n"
            "fi\n"
            # The compose_event_prompt path (``python3 <script> <action>``)
            # never runs in tests (the default /opt path is unreadable, so
            # invoke_agent_for_event uses its fallback prompt); forward it
            # to the real interpreter defensively just in case.
            f'exec {shlex.quote(real_python)} "$@"\n'
        )
        os.chmod(str(mock_python), 0o755)  # nosec B103
        return bin_dir, general_log, agent_log

    @staticmethod
    def _env(bin_dir, action: str, dedupe: str = "evt-1"):
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["EGG_AGENT_ROLE"] = "tester"
        env["EGG_PIPELINE_ID"] = "issue-3164"
        env["EGG_SLICE_ID"] = "slice-1"
        env["EGG_EVENT_ACTION"] = action
        env["EGG_EVENT_DEDUPE_KEY"] = dedupe
        # Force the compose_event_prompt fallback (unreadable default path)
        # so the agent invocation is the only ``python3 -m`` call.
        env.pop("EGG_EVENT_PROMPT_SCRIPT", None)
        return env

    @staticmethod
    def _agent_invocation_count(agent_log) -> int:
        if not agent_log.exists():
            return 0
        return len([ln for ln in agent_log.read_text().splitlines() if ln.strip()])

    def test_stale_event_exits_zero_without_invoking_agent(self, tmp_path):
        """The orchestrator injected a ``propose`` event, but by the time
        the pod starts the live next-action has moved to ``wait`` (another
        pod already handled it). The one-shot wrapper must re-check, detect
        the stale event, and exit 0 WITHOUT invoking the agent — the dedupe
        backstop."""
        cmd = self._render()
        bin_dir, general_log, agent_log = self._stub_bin(
            tmp_path, '{"action":"wait"}', agent_exit=0
        )
        env = self._env(bin_dir, "propose")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, "stale one-shot event must exit 0. stderr:\n" + result.stderr
        assert self._agent_invocation_count(agent_log) == 0, (
            "stale one-shot event must NOT invoke the agent (dedupe "
            "backstop). egg-orch log:\n"
            + (general_log.read_text() if general_log.exists() else "(empty)")
        )

    def test_inconclusive_recheck_exits_ex_tempfail_without_invoking(self, tmp_path):
        """A transient blip at re-check time (``egg-orch brc next-action``
        returns non-zero — 409 / 5xx / transport) makes ``fetch_next_action``
        fall back to ``{"action":"wait"}``. The wrapper must NOT report a
        clean handoff (exit 0) — that would let a transient failure silently
        drop a live event — and must NOT invoke the agent. Instead it exits
        75 (EX_TEMPFAIL) so the orchestrator supervisor re-derives next-action
        itself rather than treating the event as definitively handled."""
        cmd = self._render()
        bin_dir, general_log, agent_log = self._stub_bin(
            tmp_path, '{"action":"propose"}', agent_exit=0, next_action_rc=22
        )
        env = self._env(bin_dir, "propose")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 75, (
            "an inconclusive re-check (non-zero brc next-action rc) must exit "
            f"75/EX_TEMPFAIL, not 0 (clean handoff) or the agent rc; got "
            f"{result.returncode}. stderr:\n" + result.stderr
        )
        assert self._agent_invocation_count(agent_log) == 0, (
            "an inconclusive re-check must NOT invoke the agent. egg-orch log:\n"
            + (general_log.read_text() if general_log.exists() else "(empty)")
        )

    def test_fresh_event_invokes_agent_exactly_once(self, tmp_path):
        """A fresh ``propose`` (live next-action still ``propose``) invokes
        the agent exactly once, then exits — no loop, no second
        invocation."""
        cmd = self._render()
        bin_dir, general_log, agent_log = self._stub_bin(
            tmp_path, '{"action":"propose"}', agent_exit=0
        )
        env = self._env(bin_dir, "propose")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (
            "fresh one-shot event with a clean agent exit must exit 0. stderr:\n" + result.stderr
        )
        count = self._agent_invocation_count(agent_log)
        assert count == 1, (
            "fresh one-shot event must invoke the agent EXACTLY once; got "
            f"{count}. egg-orch log:\n"
            + (general_log.read_text() if general_log.exists() else "(empty)")
        )

    def test_agent_exit_code_is_passed_through(self, tmp_path):
        """#2908 exit-code classification passthrough: a non-zero agent
        exit (17 — not a signal code, so not reclassified) propagates to
        the wrapper's exit code rather than being swallowed, so the
        orchestrator can supervise the failure."""
        cmd = self._render()
        bin_dir, general_log, agent_log = self._stub_bin(
            tmp_path, '{"action":"propose"}', agent_exit=17
        )
        env = self._env(bin_dir, "propose")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert self._agent_invocation_count(agent_log) == 1, (
            "agent should have been invoked once before the failure."
        )
        assert result.returncode == 17, (
            "one-shot wrapper must pass the agent's exit code through (not "
            f"swallow it to 0/1); got {result.returncode}. stderr:\n" + result.stderr
        )

    def test_injected_confirm_is_rejected_loudly(self, tmp_path):
        self._assert_terminal_action_rejected(tmp_path, "confirm")

    def test_injected_complete_is_rejected_loudly(self, tmp_path):
        self._assert_terminal_action_rejected(tmp_path, "complete")

    def _assert_terminal_action_rejected(self, tmp_path, action: str):
        """``confirm``/``complete`` are executed orchestrator-side
        (agent-free); a pod is never spawned for them. If one is injected
        anyway, the one-shot wrapper must reject it loudly: non-zero exit,
        an error on stderr, no agent invocation, and no ``consensus
        confirmed`` call."""
        cmd = self._render()
        bin_dir, general_log, agent_log = self._stub_bin(
            tmp_path, f'{{"action":"{action}"}}', agent_exit=0
        )
        env = self._env(bin_dir, action)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0, (
            f"one-shot wrapper must reject an injected {action!r} action with a "
            f"non-zero exit. stderr:\n{result.stderr}"
        )
        assert self._agent_invocation_count(agent_log) == 0, (
            f"one-shot wrapper must NOT invoke the agent for {action!r}."
        )
        log = general_log.read_text() if general_log.exists() else ""
        assert "consensus confirmed" not in log, (
            f"one-shot wrapper must NOT run 'consensus confirmed' for {action!r}; "
            f"confirm/complete are orchestrator-side. egg-orch log:\n{log}"
        )
        assert action in result.stderr, (
            f"rejection of {action!r} must be LOUD — an error naming the "
            f"offending action on stderr. stderr:\n{result.stderr}"
        )
