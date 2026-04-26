"""Pluggable detectors for the ``/onboard-repo`` skill (issue #2073).

The skill (``skills/onboard-repo/SKILL.md``) drives onboarding
conversationally; this module is the headless brain it calls. Each
:class:`Detector` looks at the contents of a checkout root and emits
zero or more :class:`DetectionResult` records describing a proposed
``build_commands`` / ``persist`` / ``checks`` block plus a
``confidence`` and ``reasoning`` so the skill can explain *why* a
detection fired.

Built-in detectors cover the languages enumerated in HITL
decision-14: Python (uv + pip), Node (npm + pnpm + yarn), and Go.
Mixed-language repos (e.g. Go service with a Node frontend) fan out
across detectors and the orchestrator merges the proposed blocks.

Plug-in escape hatch (Q7):

    from egg_config.onboard_detectors import register_detector

    @register_detector
    class BazelDetector:
        priority = 50
        def detect(self, repo_path):
            ...

The ``priority`` field controls ordering — higher priorities run
first.  Within a single language, ties are broken by class name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class DetectionResult:
    """The output shape every detector returns.

    ``language`` names the technology (e.g. ``"python-uv"``,
    ``"node-pnpm"``, ``"go"``). ``build_commands`` is a list of shell
    commands the build stage should run. ``persist`` is the unified
    persist list (entries beginning with ``/`` are absolute system
    paths; anything else is repo-relative). ``checks`` mirrors the
    on-disk schema. ``watch_files`` is the manifest catalog the build
    context should carry.

    ``confidence`` is in ``[0, 1]``. The skill surfaces it to the user
    so they can sanity-check the detection. ``reasoning`` is a one-
    line human-readable explanation.
    """

    language: str
    build_commands: list[str] = field(default_factory=list)
    persist: list[str] = field(default_factory=list)
    watch_files: list[str] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "build_commands": list(self.build_commands),
            "persist": list(self.persist),
            "watch_files": list(self.watch_files),
            "checks": list(self.checks),
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@runtime_checkable
class Detector(Protocol):
    """The interface every detector implements.

    Detectors are stateless and idempotent — implementations should
    not mutate ``repo_path`` or any external state.
    """

    priority: int

    def detect(self, repo_path: Path) -> DetectionResult | None:
        """Return a :class:`DetectionResult` or ``None`` if not applicable."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Detector registry
# ---------------------------------------------------------------------------

_DETECTORS: list[Detector] = []


def register_detector(detector: Detector | type[Detector]) -> Detector:
    """Register a detector instance or class in the global registry.

    Accepts either an instance or a zero-arg class for decorator-style
    use. The registry is process-global; ``run_detectors()`` reads it.
    """
    obj = detector() if isinstance(detector, type) else detector
    if not isinstance(obj, Detector):
        raise TypeError(f"register_detector expected a Detector, got {type(obj).__name__}")
    _DETECTORS.append(obj)
    return obj


def _ordered_detectors() -> list[Detector]:
    """Return registered detectors sorted by priority (desc), tie-break by class name."""
    return sorted(_DETECTORS, key=lambda d: (-d.priority, type(d).__name__))


# ---------------------------------------------------------------------------
# Built-in detectors
# ---------------------------------------------------------------------------


