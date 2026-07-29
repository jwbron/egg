"""In-pod working-tree checkpoint at the session boundary (#3658).

When the wall-clock budget expires the SDK call is cancelled and the CLI
returns; before this, nothing committed, stashed, or flushed the tree on the way
out. #3644 makes the *next* respawn survivable, but the snapshot it takes is a
different process's, minutes later, with no marker of where the agent stopped.

These tests drive the real ``git`` against real temp repos: the value of this
helper is entirely in what it does to a git tree under adverse conditions, and a
mocked ``subprocess`` would pin the calls rather than the outcome.

Half of them drive it through a **stand-in for ``sandbox/scripts/git``** instead
(:data:`_FAKE_GATEWAY_GIT`), because a bare git cannot exercise the constraint
that actually broke the first cut of this module: in the pod every call is
policy-routed, and the three ways that differs from a direct git — global ``-c``
overrides dropped, flags allowlisted per subcommand, repo-discovery ``rev-parse``
answering "not a git repository" — each silently turned the snapshot into a
no-op. A test that only ever sees a direct git is green for the exact code that
captured nothing in production.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from egg_agent.checkpoint import CHECKPOINT_ENV, checkpoint_working_tree

# Resolved once, before any test puts a fake ``git`` on PATH: the assertions
# below must observe the tree with the real binary, not through the stand-in
# wrapper they install.
_REAL_GIT = shutil.which("git") or "git"


def _probe_git_init() -> str:
    """Return why a real ``git init`` is unusable here, or ``""`` if it works.

    In the agent sandbox ``git`` *is* the gateway wrapper and ``git init`` is
    refused outright, so every test in this file would ERROR during fixture
    setup with a message about the wrapper rather than about the checkpoint —
    noise that reads like a broken test suite. An honest skip says "not
    exercised in this environment"; CI runs on a real git and gets the coverage.
    """
    with tempfile.TemporaryDirectory() as probe:
        try:
            result = subprocess.run(
                [_REAL_GIT, "init", probe],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as e:  # git missing entirely
            return str(e)
    if result.returncode == 0:
        return ""
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[-1] if detail else f"git init exited {result.returncode}"


_GIT_INIT_UNAVAILABLE = _probe_git_init()

pytestmark = pytest.mark.skipif(
    bool(_GIT_INIT_UNAVAILABLE),
    reason=f"needs a real git: {_GIT_INIT_UNAVAILABLE}",
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_REAL_GIT, "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit, so HEAD exists and diffs are meaningful."""
    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.name", "test", cwd=tmp_path)
    _git("config", "user.email", "test@localhost", cwd=tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)
    return tmp_path


def _head_message(repo: Path) -> str:
    return _git("log", "-1", "--pretty=%B", cwd=repo).stdout


def _head_author(repo: Path) -> str:
    return _git("log", "-1", "--pretty=%an <%ae>", cwd=repo).stdout.strip()


def _head_committer(repo: Path) -> str:
    return _git("log", "-1", "--pretty=%cn <%ce>", cwd=repo).stdout.strip()


def test_commits_modified_and_untracked_work(repo: Path):
    (repo / "seed.txt").write_text("edited mid-turn\n")
    (repo / "new_module.py").write_text("# half-written\n")

    sha = checkpoint_working_tree(repo)

    assert sha and len(sha) == 40
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == ""
    tracked = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=repo).stdout.split()
    assert "new_module.py" in tracked


