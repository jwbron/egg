"""Unit tests for the read-only shared-evidence gatherer (#3523 §5, S7).

Covers the hard rules from the issue and the task-7-1 acceptance:

- the pack is **byte-identical across a simulated wave** (order-independent
  build + deterministic render);
- the pack **carries no editorializing fields** (evidence, never conclusions);
- the pack is **mechanically path-ordered**;
- the staged flag resolves off/log/on with unknown => off;
- the independence guardrail keeps the tester / finding-verifier cold-start;
- the gatherer exposes **no verdict / post / GitHub surface** (module surface +
  the SYSTEM contract role + gateway deny-by-default gh restrictions);
- **log-mode parity + cost recording**: ``off`` / ``log`` are byte-identical to a
  cold-start reviewer prompt, while the log record captures the measured wave
  cache-hit rate and per-wave cost;
- **Delphi redaction is unaffected**: the pack is repo-facts-only, so there is no
  producer self-assessment in it for redaction to act on.
"""

from __future__ import annotations

import subprocess
from dataclasses import fields

import evidence_gatherer
import pytest
from consensus_wrapper import aggregate_wave_cache_stats, evidence_prefix_log_record
from egg_contracts.agent_roles import (
    REVIEWER_CHECKOUT_ROLE_VALUES,
    AgentCategory,
    AgentRole,
    Role,
    get_contract_role,
    get_role_definition,
)
from evidence_gatherer import (
    EVIDENCE_PACK_SCHEMA_VERSION,
    ChangedFileEvidence,
    EvidencePack,
    SymbolEvidence,
    _editorializing_field_names,
    assert_pack_carries_no_conclusions,
    build_pack,
    evidence_prefix_mode,
    gather_evidence,
    render_pack,
    shares_evidence_prefix,
)
from routes.pipelines._criteria import (
    apply_shared_evidence_prefix,
    build_shared_evidence_prefix,
)

# ---------------------------------------------------------------------------
# Staged flag
# ---------------------------------------------------------------------------