def _read_json_safe(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@dataclass
class PythonUvDetector:
    """Python project using ``uv`` (the new fast installer).

    Triggers when ``pyproject.toml`` and ``uv.lock`` both exist.
    Recommends ``uv sync --no-install-project`` so the build stage
    only installs third-party deps (the #2087 trap if omitted).
    """

    priority: int = 100

    def detect(self, repo_path: Path) -> DetectionResult | None:
        pyproject = repo_path / "pyproject.toml"
        uv_lock = repo_path / "uv.lock"
        if not (pyproject.is_file() and uv_lock.is_file()):
            return None
        watch_files = ["pyproject.toml", "uv.lock"]
        if (repo_path / "Makefile").is_file():
            watch_files.insert(0, "Makefile")
        return DetectionResult(
            language="python-uv",
            build_commands=[
                "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh",
                "uv sync --no-install-project",
            ],
            persist=["/usr/local/bin", ".venv"],
            watch_files=watch_files,
            checks=[],
            confidence=0.95,
            reasoning=(
                "Found pyproject.toml + uv.lock. Recommended `uv sync "
                "--no-install-project` so only third-party deps are "
                "installed — the local project doesn't need to build "
                "for ruff/pytest/etc. to work in the sandbox (see "
                "#2087)."
            ),
        )


@dataclass
class PythonPipDetector:
    """Python project using pip + ``requirements*.txt``."""

    priority: int = 90

    def detect(self, repo_path: Path) -> DetectionResult | None:
        req = repo_path / "requirements.txt"
        req_dev = repo_path / "requirements-dev.txt"
        pyproject = repo_path / "pyproject.toml"
        if not req.is_file() and not req_dev.is_file():
            return None
        # If uv.lock is present prefer the uv detector.
        if (repo_path / "uv.lock").is_file():
            return None
        commands = []
        watch_files = []
        if req.is_file():
            commands.append("pip install -r requirements.txt")
            watch_files.append("requirements.txt")
        if req_dev.is_file():
            commands.append("pip install -r requirements-dev.txt")
            watch_files.append("requirements-dev.txt")
        if pyproject.is_file():
            watch_files.insert(0, "pyproject.toml")
        return DetectionResult(
            language="python-pip",
            build_commands=commands,
            persist=[".venv"],
            watch_files=watch_files,
            checks=[],
            confidence=0.85,
            reasoning=(
                "Found requirements*.txt. Recommended `pip install -r "
                "requirements.txt` (no -e .) so the local project "
                "doesn't need to build."
            ),
        )


@dataclass
class NodePnpmDetector:
    """Node project using pnpm — ``pnpm-lock.yaml``."""

    priority: int = 100

    def detect(self, repo_path: Path) -> DetectionResult | None:
        lock = repo_path / "pnpm-lock.yaml"
        pkg = repo_path / "package.json"
        if not (lock.is_file() and pkg.is_file()):
            return None
        return DetectionResult(
            language="node-pnpm",
            build_commands=[
                "npm install -g pnpm",
                "pnpm install --frozen-lockfile",
            ],
            persist=["node_modules"],
            watch_files=["package.json", "pnpm-lock.yaml"],
            checks=_node_default_checks(_read_json_safe(pkg)),
            confidence=0.95,
            reasoning="Found pnpm-lock.yaml + package.json. Recommended pnpm install.",
        )


@dataclass
class NodeYarnDetector:
    """Node project using yarn — ``yarn.lock``."""

    priority: int = 95

    def detect(self, repo_path: Path) -> DetectionResult | None:
        lock = repo_path / "yarn.lock"
        pkg = repo_path / "package.json"
        if not (lock.is_file() and pkg.is_file()):
            return None
        return DetectionResult(
            language="node-yarn",
            build_commands=[
                "npm install -g yarn",
                "yarn install --frozen-lockfile",
            ],
            persist=["node_modules"],
            watch_files=["package.json", "yarn.lock"],
            checks=_node_default_checks(_read_json_safe(pkg)),
            confidence=0.95,
            reasoning="Found yarn.lock + package.json. Recommended yarn install.",
        )


@dataclass
class NodeNpmDetector:
    """Node project using npm — ``package-lock.json``.

    Lower priority than pnpm/yarn so a repo with multiple lockfiles
    favours the more specific tooling.
    """

    priority: int = 90

    def detect(self, repo_path: Path) -> DetectionResult | None:
        lock = repo_path / "package-lock.json"
        pkg = repo_path / "package.json"
        if not pkg.is_file():
            return None
        # Defer to the more specific detectors when their lockfiles
        # are also present.
        if (repo_path / "pnpm-lock.yaml").is_file() or (repo_path / "yarn.lock").is_file():
            return None
        watch_files = ["package.json"]
        if lock.is_file():
            watch_files.append("package-lock.json")
            cmd = "npm ci"
        else:
            cmd = "npm install"
        return DetectionResult(
            language="node-npm",
            build_commands=[cmd],
            persist=["node_modules"],
            watch_files=watch_files,
            checks=_node_default_checks(_read_json_safe(pkg)),
            confidence=0.85,
            reasoning=(
                f"Found package.json{' + package-lock.json' if lock.is_file() else ''}. "
                f"Recommended `{cmd}`."
            ),
        )


@dataclass
class GoDetector:
    """Go project — ``go.mod``."""

    priority: int = 100

    def detect(self, repo_path: Path) -> DetectionResult | None:
        mod = repo_path / "go.mod"
        if not mod.is_file():
            return None
        # Try to read the go directive so the install command pins it.
        go_version = ""
        for line in _read_text_safe(mod).splitlines():
            stripped = line.strip()
            if stripped.startswith("go ") and len(stripped.split()) >= 2:
                go_version = stripped.split()[1]
                break
        version_token = go_version or "1.22.0"
        watch_files = ["go.mod"]
        if (repo_path / "go.sum").is_file():
            watch_files.append("go.sum")
        return DetectionResult(
            language="go",
            build_commands=[
                f'curl -fsSL "https://go.dev/dl/go{version_token}.linux-$(dpkg '
                '--print-architecture).tar.gz" | tar -xz -C /usr/local',
                "PATH=/usr/local/go/bin:$PATH go mod download",
            ],
            persist=["/usr/local/go"],
            watch_files=watch_files,
            checks=[],
            confidence=0.9,
            reasoning=(f"Found go.mod (Go {version_token}). Recommended `go mod download`."),
        )


def _node_default_checks(pkg: dict[str, Any]) -> list[dict[str, str]]:
    """Synthesise default check entries from ``package.json`` scripts."""
    out: list[dict[str, str]] = []
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return out
    if "lint" in scripts:
        out.append({"name": "lint", "command": "npm run lint"})
    if "test" in scripts:
        out.append({"name": "test", "command": "npm test"})
    return out


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def _builtin_detectors() -> list[Detector]:
    return [
        PythonUvDetector(),
        PythonPipDetector(),
        NodePnpmDetector(),
        NodeYarnDetector(),
        NodeNpmDetector(),
        GoDetector(),
    ]


def run_detectors(repo_path: Path, *, include_registered: bool = True) -> list[DetectionResult]:
    """Run every applicable detector against ``repo_path``.

    Built-in detectors run first (always), followed by any plug-ins
    registered via :func:`register_detector`. Returns the non-``None``
    results sorted by priority desc.
    """
    repo_path = Path(repo_path).resolve()
    detectors: list[Detector] = list(_builtin_detectors())
    if include_registered:
        detectors.extend(_ordered_detectors())
    detectors = sorted(detectors, key=lambda d: (-d.priority, type(d).__name__))

    results: list[DetectionResult] = []
    for detector in detectors:
        try:
            result = detector.detect(repo_path)
        except Exception:
            continue
        if result is not None:
            results.append(result)
    return results


def merge_detections(detections: list[DetectionResult]) -> DetectionResult:
    """Merge multiple detections into a single proposed block.

    Used for mixed-language repos: concatenate build_commands /
    watch_files / persist / checks, dedup-preserving order, take the
    max confidence, and join the reasoning strings.

    Returns a synthetic :class:`DetectionResult` with
    ``language="mixed"``.
    """
    if not detections:
        return DetectionResult(language="none", confidence=0.0, reasoning="No detectors fired.")
    if len(detections) == 1:
        return detections[0]

    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    languages = "+".join(sorted({d.language for d in detections}))
    build_commands: list[str] = []
    persist: list[str] = []
    watch_files: list[str] = []
    checks: list[dict[str, str]] = []
    reasonings: list[str] = []
    for det in detections:
        build_commands.extend(det.build_commands)
        persist.extend(det.persist)
        watch_files.extend(det.watch_files)
        for entry in det.checks:
            if entry not in checks:
                checks.append(entry)
        reasonings.append(f"[{det.language}] {det.reasoning}")
    return DetectionResult(
        language=f"mixed:{languages}",
        build_commands=_dedup(build_commands),
        persist=_dedup(persist),
        watch_files=_dedup(watch_files),
        checks=checks,
        confidence=max(d.confidence for d in detections),
        reasoning=" / ".join(reasonings),
    )


__all__ = [
    "Detector",
    "DetectionResult",
    "GoDetector",
    "NodeNpmDetector",
    "NodePnpmDetector",
    "NodeYarnDetector",
    "PythonPipDetector",
    "PythonUvDetector",
    "merge_detections",
    "register_detector",
    "run_detectors",
]