def test_snapshot_carries_the_salvage_marker_and_identity(repo: Path):
    """One ``[salvage]`` grep must find every machine-made tree snapshot."""
    (repo / "seed.txt").write_text("edited\n")

    checkpoint_working_tree(repo)

    message = _head_message(repo)
    assert message.startswith("[salvage]")
    assert "#3658" in message
    # The message has to warn a reader that this is mid-edit content, not a
    # considered commit — otherwise the next agent builds on it as if it were.
    assert "machine commit" in message
    # Against a direct git both trailers come from the ``GIT_*`` env vars. In the
    # pod only the author survives, because it rides on ``commit --author=`` and
    # the env cannot cross the gateway's HTTP boundary — see
    # ``test_gateway_routed_snapshot_keeps_the_salvage_author``, which pins that
    # weaker guarantee separately so a reader knows which one to grep on.
    assert _head_author(repo) == "egg-salvage <egg-salvage@localhost>"
    assert _head_committer(repo) == "egg-salvage <egg-salvage@localhost>"


def test_clean_tree_is_a_no_op_not_a_failure(repo: Path):
    """The good case: an agent that committed before the deadline."""
    before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    assert checkpoint_working_tree(repo) is None

    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before


def test_non_repo_path_returns_none(tmp_path: Path):
    assert checkpoint_working_tree(tmp_path / "not-a-repo") is None


def test_disabled_leaves_the_tree_untouched(repo: Path, monkeypatch):
    (repo / "seed.txt").write_text("edited\n")
    monkeypatch.setenv(CHECKPOINT_ENV, "false")

    assert checkpoint_working_tree(repo) is None

    assert _git("status", "--porcelain", cwd=repo).stdout.strip() != ""


def test_resolves_the_repo_from_egg_repo_path(repo: Path, monkeypatch):
    """The pod sets EGG_REPO_PATH; the CLI calls this with no argument."""
    (repo / "seed.txt").write_text("edited\n")
    monkeypatch.setenv("EGG_REPO_PATH", str(repo))

    assert checkpoint_working_tree() is not None
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == ""


