"""Contract-layer tests for the multi-repo repo dimension (#3393, slice-1).

These cover the CONTRACT half of the two-layer slice-1 design
(``shared/egg_contracts/models.py``); the orchestrator half
(``RepoSpec`` / ``Pipeline.repos`` / ``primary_repo`` /
``resolve_slice_repo``) is exercised in ``orchestrator/tests/test_models.py``.

The design (ratified architect ``aeb3528`` / risk_analyst R1) splits the repo
dimension across two model layers precisely because the ``Contract`` model has
NO repo field of its own and cannot see the orchestrator ``Pipeline``. So at
the contract layer the invariants are deliberately narrow:

* ``Slice.repo`` exists (``str | None``, default ``None``) and round-trips.
* ``Contract.schemaVersion`` is ``"1.4"``; ``_migrate_schema_version_to_1_4``
  is a PURE ADDITIVE after-stamp (guarded on ``"1.3"``, idempotent, no field
  mutation) — a legacy 1.3 load bumps the version but leaves every
  ``Slice.repo`` as ``None`` (the absent⇒primary default is a RUNTIME
  orchestrator concern, never the migration's).
* The four pre-existing migration branches (1.0→1.1, the wrap-mode
  ``pr.context_*`` strip + 1.1→1.2, 1.2→1.3) still fire for their versions —
  no regression from adding the 1.4 stamp.
"""

from __future__ import annotations

from egg_contracts.models import Contract, IssueInfo, Slice


def _payload(schema_version: str | None, *, slices: list[dict] | None = None) -> dict:
    """A minimal, valid contract payload at ``schema_version``.

    ``schemaVersion`` is omitted from the dict entirely when ``None`` so the
    "field absent" load path (pre-1.0 contracts) is exercised too.
    """
    data: dict = {
        "issue": {
            "number": 3393,
            "title": "multi-repo pipelines",
            "url": "https://example.com/i/3393",
        },
        "slices": slices if slices is not None else [],
    }
    if schema_version is not None:
        data["schemaVersion"] = schema_version
    return data


def _slice_dict(slice_id: str, name: str, **extra: object) -> dict:
    base: dict = {"id": slice_id, "name": name, "tasks": [], "dependencies": []}
    base.update(extra)
    return base


class TestSliceRepoField:
    """``Slice.repo`` exists, defaults to ``None``, and round-trips (AC-a)."""

    def test_slice_repo_defaults_to_none(self) -> None:
        s = Slice(id="slice-1", name="schema repo")
        assert s.repo is None

    def test_slice_repo_accepts_owner_name(self) -> None:
        s = Slice(id="slice-1", name="schema repo", repo="jwbron/egg")
        assert s.repo == "jwbron/egg"

    def test_fresh_1_4_contract_round_trips_slice_repo(self) -> None:
        contract = Contract.model_validate(
            _payload(
                "1.4",
                slices=[
                    _slice_dict("slice-1", "in repo A", repo="jwbron/egg"),
                    _slice_dict("slice-2", "unset repo"),
                ],
            )
        )
        assert contract.schemaVersion == "1.4"
        assert contract.slices[0].repo == "jwbron/egg"
        assert contract.slices[1].repo is None

        # Dump → reload is a no-op on the repo field and the version.
        reloaded = Contract.model_validate(contract.model_dump())
        assert reloaded.schemaVersion == "1.4"
        assert reloaded.slices[0].repo == "jwbron/egg"
        assert reloaded.slices[1].repo is None


class TestFreshContractSchemaVersion:
    """A brand-new contract is stamped ``"1.4"`` by default."""

    def test_default_schema_version_is_1_4(self) -> None:
        contract = Contract(issue={"number": 3393, "title": "x", "url": "u"})
        assert contract.schemaVersion == "1.4"


class TestLegacy13LoadIsAdditiveStamp:
    """Loading a persisted 1.3 contract bumps to 1.4 without filling repo (AC-b)."""

    def test_1_3_load_bumps_to_1_4(self) -> None:
        contract = Contract.model_validate(
            _payload("1.3", slices=[_slice_dict("slice-1", "legacy")])
        )
        assert contract.schemaVersion == "1.4"

    def test_1_3_load_leaves_every_slice_repo_none(self) -> None:
        # CRITICAL: the migration is a pure stamp. It must NOT populate
        # Slice.repo — the absent⇒primary default is a RUNTIME orchestrator
        # concern (resolve_slice_repo), never the contract migration's.
        contract = Contract.model_validate(
            _payload(
                "1.3",
                slices=[
                    _slice_dict("slice-1", "one"),
                    _slice_dict("slice-2", "two"),
                    _slice_dict("slice-3", "three"),
                ],
            )
        )
        assert contract.schemaVersion == "1.4"
        assert [s.repo for s in contract.slices] == [None, None, None]

    def test_1_3_load_preserves_an_explicitly_set_repo(self) -> None:
        # A slice that already carries a repo keeps it — the stamp neither
        # fills absent repos nor clobbers present ones.
        contract = Contract.model_validate(
            _payload("1.3", slices=[_slice_dict("slice-1", "explicit", repo="jwbron/egg")])
        )
        assert contract.schemaVersion == "1.4"
        assert contract.slices[0].repo == "jwbron/egg"