class TestEvidencePrefixMode:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("EGG_REVIEW_EVIDENCE_PREFIX", raising=False)
        assert evidence_prefix_mode() == "off"

    @pytest.mark.parametrize("raw", ["on", "1", "true", "YES", "  On  "])
    def test_on_values(self, monkeypatch, raw):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", raw)
        assert evidence_prefix_mode() == "on"

    @pytest.mark.parametrize("raw", ["log", "log-only", "LOG_ONLY"])
    def test_log_values(self, monkeypatch, raw):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", raw)
        assert evidence_prefix_mode() == "log"

    def test_unknown_resolves_off(self, monkeypatch):
        """A typo must degrade to off, never silently activate the prefix."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "maybe")
        assert evidence_prefix_mode() == "off"


# ---------------------------------------------------------------------------
# Independence guardrail
# ---------------------------------------------------------------------------


class TestSharesEvidencePrefix:
    @pytest.mark.parametrize(
        "role",
        [
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "reviewer_security",
            "reviewer_concurrency",
        ],
    )
    def test_specialist_lenses_share(self, role):
        assert shares_evidence_prefix(role) is True

    @pytest.mark.parametrize(
        "role", ["tester", "finding_verifier", "coder", "documenter", "", None]
    )
    def test_cold_start_and_producers_excluded(self, role):
        """Tester + finding-verifier stay cold-start; producers never share."""
        assert shares_evidence_prefix(role) is False

    def test_case_insensitive(self):
        assert shares_evidence_prefix("Reviewer_Code") is True


# ---------------------------------------------------------------------------
# Evidence, never conclusions
# ---------------------------------------------------------------------------


class TestNoConclusions:
    def test_schema_has_no_editorializing_fields(self):
        assert _editorializing_field_names() == []

    def test_assert_helper_passes_for_shipped_schema(self):
        # Should not raise for the shipped schema.
        assert_pack_carries_no_conclusions()


# ---------------------------------------------------------------------------
# Deterministic, path-ordered, byte-identical build/render
# ---------------------------------------------------------------------------


def _sample_inputs():
    files = [
        ChangedFileEvidence(path="orchestrator/z_last.py", content="z = 1\n"),
        ChangedFileEvidence(path="orchestrator/a_first.py", content="def a():\n    return 1\n"),
    ]
    symbols = [
        SymbolEvidence(
            symbol="foo", defined_in="orchestrator/a_first.py", callers=("z.py:9", "b.py:2")
        ),
        SymbolEvidence(symbol="bar", defined_in="orchestrator/z_last.py", callers=("c.py:1",)),
    ]
    env = {"python_version": "3.12.0", "platform": "Linux"}
    return files, symbols, env


class TestBuildAndRender:
    def test_path_ordered_files_and_symbols(self):
        files, symbols, env = _sample_inputs()
        pack = build_pack(diff="d", files=files, symbols=symbols, environment=env)
        assert [f.path for f in pack.files] == [
            "orchestrator/a_first.py",
            "orchestrator/z_last.py",
        ]
        # symbols sorted by (name, defined_in)
        assert [s.symbol for s in pack.symbols] == ["bar", "foo"]
        # callers sorted within a symbol
        foo = next(s for s in pack.symbols if s.symbol == "foo")
        assert foo.callers == ("b.py:2", "z.py:9")
        # environment sorted by key
        assert pack.environment == (("platform", "Linux"), ("python_version", "3.12.0"))
        assert pack.schema_version == EVIDENCE_PACK_SCHEMA_VERSION

    def test_build_is_order_independent(self):
        """Same raw material in any input order builds an equal pack."""
        files, symbols, env = _sample_inputs()
        pack_a = build_pack(diff="d", files=files, symbols=symbols, environment=env)
        pack_b = build_pack(
            diff="d",
            files=list(reversed(files)),
            symbols=list(reversed(symbols)),
            environment=dict(reversed(list(env.items()))),
        )
        assert pack_a == pack_b

    def test_render_is_byte_identical_across_a_wave(self):
        """Every same-model reviewer in the wave shares one byte-identical prefix."""
        files, symbols, env = _sample_inputs()
        pack = build_pack(diff="some diff", files=files, symbols=symbols, environment=env)
        # Simulate 5 reviewers each rendering the SAME shared pack.
        renders = [render_pack(pack) for _ in range(5)]
        assert len(set(renders)) == 1
        # And two independently-built packs from the same material render equal.
        pack2 = build_pack(
            diff="some diff",
            files=list(reversed(files)),
            symbols=symbols,
            environment=env,
        )
        assert render_pack(pack) == render_pack(pack2)

    def test_render_has_no_conclusion_language_headers(self):
        files, symbols, env = _sample_inputs()
        pack = build_pack(diff="d", files=files, symbols=symbols, environment=env)
        rendered = render_pack(pack).lower()
        # The pack self-describes as evidence-only; no analysis/priority sections.
        assert "no analysis" in rendered
        for banned in ("areas of concern", "recommendation", "priority ranking", "severity"):
            assert banned not in rendered

    def test_dedup_files_by_path(self):
        files = [
            ChangedFileEvidence(path="a.py", content="first"),
            ChangedFileEvidence(path="./a.py", content="dup"),
        ]
        pack = build_pack(diff="", files=files, symbols=[], environment={})
        assert len(pack.files) == 1
        assert pack.files[0].content == "first"

    def test_diff_truncation_sets_flag_and_sentinel(self):
        big = "x" * (300 * 1024)
        pack = build_pack(diff=big, files=[], symbols=[], environment={})
        assert pack.diff_truncated is True
        assert "truncated by evidence_gatherer" in pack.diff


# ---------------------------------------------------------------------------
# Read-only I/O gathering (with an injected git runner)
# ---------------------------------------------------------------------------


class _FakeGit:
    """Minimal fake for subprocess.run over the git calls the gatherer makes."""

    def __init__(self, diff="", grep=""):
        self.diff = diff
        self.grep = grep
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        # args like ["git", "diff", ...] or ["git", "grep", ...]
        sub = args[1] if len(args) > 1 else ""
        if sub == "diff":
            return subprocess.CompletedProcess(args, 0, stdout=self.diff, stderr="")
        if sub == "grep":
            rc = 0 if self.grep else 1
            return subprocess.CompletedProcess(args, rc, stdout=self.grep, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestGatherEvidence:
    def test_gathers_diff_files_symbols(self, tmp_path):
        (tmp_path / "mod.py").write_text(
            "def hello():\n    return helper()\n\ndef helper():\n    return 1\n"
        )
        diff = (
            "diff --git a/mod.py b/mod.py\n@@ -0,0 +1,2 @@\n+def hello():\n+    return helper()\n"
        )
        fake = _FakeGit(diff=diff, grep="other.py:3:    hello()\nmod.py:1:def hello():\n")
        pack = gather_evidence(
            ["mod.py"],
            tmp_path,
            environment={"python_version": "3.12.0"},
            runner=fake,
        )
        assert pack.diff == diff
        assert [f.path for f in pack.files] == ["mod.py"]
        # 'hello' changed symbol resolved with a caller from another file only.
        hello = next((s for s in pack.symbols if s.symbol == "hello"), None)
        assert hello is not None
        assert hello.defined_in == "mod.py"
        assert "other.py:3" in hello.callers
        # its own definition file's def line is excluded from callers
        assert "mod.py:1" not in hello.callers
        # callee resolved within changed set (hello calls helper, both defined)
        assert "helper" in hello.callees

    def test_missing_file_degrades_not_raises(self, tmp_path):
        fake = _FakeGit(diff="", grep="")
        # File does not exist on disk (deleted-by-diff); must not raise.
        pack = gather_evidence(["gone.py"], tmp_path, environment={}, runner=fake)
        assert pack.files == ()
        assert isinstance(pack, EvidencePack)

    def test_git_failure_degrades_to_empty_diff(self, tmp_path):
        def boom(args, **kwargs):
            raise OSError("git not found")

        pack = gather_evidence(["x.py"], tmp_path, environment={}, runner=boom)
        assert pack.diff == ""
        assert isinstance(pack, EvidencePack)


# ---------------------------------------------------------------------------
# No verdict / post / GitHub surface (task-7-3 acceptance)
# ---------------------------------------------------------------------------
#
# The gatherer is *unprivileged by construction*: it assembles evidence and
# nothing else. This is guaranteed on two planes — the module exposes no verdict
# / post / write callable, and the paired EVIDENCE_GATHERER role is structurally
# denied verdict-casting, contract writes, and EVERY gh operation.


class TestNoVerdictPostOrGitHubSurface:
    def test_module_exposes_no_verdict_or_post_callable(self):
        """The public module surface is read-only assembly — no verdict/post/write."""
        # Token-equality (not substring) so 'pack' is not mistaken for 'ack'.
        banned = {
            "verdict",
            "ack",
            "nack",
            "post",
            "comment",
            "review",
            "approve",
            "merge",
            "push",
            "gh",
        }
        offenders = [
            name
            for name in dir(evidence_gatherer)
            if not name.startswith("_")
            and callable(getattr(evidence_gatherer, name))
            and banned & set(name.lower().split("_"))
        ]
        assert offenders == [], f"gatherer must expose no verdict/post surface: {offenders}"

    def test_role_maps_to_system_not_reviewer(self):
        """SYSTEM contract role: an observer, structurally unable to cast a verdict."""
        assert get_contract_role(AgentRole.EVIDENCE_GATHERER) is Role.SYSTEM
        assert get_contract_role(AgentRole.EVIDENCE_GATHERER) is not Role.REVIEWER

    def test_role_is_not_a_checkout_reviewer(self):
        assert AgentRole.EVIDENCE_GATHERER not in REVIEWER_CHECKOUT_ROLE_VALUES

    def test_role_is_utility_category(self):
        assert get_role_definition(AgentRole.EVIDENCE_GATHERER).category is AgentCategory.UTILITY

    @pytest.mark.parametrize(
        "gh_command",
        [
            "pr create --title x",
            "pr review --approve 1",
            "pr merge 1",
            "issue comment 1 --body x",
            "issue edit 1 --body x",
            "pr comment 1 --body x",
        ],
    )
    def test_every_gh_operation_denied_by_default(self, gh_command):
        """Absent from AGENT_GH_RESTRICTIONS => deny-by-default rejects EVERY gh op.

        The definitive gh-deny-by-default coverage lives in its correctly-scoped
        home, gateway/tests/test_agent_restrictions_gh.py (gateway/ is on the test
        PYTHONPATH there). This orchestrator-side mirror imports the gateway module
        function-locally so it (a) never breaks module collection and (b) runs +
        passes under the canonical ``make test`` runner (PYTHONPATH=shared:gateway:
        orchestrator); in a bare isolated ``pytest`` run where gateway/ is off the
        path it skips cleanly rather than erroring — a top-level import here would
        abort collection of the entire file (the S7 F-code-1 / SEC-T1 regression).
        """
        agent_restrictions = pytest.importorskip("agent_restrictions")
        allowed, _reason = agent_restrictions.check_agent_gh_operation(
            "evidence_gatherer", gh_command
        )
        assert allowed is False

    def test_file_access_writes_only_handoff_dir(self):
        """Read-all, write-only-handoff: no source/tests/contracts/reviews writes."""
        fa = get_role_definition(AgentRole.EVIDENCE_GATHERER).file_access
        assert fa.allowed_write == [".egg-state/agent-outputs/"]
        for blocked in (".egg-state/contracts/", ".egg-state/reviews/", "tests/", ".github/"):
            assert blocked in fa.blocked_write


# ---------------------------------------------------------------------------
# Log-mode parity + cost recording (task-7-3 acceptance)
# ---------------------------------------------------------------------------
#
# Item 5's whole bet is cost, and "measure the cache-hit rate + per-wave cost in
# log mode before enabling" is an explicit acceptance criterion. The log record
# must capture those numbers while changing NOTHING about prompt assembly —
# ``off`` and ``log`` must be byte-identical to a true cold-start; only ``on``
# prepends the shared prefix.

_LENS_TAIL = "**CODE REVIEW** lens instruction tail (per-lens, distinct)."


def _wave_pack():
    return build_pack(
        diff="diff --git a/x b/x\n@@\n+changed\n",
        files=[ChangedFileEvidence(path="x.py", content="def x():\n    return 1\n")],
        symbols=[],
        environment={"python_version": "3.12.0"},
    )


class TestLogModeParityAndCostRecording:
    def test_log_mode_is_byte_identical_to_cold_start(self, monkeypatch):
        """log mode changes NO prompt assembly — parity vs the cold-start lens."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        cold_start = _LENS_TAIL  # what the reviewer gets today, with no prefix
        assembled = apply_shared_evidence_prefix(
            _LENS_TAIL, reviewer_role="reviewer_code", evidence_pack=_wave_pack()
        )
        assert assembled == cold_start

    def test_off_mode_is_byte_identical_to_cold_start(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "off")
        assembled = apply_shared_evidence_prefix(
            _LENS_TAIL, reviewer_role="reviewer_code", evidence_pack=_wave_pack()
        )
        assert assembled == _LENS_TAIL

    def test_on_mode_prepends_prefix_proving_log_was_parity(self, monkeypatch):
        """Contrast: only ``on`` actually changes assembly, so log-mode parity is real."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        pack = _wave_pack()
        assembled = apply_shared_evidence_prefix(
            _LENS_TAIL, reviewer_role="reviewer_code", evidence_pack=pack
        )
        assert assembled != _LENS_TAIL
        assert assembled.startswith(build_shared_evidence_prefix(pack))
        assert assembled.endswith(_LENS_TAIL)

    def test_log_record_captures_hit_rate_and_per_wave_cost(self, monkeypatch):
        """log mode records the measured wave cache-hit rate AND per-wave cost."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        rec = evidence_prefix_log_record(
            wave_roles=["reviewer_security", "reviewer_code"],
            shared_prefix_bytes=4096,
            session_records=[
                {"session": {"prompt_tokens": 1000, "cached_tokens": 900, "cost": 0.02}},
                {"session": {"prompt_tokens": 1000, "cached_tokens": 700, "cost": 0.03}},
            ],
        )
        assert rec["kind"] == "evidence_prefix"
        assert rec["mode"] == "log"
        assert rec["cache_stats"]["cache_hit_rate_pct"] == 80.0
        assert rec["cache_stats"]["per_wave_cost"] == pytest.approx(0.05)
        assert rec["cache_stats"]["sessions"] == 2

    def test_per_wave_cost_is_none_when_uncaptured_not_zero(self):
        """Absent cost reads as None ('not captured'), never a silent 0.0."""
        stats = aggregate_wave_cache_stats(
            [{"session": {"prompt_tokens": 100, "cached_tokens": 10}}]
        )
        assert stats["per_wave_cost"] is None
        assert stats["cache_hit_rate_pct"] == 10.0


