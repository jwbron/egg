"""Unit tests for the read-only shared-evidence gatherer (#3523 §5, S7).

Covers the hard rules from the issue and the task-7-1 acceptance:

- the pack is **byte-identical across a simulated wave** (order-independent
  build + deterministic render);
- the pack **carries no editorializing fields** (evidence, never conclusions);
- the pack is **mechanically path-ordered**;
- the staged flag resolves off/log/on with unknown => off;
- the independence guardrail keeps the tester / finding-verifier cold-start.
"""

from __future__ import annotations

import subprocess

import pytest
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