class TestSchemaVersion14StampIdempotentAndGuarded:
    """The 1.4 stamp is idempotent and version-exact (AC-c)."""

    def test_1_4_payload_stays_1_4(self) -> None:
        contract = Contract.model_validate(_payload("1.4"))
        assert contract.schemaVersion == "1.4"

    def test_reload_of_1_4_is_stable(self) -> None:
        contract = Contract.model_validate(_payload("1.4"))
        reloaded = Contract.model_validate(contract.model_dump())
        assert reloaded.schemaVersion == "1.4"

    def test_stamp_fires_on_a_1_3_instance(self) -> None:
        # Direct branch coverage: the 1.4 stamp lifts a 1.3 instance to 1.4.
        # ``issue`` is supplied because the assignment re-runs the after-mode
        # validators (incl. ``_require_issue_or_pipeline_id``).
        c = Contract.model_construct(
            schemaVersion="1.3", issue=IssueInfo(number=3393, title="x", url="u")
        )
        assert c._migrate_schema_version_to_1_4().schemaVersion == "1.4"

    def test_stamp_is_idempotent_on_1_4(self) -> None:
        c = Contract.model_construct(schemaVersion="1.4")
        assert c._migrate_schema_version_to_1_4().schemaVersion == "1.4"

    def test_stamp_never_downgrades_a_future_version(self) -> None:
        # A hypothetical future 2.0 must not be silently downgraded.
        c = Contract.model_construct(schemaVersion="2.0")
        assert c._migrate_schema_version_to_1_4().schemaVersion == "2.0"

    def test_stamp_does_not_fire_early_on_1_2(self) -> None:
        c = Contract.model_construct(schemaVersion="1.2")
        # The 1.4 stamp guards on "1.3" only — it must not lift 1.2.
        assert c._migrate_schema_version_to_1_4().schemaVersion == "1.2"


class TestPriorMigrationBranchesStillFire:
    """The four pre-existing branches survive the 1.4 addition (AC-c, no regression)."""

    def test_migrate_1_1_branch_fires_on_1_0(self) -> None:
        # The 1.0→1.1 after-stamp still fires for a 1.0 instance. (In a real
        # load the wrap-mode 1.2 migrator lifts {None,1.0,1.1}→1.2 first, so
        # this branch is only observable via a direct call; ``issue`` is
        # supplied because the assignment re-runs the after-mode validators.)
        c = Contract.model_construct(
            schemaVersion="1.0", issue=IssueInfo(number=3393, title="x", url="u")
        )
        assert c._migrate_schema_version_to_1_1().schemaVersion == "1.1"

    def test_migrate_1_1_branch_guarded(self) -> None:
        c = Contract.model_construct(schemaVersion="1.1")
        assert c._migrate_schema_version_to_1_1().schemaVersion == "1.1"

    def test_migrate_1_3_branch_guarded(self) -> None:
        # The 1.3 stamp only lifts an exact "1.2"; on "1.3" it is a no-op (the
        # 1.4 stamp owns "1.3"). Note the *firing* of the 1.2→1.3 branch is
        # asserted observably in TestFullMigrationChainComposes.test_1_2_...:
        # a direct call cannot isolate it because assigning "1.3" re-runs the
        # after-mode 1.4 stamp and cascades to "1.4".
        c = Contract.model_construct(schemaVersion="1.3")
        assert c._migrate_schema_version_to_1_3().schemaVersion == "1.3"

    def test_wrap_mode_strips_pr_context_fields_on_pre_1_2_load(self) -> None:
        # The wrap-mode _migrate_schema_version_to_1_2 strips the removed
        # pr.context_* keys for versions in {None, 1.0, 1.1}; context_pr_number
        # survives. This must still happen after the 1.4 stamp was added.
        data = _payload("1.1")
        data["pr"] = {
            "title": "keep me",
            "context_pr_number": 42,
            "context_branch": "egg/issue-3393/work",
            "context_title": "legacy title",
            "context_description": "legacy description",
        }
        contract = Contract.model_validate(data)
        dumped_pr = contract.model_dump()["pr"]
        assert dumped_pr["title"] == "keep me"
        assert dumped_pr["context_pr_number"] == 42
        assert "context_branch" not in dumped_pr
        assert "context_title" not in dumped_pr
        assert "context_description" not in dumped_pr


class TestFullMigrationChainComposes:
    """Every legacy version composes through to 1.4 in one load (AC-c)."""

    def test_absent_version_migrates_to_1_4(self) -> None:
        contract = Contract.model_validate(_payload(None))
        assert contract.schemaVersion == "1.4"

    def test_1_0_migrates_to_1_4(self) -> None:
        contract = Contract.model_validate(_payload("1.0"))
        assert contract.schemaVersion == "1.4"

    def test_1_1_migrates_to_1_4(self) -> None:
        contract = Contract.model_validate(_payload("1.1"))
        assert contract.schemaVersion == "1.4"

    def test_1_2_migrates_to_1_4(self) -> None:
        contract = Contract.model_validate(_payload("1.2"))
        assert contract.schemaVersion == "1.4"

    def test_1_3_migrates_to_1_4(self) -> None:
        contract = Contract.model_validate(_payload("1.3"))
        assert contract.schemaVersion == "1.4"

    def test_future_version_is_not_downgraded_through_the_chain(self) -> None:
        contract = Contract.model_validate(_payload("2.0"))
        assert contract.schemaVersion == "2.0"