def test_commits_despite_a_repo_pre_commit_hook_that_fails(repo: Path):
    """A hook must not be able to veto the snapshot — that loses the work."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None


def test_commits_despite_gpgsign_with_no_key(repo: Path):
    """A worktree inheriting commit.gpgsign=true must still checkpoint."""
    _git("config", "commit.gpgsign", "true", cwd=repo)
    _git("config", "user.signingkey", "0xNOSUCHKEY", cwd=repo)
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None


def test_never_raises_on_a_hostile_worktree(repo: Path, monkeypatch):
    """The contract is 'never raises' — an exit code must stay classifiable."""

    def _boom(*_a, **_kw):
        raise OSError("git is not on PATH")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert checkpoint_working_tree(repo) is None


# ---------------------------------------------------------------------------
# The gateway-routed path
# ---------------------------------------------------------------------------

# A stand-in for ``sandbox/scripts/git`` reduced to the four behaviours that
# decide whether this checkpoint captures anything in a pod. It is deliberately
# a separate process on PATH rather than a ``subprocess.run`` monkeypatch: the
# thing under test is what survives an argv crossing a policy boundary, and a
# patch that inspects the argv in-process would let a command through that the
# real wrapper drops on the floor.
#
# Faithful to, in order: the global-option loop in ``sandbox/scripts/git``
# (``-c`` skipped WITH its value, ``-C`` captured as the work dir), its
# ``rev-parse`` discovery branch (exec'd to the real binary, which in-pod sees a
# tmpfs-shadowed ``.git``), ``gateway/git_client/_validation.py`` (per-subcommand
# flag allowlist), and ``gateway/gateway/_git_execute.py`` (phase restrictions
# validated over the WHOLE staged set, plus a forced ``--no-verify``). The
# gateway also runs on the host, so the pod's ``GIT_*`` env never reaches it.
_FAKE_GATEWAY_GIT = '''#!/usr/bin/env python3
"""Test stand-in for sandbox/scripts/git — see test_checkpoint.py."""
import json
import os
import subprocess
import sys

REAL_GIT = os.environ["FAKE_GATEWAY_REAL_GIT"]
LOG = os.environ["FAKE_GATEWAY_LOG"]
BLOCKED = [p for p in os.environ.get("FAKE_GATEWAY_PHASE_BLOCKED", "").split(",") if p]

ALLOWED_FLAGS = {
    "status": {"--porcelain"},
    "diff": {"--cached", "--name-only"},
    "add": {"--all", "-A", "--ignore-errors"},
    "commit": {"-m", "--message", "--author", "--allow-empty"},
    "reset": {"--quiet", "-q"},
    "rev-parse": {"--abbrev-ref", "--short", "--verify"},
}
DISCOVERY = {
    "--git-dir", "--git-common-dir", "--show-toplevel", "--is-inside-work-tree",
    "--is-inside-git-dir", "--is-bare-repository", "--show-prefix", "--show-cdup",
}


def die(message, code):
    sys.stderr.write(message + "\\n")
    raise SystemExit(code)


argv = sys.argv[1:]
work_dir = None
i = 0
while i < len(argv):
    if argv[i] == "-c":       # dropped WITH its value; never reaches the gateway
        i += 2
        continue
    if argv[i] == "-C":
        work_dir = argv[i + 1]
        i += 2
        continue
    break
sub = argv[i] if i < len(argv) else ""
rest = argv[i + 1:]

with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"raw": argv, "sub": sub, "args": rest}) + "\\n")

if sub == "rev-parse" and DISCOVERY.intersection(rest):
    die("fatal: not a git repository (or any of the parent directories): .git", 128)

allowed = ALLOWED_FLAGS.get(sub)
if allowed is None:
    die("Operation 'git %s' is not allowed" % sub, 1)
after_separator = False
for arg in rest:
    if arg == "--":
        after_separator = True
        continue
    if after_separator or not arg.startswith("-"):
        continue
    if arg.split("=")[0] not in allowed:
        die("Flag '%s' is not allowed for git %s" % (arg, sub), 1)

if sub == "commit" and BLOCKED:
    staged = subprocess.run(
        [REAL_GIT, "-C", work_dir, "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    hit = [p for p in staged if p in BLOCKED]
    if hit:
        die(
            "Commit blocked: Phase 'implement' cannot modify: %s. "
            "Unstage the blocked files with 'git reset HEAD <file>'." % ", ".join(hit),
            1,
        )

if sub == "commit":
    rest = ["--no-verify"] + rest

# The gateway runs on the host and builds its child env from its own process, so
# the pod's GIT_AUTHOR_* / GIT_COMMITTER_* exports simply do not exist there.
env = {
    k: v for k, v in os.environ.items()
    if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
}
cmd = [REAL_GIT]
if work_dir:
    cmd += ["-C", work_dir]
cmd += [sub] + rest
done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
raise SystemExit(done.returncode)
'''


@pytest.fixture
def gateway_git(tmp_path_factory, monkeypatch) -> Path:
    """Put the gateway-shaped wrapper on PATH; return its call log.

    Its files live outside ``tmp_path`` on purpose — ``tmp_path`` *is* the repo
    under test, and a wrapper that ends up inside the tree it snapshots is a
    wrapper that stages itself.
    """
    bin_dir = tmp_path_factory.mktemp("gateway")
    wrapper = bin_dir / "git"
    wrapper.write_text(_FAKE_GATEWAY_GIT)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    log = bin_dir / "calls.jsonl"
    monkeypatch.setenv("FAKE_GATEWAY_REAL_GIT", _REAL_GIT)
    monkeypatch.setenv("FAKE_GATEWAY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def _gateway_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_gateway_routed_snapshot_commits_the_tree(repo: Path, gateway_git: Path):
    """The whole point: a policy-routed git must still capture the tree.

    The first cut of this module used ``rev-parse --is-inside-work-tree`` as its
    repo check and pinned identity with ``-c user.name=``. Both are inert through
    the wrapper, so it declined every snapshot in the one environment it exists
    for while passing a direct-git suite.
    """
    (repo / "seed.txt").write_text("edited mid-turn\n")
    (repo / "new_module.py").write_text("# half-written\n")

    sha = checkpoint_working_tree(repo)

    assert sha and len(sha) == 40
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == ""
    assert "new_module.py" in _git("ls-tree", "-r", "--name-only", "HEAD", cwd=repo).stdout
    assert _head_message(repo).startswith("[salvage]")


def test_gateway_routed_snapshot_keeps_the_salvage_author(repo: Path, gateway_git: Path):
    """Author survives the boundary on ``--author=``; committer does not.

    The pod's ``GIT_*`` exports die at the gateway's HTTP boundary, so the
    committer is whatever ambient identity the gateway commits under. Recovery
    tooling therefore has to match on the author — this pins that it can.
    """
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None

    assert _head_author(repo) == "egg-salvage <egg-salvage@localhost>"
    assert _head_committer(repo) != "egg-salvage <egg-salvage@localhost>"


def test_no_pinned_config_or_discovery_reaches_the_gateway(repo: Path, gateway_git: Path):
    """Pin the argv shape the wrapper actually forwards.

    Two of these are silent failures rather than loud ones — a dropped ``-c`` and
    a rejected flag both leave a plausible-looking command that stages nothing —
    so the commands are asserted directly instead of only through their effect.
    """
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None

    calls = _gateway_calls(gateway_git)
    assert calls, "the checkpoint never invoked git"
    # Whatever this module pins with ``-c`` is documentation for the direct-git
    # path only; nothing may depend on it arriving.
    assert all("-c" not in call["args"] for call in calls)
    subcommands = [call["sub"] for call in calls]
    assert subcommands[0] == "status", "the repo check must be the gateway-routed status"
    # A discovery rev-parse is routed AROUND the gateway to a binary that sees a
    # tmpfs-shadowed ``.git``; using one as the repo check declines every pod.
    assert not any(
        call["sub"] == "rev-parse" and any(a.startswith("--") for a in call["args"])
        for call in calls
    )
    assert "add" in subcommands
    add = next(call for call in calls if call["sub"] == "add")
    assert "--ignore-errors" in add["args"], "the allowlisted tolerant add is the point of #3658"
    commit = next(call for call in calls if call["sub"] == "commit")
    assert "--author=egg-salvage <egg-salvage@localhost>" in commit["args"]


def test_gateway_phase_restrictions_lose_only_the_blocked_paths(
    repo: Path, gateway_git: Path, monkeypatch
):
    """``add -A`` stages out-of-phase files; the 403 must not cost the rest.

    Dropping the snapshot on a phase rejection would lose the tree in exactly
    the pipeline sessions this exists to protect, since ``add -A`` cannot avoid
    staging the scratch files a phase forbids.
    """
    (repo / "seed.txt").write_text("in-phase edit\n")
    (repo / "scratch.md").write_text("out-of-phase scratch\n")
    monkeypatch.setenv("FAKE_GATEWAY_PHASE_BLOCKED", "scratch.md")

    assert checkpoint_working_tree(repo) is not None

    committed = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=repo).stdout.split()
    assert "seed.txt" in committed
    assert "scratch.md" not in committed
    # The blocked file is still there to be recovered by hand, not silently gone.
    assert "scratch.md" in _git("status", "--porcelain", cwd=repo).stdout
    assert "PARTIAL" in _head_message(repo)


def test_gateway_phase_restrictions_blocking_everything_declines(
    repo: Path, gateway_git: Path, monkeypatch
):
    """Nothing left to commit after unstaging is a decline, not an empty commit."""
    before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "scratch.md").write_text("out-of-phase scratch\n")
    monkeypatch.setenv("FAKE_GATEWAY_PHASE_BLOCKED", "scratch.md")

    assert checkpoint_working_tree(repo) is None

    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before
    assert "scratch.md" in _git("status", "--porcelain", cwd=repo).stdout