# ---------------------------------------------------------------------------
# Delphi redaction is unaffected (task-7-3 acceptance)
# ---------------------------------------------------------------------------
#
# Delphi independence redacts a *producer's self-assessment* out of the review
# input so lenses judge blind. The evidence pack carries REPO FACTS ONLY (diff,
# files, symbols, env) — never a producer's attribution or self-assessment — so
# there is nothing in it for Delphi redaction to act on: its output is unchanged.
# Enforced structurally as a fixed repo-facts field allowlist so a future edge
# that smuggles a producer-attributed field into the shared prefix fails here.


class TestDelphiRedactionUnaffected:
    _REPO_FACT_FIELDS = frozenset(
        {"schema_version", "diff", "files", "symbols", "environment", "diff_truncated"}
    )

    def test_pack_schema_is_repo_facts_only(self):
        """No producer/author/self-assessment field for Delphi to redact."""
        actual = {f.name for f in fields(EvidencePack)}
        assert actual == self._REPO_FACT_FIELDS

    def test_no_producer_attribution_field(self):
        smell = ("author", "producer", "agent", "self", "assessment", "opinion", "claim")
        offenders = [
            f.name for f in fields(EvidencePack) if any(s in f.name.lower() for s in smell)
        ]
        assert offenders == []

    def test_rendered_pack_carries_no_producer_attribution(self):
        pack = build_pack(
            diff="diff --git a/x b/x\n@@\n+x\n",
            files=[ChangedFileEvidence(path="x.py", content="def x(): ...\n")],
            symbols=[SymbolEvidence(symbol="x", defined_in="x.py")],
            environment={"python_version": "3.12.0"},
        )
        rendered = render_pack(pack).lower()
        for marker in ("producer:", "author:", "my assessment", "i think", "self-assessment"):
            assert marker not in rendered
