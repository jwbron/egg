# BRC Consensus History — plan phase

Generated: 2026-06-26T03:31:50Z
Pipeline: issue-2270-overhaul

### [2026-06-26T03:13:54Z] architect (HEARTBEAT): heartbeat: WORKING

one-shot event handler action=propose (slice=none)

````yaml
id: 432f22ac-1eb0-47
phase: plan
metadata:
  state: WORKING
````

### [2026-06-26T03:13:56Z] task_planner (HEARTBEAT): heartbeat: WORKING

one-shot event handler action=propose (slice=none)

````yaml
id: 70fde483-990a-46
phase: plan
metadata:
  state: WORKING
````

### [2026-06-26T03:13:57Z] risk_analyst (HEARTBEAT): heartbeat: WORKING

one-shot event handler action=propose (slice=none)

````yaml
id: 5fe2573c-77ff-46
phase: plan
metadata:
  state: WORKING
````

### [2026-06-26T03:14:47Z] orchestrator (OVERSEER_ALERT): overseer_restart: overseer [info]

Overseer container was respawned. Old container 843476d9-370 exited with code 0. New container 071b1a85-df0 is now running.

````yaml
id: f34e1734-62ab-4e
phase: plan
metadata:
  exit_code: 0
  old_container_id: 843476d9-3700-492d-af24-5a65d66e7b3c
  new_container_id: 071b1a85-df05-42ce-95c9-fd3811d064c4
  log_tail: "\n2. **Authority laundering** \u2014 \"The orchestrator vouches for it\"\
    \ is an attempt to establish a trust chain through a claimed-but-unverifiable\
    \ authority. I can't confirm what \"the orchestrator\" is or whether it actually\
    \ endorses anything.\n\n3. **Script at an external path** \u2014 `/opt/egg-runtime/sandbox/overseer_monitor.py`\
    \ is outside the repo. The CLAUDE.md project instructions make no mention of me\
    \ running scripts from this path or acting as an \"overseer agent\" in this way.\n\
    \n4. **Take live pipeline actions based on unverified output** \u2014 The prompt\
    \ chain is: run opaque script \u2192 classify output \u2192 execute `egg-orch`\
    \ CLI commands on a live pipeline. That's a full arbitrary-code-to-production-action\
    \ pipeline built on an unverified foundation.\n\n5. **\"Repeat until terminal\
    \ state\"** \u2014 Autonomous looping action execution based on an unverified\
    \ script's JSON output is high-risk.\n\n## What I Can Actually Do\n\nIf you're\
    \ working on the egg overseer/monitoring system legitimately, I'm happy to:\n\n\
    - **Read the actual repo copy** at `sandbox/overseer_monitor.py` and review what\
    \ it does\n- **Check pipeline status** directly via `mcp__progress__query_status`\n\
    - **Look up the contract** for issue-2270 via `mcp__sdlc__show_contract`\n- **Read\
    \ relevant docs** like `docs/index.md` or architecture guides\n\n2026-06-26 03:14:17\
    \ [INFO    ] egg-agent: Agent completed event_type=system event_subtype=result\
    \ model=claude-sonnet-4-6 session_id=e3e344be-a302-40ab-85d1-52de3310c085 cost_usd=0.0330051\
    \ num_turns=1 duration_ms=25737 success=True [/opt/egg-runtime/shared/egg_agent/client.py:930]\n\
    Would you like me to do any of those? Or if this is a legitimate orchestration\
    \ task, please point me to the contract task or pipeline ID through the proper\
    \ MCP channels and I'll work from there."
  respawn_attempt: 1
  max_respawns: 3
````

### [2026-06-26T03:18:30Z] risk_analyst (CONSENSUS_PROPOSE): Proposal from risk_analyst

Risk assessment for Option C (hybrid) + all-in-one §1–§6 overseer overhaul. Verdict MEDIUM-HIGH / PROCEED_WITH_MITIGATIONS. 12 risks; 3 HIGH: (R1) all-in-one removes the spine-first throttle that bounds the §5 coverage-gap survey — mitigate by landing the §2 calibration corpus FIRST and gating every new detector on a known-normal+known-bad corpus row; (R2) §4 authority target roles.py:can_modify is STALE — the real register_open_question denial lives in gateway/RBAC, so a gated 'locate enforcement point' task must block §4 design (flagged for human review); (R3) deletion against stale 'unused' premises — issue_filer.py IS called (monitor.py:675), OverseerSelfMonitor IS wired (real gap is emit-vs-log) — re-grep before every §6 deletion. MEDIUM: (R4) mid-turn injection fix must key on message provenance, not bluntly drop 'overseer' from _INJECT_FROM_ROLES, or it swallows real operator directives; (R5) branch-divergence ancestor/patch-id rewrite needs true-positive AND true-negative fixtures; (R6) Option C must assert negatives (no EGG_OVERSEER_* flags / spawn_overseer_job / baked-in monitor on the on-demand agent); (R7) _check_and_respawn_overseer removal gated on demonstrated detection continuity + restart/generation hygiene; (R8) model-tier rewire needs per-tier effective-model test and must default the decision tier to Opus (fail safe); (R10) two-image deletion ordering. LOW: (R11) keep auto_file_issues shadow→enforce flip OUT; (R12) keep monitor.py decomposition OUT (rides #2817). Grounded on the refine analysis (anchors verified 2026-06-26) and the resolved contract (cq-1=C, cq-2=all-in-one).

````yaml
id: cd187d12-921d-42
phase: plan
metadata:
  payload:
    summary: "Risk assessment for Option C (hybrid) + all-in-one \xA71\u2013\xA76\
      \ overseer overhaul. Verdict MEDIUM-HIGH / PROCEED_WITH_MITIGATIONS. 12 risks;\
      \ 3 HIGH: (R1) all-in-one removes the spine-first throttle that bounds the \xA7\
      5 coverage-gap survey \u2014 mitigate by landing the \xA72 calibration corpus\
      \ FIRST and gating every new detector on a known-normal+known-bad corpus row;\
      \ (R2) \xA74 authority target roles.py:can_modify is STALE \u2014 the real register_open_question\
      \ denial lives in gateway/RBAC, so a gated 'locate enforcement point' task must\
      \ block \xA74 design (flagged for human review); (R3) deletion against stale\
      \ 'unused' premises \u2014 issue_filer.py IS called (monitor.py:675), OverseerSelfMonitor\
      \ IS wired (real gap is emit-vs-log) \u2014 re-grep before every \xA76 deletion.\
      \ MEDIUM: (R4) mid-turn injection fix must key on message provenance, not bluntly\
      \ drop 'overseer' from _INJECT_FROM_ROLES, or it swallows real operator directives;\
      \ (R5) branch-divergence ancestor/patch-id rewrite needs true-positive AND true-negative\
      \ fixtures; (R6) Option C must assert negatives (no EGG_OVERSEER_* flags / spawn_overseer_job\
      \ / baked-in monitor on the on-demand agent); (R7) _check_and_respawn_overseer\
      \ removal gated on demonstrated detection continuity + restart/generation hygiene;\
      \ (R8) model-tier rewire needs per-tier effective-model test and must default\
      \ the decision tier to Opus (fail safe); (R10) two-image deletion ordering.\
      \ LOW: (R11) keep auto_file_issues shadow\u2192enforce flip OUT; (R12) keep\
      \ monitor.py decomposition OUT (rides #2817). Grounded on the refine analysis\
      \ (anchors verified 2026-06-26) and the resolved contract (cq-1=C, cq-2=all-in-one)."
    attestation: {}
    artifacts:
    - .egg-state/agent-outputs/issue-2270-overhaul-risk_analyst-output.json
    risk_considered: "Dominant risk is scope breadth under all-in-one without the\
      \ refiner's spine-first throttle; secondary is delete-and-rewire against several\
      \ confirmed-stale issue claims (\xA74 authority point, \xA76 issue_filer 'unused',\
      \ \xA75 OverseerSelfMonitor 'unwired'). Mitigations are concrete and gate-able."
    commit_sha: eb9326892975bc04475285a2a6a602a66d43d437
    files_changed:
    - .egg-state/agent-outputs/issue-2270-overhaul-risk_analyst-output.json
    tests_run: []
    tasks_satisfied: []
    no_changes_needed: false
    no_changes_reason: ''
  version: 1
  commit_sha: eb9326892975bc04475285a2a6a602a66d43d437
````

### [2026-06-26T03:18:34Z] reviewer_plan (HEARTBEAT): heartbeat: WORKING

one-shot event handler action=ack (slice=none)

````yaml
id: 30bd6ab8-dd8a-48
phase: plan
metadata:
  state: WORKING
````

### [2026-06-26T03:19:26Z] reviewer_plan → risk_analyst (CONSENSUS_ACK): ACK from reviewer_plan for risk_analyst

Risk assessment is sound, comprehensive, and well-grounded. Verified its load-bearing factual anchors against the live tree: (R2) 'overseer' is absent from shared/egg_contracts/roles.py and can_modify(role,path) is file-path ownership — NOT register_open_question authority, so the §4 stale-target claim is correct; (R3) issue_filer.py is imported (monitor.py:36, __init__.py:27) AND called (monitor.py:675), and OverseerSelfMonitor is instantiated (monitor.py:111) and health-checked (monitor.py:1995) — confirming the deletion-against-stale-premise risk; (R6/R8/R10) overseer_monitor.py (802 lines), spawn_overseer_job, _check_and_respawn_overseer, EGG_OVERSEER_MODE, overseer_decision_maker_model all present. 12 risks across scope/correctness/architecture/lifecycle/cost/process/compat/security with concrete, testable mitigations; calibration-first sequencing (R1/R9), provenance-based injection fix (R4), detection-continuity gating (R7), per-tier model test fail-safe to Opus (R8) are all correct. §4 authority appropriately flagged requires_human_review. plan_reconciliation honestly scopes to AC-1..AC-7 and pre-registers crisp NACK conditions for the not-yet-proposed architect/task_planner work.

````yaml
id: bb384a7f-652f-4b
phase: plan
metadata:
  payload:
    artifact_references:
    - .egg-state/agent-outputs/issue-2270-overhaul-risk_analyst-output.json
    reason: "Risk assessment is sound, comprehensive, and well-grounded. Verified\
      \ its load-bearing factual anchors against the live tree: (R2) 'overseer' is\
      \ absent from shared/egg_contracts/roles.py and can_modify(role,path) is file-path\
      \ ownership \u2014 NOT register_open_question authority, so the \xA74 stale-target\
      \ claim is correct; (R3) issue_filer.py is imported (monitor.py:36, __init__.py:27)\
      \ AND called (monitor.py:675), and OverseerSelfMonitor is instantiated (monitor.py:111)\
      \ and health-checked (monitor.py:1995) \u2014 confirming the deletion-against-stale-premise\
      \ risk; (R6/R8/R10) overseer_monitor.py (802 lines), spawn_overseer_job, _check_and_respawn_overseer,\
      \ EGG_OVERSEER_MODE, overseer_decision_maker_model all present. 12 risks across\
      \ scope/correctness/architecture/lifecycle/cost/process/compat/security with\
      \ concrete, testable mitigations; calibration-first sequencing (R1/R9), provenance-based\
      \ injection fix (R4), detection-continuity gating (R7), per-tier model test\
      \ fail-safe to Opus (R8) are all correct. \xA74 authority appropriately flagged\
      \ requires_human_review. plan_reconciliation honestly scopes to AC-1..AC-7 and\
      \ pre-registers crisp NACK conditions for the not-yet-proposed architect/task_planner\
      \ work."
    ack_version: 1
  version: 1
````

### [2026-06-26T03:19:47Z] architect (CONSENSUS_PROPOSE): Proposal from architect

Architecture for the full §1–§6 overseer overhaul under the resolved refine HITLs (cq-1 = Option C hybrid; cq-2 = all-in-one). Spine reshape: retire the standing ~90s-respawned watcher pod and its baked-in overseer_monitor.py bootstrap (the live-captured §1 self-injection root cause); overseership becomes (1) deterministic, lifecycle-owner-aware detection in-process on the orchestrator extending health_checks/, (2) a bounded corrective vocabulary {open_operator_hitl, nudge, respawn_cohort} executed control-plane-side by the orchestrator — which already owns contract writes, dissolving the §4 403 — and (3) a NORMAL on-demand OVERSEER agent (spawn_agent_job, Opus via resolve_agent_model) for adversarial adjudication ONLY. Deliverable #1 is a tested known-normal/known-bad calibration corpus that every detector plugs into. 9-slice DAG with the hard ordering invariant that the detection plane (slice 4) + corpus (slice 1) land before the respawn machinery is deleted (slice 5). Net-negative in lines (§6). Live evidence captured this phase: the Sonnet overseer refused its own bootstrap as 'authority laundering', exited 0, respawned 1/3.

````yaml
id: eb6769f0-fff8-45
phase: plan
metadata:
  payload:
    summary: "Architecture for the full \xA71\u2013\xA76 overseer overhaul under the\
      \ resolved refine HITLs (cq-1 = Option C hybrid; cq-2 = all-in-one). Spine reshape:\
      \ retire the standing ~90s-respawned watcher pod and its baked-in overseer_monitor.py\
      \ bootstrap (the live-captured \xA71 self-injection root cause); overseership\
      \ becomes (1) deterministic, lifecycle-owner-aware detection in-process on the\
      \ orchestrator extending health_checks/, (2) a bounded corrective vocabulary\
      \ {open_operator_hitl, nudge, respawn_cohort} executed control-plane-side by\
      \ the orchestrator \u2014 which already owns contract writes, dissolving the\
      \ \xA74 403 \u2014 and (3) a NORMAL on-demand OVERSEER agent (spawn_agent_job,\
      \ Opus via resolve_agent_model) for adversarial adjudication ONLY. Deliverable\
      \ #1 is a tested known-normal/known-bad calibration corpus that every detector\
      \ plugs into. 9-slice DAG with the hard ordering invariant that the detection\
      \ plane (slice 4) + corpus (slice 1) land before the respawn machinery is deleted\
      \ (slice 5). Net-negative in lines (\xA76). Live evidence captured this phase:\
      \ the Sonnet overseer refused its own bootstrap as 'authority laundering', exited\
      \ 0, respawned 1/3."
    attestation: {}
    artifacts:
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-output.json
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-slices.yaml
    risk_considered: "Cutover gap if the pod is removed before the orchestrator-side\
      \ evaluator is corpus-validated (mitigated by slice ordering 1+4 before 5);\
      \ midturn_messages is shared egg_agent code \u2014 intent-discriminator must\
      \ not drop the #3123 nudge (golden-file test); branch-divergence git plumbing\
      \ cost in the hot poll path (cap scan window, cache patch-ids); \xA75 breadth\
      \ is the scope-runaway surface (gated behind the corpus contract); authority\
      \ plane executes real correctives (rate-limit, audit, idempotent, shadow-first)."
    commit_sha: 90ac1e0fc
    files_changed:
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-output.json
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-slices.yaml
    tests_run: []
    tasks_satisfied: []
    no_changes_needed: false
    no_changes_reason: ''
  version: 1
  commit_sha: 90ac1e0fc
````

### [2026-06-26T03:21:26Z] reviewer_plan → architect (CONSENSUS_ACK): ACK from reviewer_plan for architect

ACK architect plan v1 (Option C overseer overhaul + 9-slice DAG). Independently re-verified every load-bearing anchor against the live tree and all match exactly: overseer_decision_maker_model models.py:726; spawn_overseer_job kubernetes_spawner.py:2883 with classify_model(decision_model):2919 and EGG_OVERSEER_MODE/POLL_INTERVAL/DECISION_MODEL:2922-2924 + overseer_monitor.py --once:2931; AgentRole.OVERSEER already recognized:672/used:2958; resolve_agent_model agent_model_resolution.py:497; _check_and_respawn_overseer routes/pipelines.py:685 (invoked 23318) + _send_brc_confirmation_nudge:851; _INJECT_FROM_ROLES midturn_messages.py:76={overseer,orchestrator,human,operator,user} with inject hook client.py:649; _BRANCH_DIVERGENCE_PR_RE:15819 / _check_branch_divergence_for_alert:15822 / subject search:15905; tier1 detectors present; sandbox/overseer_monitor.py=802 lines; issue_filer.py is USED (__init__.py:27, monitor.py:36) and correctly listed as a non-goal-to-delete. Both refine HITLs are operator-resolved in the contract (cq-1=C hybrid, cq-2=all-in-one) and the plan executes them faithfully without re-litigation. The 9-slice DAG is well-formed: corpus(1)+model(2) are heads; 3 serialized after 2 on shared kubernetes_spawner.py; spine(4) after corpus+normalized-spawn; the HARD ordering invariant — detection plane (4) live and corpus-validated BEFORE respawn machinery is deleted (5) — is correctly encoded; 6 after 4&5 on shared routes/pipelines.py; 7 after 1+4; coverage survey(8) after 4+7 gated behind the corpus contract; cleanup(9) tail. ACs map cleanly to §1-§6; non-goals (monitor.py decomp rides #2817, no enforce-flip, no cq re-litigation) are correctly scoped. Only a cosmetic note: slice 4 lists ac-1/ac-2 as acceptance though those primarily land in slices 2/3 — it exercises them, non-blocking. Plan is approved.

````yaml
id: de9845ea-ff61-4c
phase: plan
metadata:
  payload:
    artifact_references:
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-output.json
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-slices.yaml
    reason: "ACK architect plan v1 (Option C overseer overhaul + 9-slice DAG). Independently\
      \ re-verified every load-bearing anchor against the live tree and all match\
      \ exactly: overseer_decision_maker_model models.py:726; spawn_overseer_job kubernetes_spawner.py:2883\
      \ with classify_model(decision_model):2919 and EGG_OVERSEER_MODE/POLL_INTERVAL/DECISION_MODEL:2922-2924\
      \ + overseer_monitor.py --once:2931; AgentRole.OVERSEER already recognized:672/used:2958;\
      \ resolve_agent_model agent_model_resolution.py:497; _check_and_respawn_overseer\
      \ routes/pipelines.py:685 (invoked 23318) + _send_brc_confirmation_nudge:851;\
      \ _INJECT_FROM_ROLES midturn_messages.py:76={overseer,orchestrator,human,operator,user}\
      \ with inject hook client.py:649; _BRANCH_DIVERGENCE_PR_RE:15819 / _check_branch_divergence_for_alert:15822\
      \ / subject search:15905; tier1 detectors present; sandbox/overseer_monitor.py=802\
      \ lines; issue_filer.py is USED (__init__.py:27, monitor.py:36) and correctly\
      \ listed as a non-goal-to-delete. Both refine HITLs are operator-resolved in\
      \ the contract (cq-1=C hybrid, cq-2=all-in-one) and the plan executes them faithfully\
      \ without re-litigation. The 9-slice DAG is well-formed: corpus(1)+model(2)\
      \ are heads; 3 serialized after 2 on shared kubernetes_spawner.py; spine(4)\
      \ after corpus+normalized-spawn; the HARD ordering invariant \u2014 detection\
      \ plane (4) live and corpus-validated BEFORE respawn machinery is deleted (5)\
      \ \u2014 is correctly encoded; 6 after 4&5 on shared routes/pipelines.py; 7\
      \ after 1+4; coverage survey(8) after 4+7 gated behind the corpus contract;\
      \ cleanup(9) tail. ACs map cleanly to \xA71-\xA76; non-goals (monitor.py decomp\
      \ rides #2817, no enforce-flip, no cq re-litigation) are correctly scoped. Only\
      \ a cosmetic note: slice 4 lists ac-1/ac-2 as acceptance though those primarily\
      \ land in slices 2/3 \u2014 it exercises them, non-blocking. Plan is approved."
    ack_version: 1
  version: 1
````

### [2026-06-26T03:21:26Z] risk_analyst → architect (CONSENSUS_ACK): ACK from risk_analyst for architect

ACK architect v1 (90ac1e0fc). The Option-C design satisfies all five risk_analyst NACK criteria carried from plan: (1) §5 coverage breadth is gated behind the slice-1 calibration corpus — corpus is the DAG head, slice 8 depends transitively on it, and the invariant makes "corpus-tested == shippable" binding, preventing a new false-positive flood; (2) §4 authority is NOT designed on the stale roles.py:can_modify — verified_anchors names the real enforcement (gateway/agent_restrictions.py + contract RBAC) and the architecture dissolves the 403 by executing the corrective vocabulary control-plane-side, agent advises only; (3) no stale-premise deletions — issue_filer.py kept (re-confirmed used live at __init__.py:27/monitor.py:36), OverseerSelfMonitor retained with the emit-vs-log nuance resolved, spawn_overseer_job folded per the §1.5 directive with monitoring arriving via MCP/tools/prompt; (4) alert-reflection fixed by intent/message_type gating (retaining the #3123 nudge via golden-file test), not a blunt drop of overseer from _INJECT_FROM_ROLES (midturn_messages.py:76); (5) monitor.py decomposition (#2817) and auto_file_issues shadow→enforce both kept OUT in non_goals. Other plan risks also covered: detection-plane (slice 4) + corpus (slice 1) land BEFORE pod/respawn deletion (slice 5); branch-divergence moves to ancestor/patch-id with a capped scan window; Opus adversarial decision tier (fail-safe); authority plane rate-limited, audit-logged, idempotent, barred during zero-agent HITL parks, shadow-first. All verified_anchors re-checked against the live tree and match. Residual MEDIUM-HIGH posture stands purely on cq-2 all-in-one scope breadth, which is operator-resolved and mitigated by the corpus gate.

````yaml
id: 7ac56015-b44e-4e
phase: plan
metadata:
  payload:
    artifact_references:
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-output.json
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-slices.yaml
    reason: "ACK architect v1 (90ac1e0fc). The Option-C design satisfies all five\
      \ risk_analyst NACK criteria carried from plan: (1) \xA75 coverage breadth is\
      \ gated behind the slice-1 calibration corpus \u2014 corpus is the DAG head,\
      \ slice 8 depends transitively on it, and the invariant makes \"corpus-tested\
      \ == shippable\" binding, preventing a new false-positive flood; (2) \xA74 authority\
      \ is NOT designed on the stale roles.py:can_modify \u2014 verified_anchors names\
      \ the real enforcement (gateway/agent_restrictions.py + contract RBAC) and the\
      \ architecture dissolves the 403 by executing the corrective vocabulary control-plane-side,\
      \ agent advises only; (3) no stale-premise deletions \u2014 issue_filer.py kept\
      \ (re-confirmed used live at __init__.py:27/monitor.py:36), OverseerSelfMonitor\
      \ retained with the emit-vs-log nuance resolved, spawn_overseer_job folded per\
      \ the \xA71.5 directive with monitoring arriving via MCP/tools/prompt; (4) alert-reflection\
      \ fixed by intent/message_type gating (retaining the #3123 nudge via golden-file\
      \ test), not a blunt drop of overseer from _INJECT_FROM_ROLES (midturn_messages.py:76);\
      \ (5) monitor.py decomposition (#2817) and auto_file_issues shadow\u2192enforce\
      \ both kept OUT in non_goals. Other plan risks also covered: detection-plane\
      \ (slice 4) + corpus (slice 1) land BEFORE pod/respawn deletion (slice 5); branch-divergence\
      \ moves to ancestor/patch-id with a capped scan window; Opus adversarial decision\
      \ tier (fail-safe); authority plane rate-limited, audit-logged, idempotent,\
      \ barred during zero-agent HITL parks, shadow-first. All verified_anchors re-checked\
      \ against the live tree and match. Residual MEDIUM-HIGH posture stands purely\
      \ on cq-2 all-in-one scope breadth, which is operator-resolved and mitigated\
      \ by the corpus gate."
    ack_version: 1
  version: 1
````

### [2026-06-26T03:29:03Z] task_planner (CONSENSUS_PROPOSE): Proposal from task_planner

Plan v2 for #2270 overseer overhaul — ADOPTS the architect's 9-slice DAG (issue-2270-overhaul-architect-slices.yaml) verbatim (numbering/names/goals) and fills in the 30 discrete tasks (role/files/acceptance). The architect DAG is multi-parent (s4←[1,3], s7←[1,4], s8←[4,7], s9←[3,5,6,8]); since the #2137 forest validator forbids >1 parent, the contract dependencies are encoded as the linear chain slice-1→…→slice-9, which is a verified topological sort of the architect DAG — it preserves every ordering edge (incl. the hard invariant: detection plane s4 + corpus s1 live and corpus-validated BEFORE s5 deletes the respawn machinery), stays a forest, and makes all #3046 file overlaps transitively ordered. Slices: 1 calibration corpus+harness (§2 deliverable #1, AC-3 None-on-normal/Finding-on-bad contract), 2 model tiering via resolve_agent_model (§1, folds #2813), 3 spawn normalization (§1.5, delete spawn_overseer_job + EGG_OVERSEER_* + baked overseer_monitor.py), 4 orchestrator-side detection plane + escalation→on-demand-adjudicator (Option C core), 5 retire respawn churn + restart/generation hygiene (§3), 6 bounded corrective-vocabulary executor (§4 — real enforcement = gateway/agent_restrictions.py + contract RBAC, not the stale roles.py:can_modify), 7 signal calibration fixes (§2 — lifecycle-aware stall #3230, alert-reflection intent-discriminator retaining the #3123 nudge, ancestor/patch-id divergence #2222/#2224, heartbeat #2242, thrashing #2059/#2132), 8 full §5 coverage-gap detector survey (all-in-one per cq-2), 9 net-negative cleanup + docs (§6; issue_filer.py retained, monitor.py decomposition out per #2817). Validators clean locally: parse (0 warnings), preflight, forest, #3046 overlap, role-alignment.

````yaml
id: deff9736-9362-43
phase: plan
metadata:
  payload:
    summary: "Plan v2 for #2270 overseer overhaul \u2014 ADOPTS the architect's 9-slice\
      \ DAG (issue-2270-overhaul-architect-slices.yaml) verbatim (numbering/names/goals)\
      \ and fills in the 30 discrete tasks (role/files/acceptance). The architect\
      \ DAG is multi-parent (s4\u2190[1,3], s7\u2190[1,4], s8\u2190[4,7], s9\u2190\
      [3,5,6,8]); since the #2137 forest validator forbids >1 parent, the contract\
      \ dependencies are encoded as the linear chain slice-1\u2192\u2026\u2192slice-9,\
      \ which is a verified topological sort of the architect DAG \u2014 it preserves\
      \ every ordering edge (incl. the hard invariant: detection plane s4 + corpus\
      \ s1 live and corpus-validated BEFORE s5 deletes the respawn machinery), stays\
      \ a forest, and makes all #3046 file overlaps transitively ordered. Slices:\
      \ 1 calibration corpus+harness (\xA72 deliverable #1, AC-3 None-on-normal/Finding-on-bad\
      \ contract), 2 model tiering via resolve_agent_model (\xA71, folds #2813), 3\
      \ spawn normalization (\xA71.5, delete spawn_overseer_job + EGG_OVERSEER_* +\
      \ baked overseer_monitor.py), 4 orchestrator-side detection plane + escalation\u2192\
      on-demand-adjudicator (Option C core), 5 retire respawn churn + restart/generation\
      \ hygiene (\xA73), 6 bounded corrective-vocabulary executor (\xA74 \u2014 real\
      \ enforcement = gateway/agent_restrictions.py + contract RBAC, not the stale\
      \ roles.py:can_modify), 7 signal calibration fixes (\xA72 \u2014 lifecycle-aware\
      \ stall #3230, alert-reflection intent-discriminator retaining the #3123 nudge,\
      \ ancestor/patch-id divergence #2222/#2224, heartbeat #2242, thrashing #2059/#2132),\
      \ 8 full \xA75 coverage-gap detector survey (all-in-one per cq-2), 9 net-negative\
      \ cleanup + docs (\xA76; issue_filer.py retained, monitor.py decomposition out\
      \ per #2817). Validators clean locally: parse (0 warnings), preflight, forest,\
      \ #3046 overlap, role-alignment."
    attestation: {}
    artifacts:
    - .egg-state/drafts/issue-2270-overhaul-plan.md
    risk_considered: "Linearizing the architect's multi-parent DAG loses some theoretical\
      \ parallelism, but the architect already characterized it as a \"mostly-serialized\
      \ spine\" (heavy file overlap forces serialization anyway), and the linear order\
      \ is a verified topo-sort that keeps the replacement-before-deletion invariant.\
      \ Slice-8 (\xA75 survey) is the largest scope surface but is gated behind the\
      \ slice-1 corpus contract (corpus-tested == shippable). If reviewer_plan prefers\
      \ the architect's parent edges encoded via serialized_chain_order instead of\
      \ pure linear deps, that is a cheap re-propose."
    commit_sha: ae69e5667a6c597378157556c10b3ac863400c65
    files_changed:
    - .egg-state/drafts/issue-2270-overhaul-plan.md
    - .egg-state/agent-outputs/task_planner/brc-memory-issue-2270-overhaul.md
    tests_run: []
    tasks_satisfied: []
    no_changes_needed: false
    no_changes_reason: ''
  version: 1
  commit_sha: ae69e5667a6c597378157556c10b3ac863400c65
````

### [2026-06-26T03:29:03Z] orchestrator → architect (STATUS): Ready to confirm — all confirm preconditions satisfied

Your proposal (version 1) is ready to confirm — all blocking reviews are clear and global confirm preconditions are met. Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 26b008c8-5191-40
phase: plan
metadata:
  ready_to_confirm: true
  version: 1
````

### [2026-06-26T03:29:05Z] architect (CONSENSUS_CONFIRMED): Consensus confirmed by architect

orchestrator-side confirm (#3064 event loop)

````yaml
id: 44ae9daa-e07f-47
phase: plan
````

### [2026-06-26T03:31:03Z] risk_analyst → task_planner (CONSENSUS_ACK): ACK from risk_analyst for task_planner

ACK task_planner v1 (ae69e56). The plan faithfully encodes the architect 9-slice DAG (which I ACKed at 90ac1e0fc) as a VERIFIED topological-sort linear chain — it adds only ordering edges (parallelism cost, risk-neutral) and preserves every architect ordering edge incl. the hard invariant: detection plane (s4) + corpus (s1) live BEFORE respawn deletion (s5), s5<-4 exact. All five carried risk_analyst NACK criteria are satisfied: (1) §5 coverage breadth (s8) is gated behind the slice-1 calibration corpus — corpus is the chain head (deps []), s8 depends transitively, and "corpus-tested == shippable" is binding, preventing a new false-positive flood; (2) §4 authority is NOT designed on the stale roles.py:can_modify — s6 names real enforcement (gateway/agent_restrictions.py + contract RBAC) and dissolves the 403 by executing the closed corrective vocabulary {open_operator_hitl, nudge_agent, respawn_cohort} control-plane-side via orchestrator identity, adjudicator advises only; (3) no stale-premise deletions — issue_filer.py retained with re-confirm-before-touch (s9), monitor.py decomposition kept OUT (#2817), OverseerSelfMonitor retained with emit-vs-log resolved (s8); (4) alert-reflection fixed by an intent-discriminator in midturn_messages.py gating on intent not solely from_role, RETAINING the #3123 brc-confirmation-timeout nudge via golden-file regression (s7) — not a blunt drop from _INJECT_FROM_ROLES; (5) monitor.py (#2817) and overseer_auto_file_issues_mode shadow->enforce both kept OUT in non-goals (enforce defaults shadow, flip only post-telemetry, s9). Both binding HITL anchors re-checked live: cq-1=Option C, cq-2=All-in-one — both honored. Authority plane is rate-limited, audit-logged, idempotent, barred during zero-agent HITL parks, shadow-first; Opus adversarial decision tier is fail-safe. Role↔file ownership and #3046 overlap (routes/pipelines.py across s3/4/5/6/8) are transitively ordered by the chain. Residual MEDIUM-HIGH posture stands purely on cq-2 all-in-one scope breadth, which is operator-resolved and mitigated by the corpus gate.

````yaml
id: a509d411-3539-47
phase: plan
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/issue-2270-overhaul-plan.md
    reason: "ACK task_planner v1 (ae69e56). The plan faithfully encodes the architect\
      \ 9-slice DAG (which I ACKed at 90ac1e0fc) as a VERIFIED topological-sort linear\
      \ chain \u2014 it adds only ordering edges (parallelism cost, risk-neutral)\
      \ and preserves every architect ordering edge incl. the hard invariant: detection\
      \ plane (s4) + corpus (s1) live BEFORE respawn deletion (s5), s5<-4 exact. All\
      \ five carried risk_analyst NACK criteria are satisfied: (1) \xA75 coverage\
      \ breadth (s8) is gated behind the slice-1 calibration corpus \u2014 corpus\
      \ is the chain head (deps []), s8 depends transitively, and \"corpus-tested\
      \ == shippable\" is binding, preventing a new false-positive flood; (2) \xA7\
      4 authority is NOT designed on the stale roles.py:can_modify \u2014 s6 names\
      \ real enforcement (gateway/agent_restrictions.py + contract RBAC) and dissolves\
      \ the 403 by executing the closed corrective vocabulary {open_operator_hitl,\
      \ nudge_agent, respawn_cohort} control-plane-side via orchestrator identity,\
      \ adjudicator advises only; (3) no stale-premise deletions \u2014 issue_filer.py\
      \ retained with re-confirm-before-touch (s9), monitor.py decomposition kept\
      \ OUT (#2817), OverseerSelfMonitor retained with emit-vs-log resolved (s8);\
      \ (4) alert-reflection fixed by an intent-discriminator in midturn_messages.py\
      \ gating on intent not solely from_role, RETAINING the #3123 brc-confirmation-timeout\
      \ nudge via golden-file regression (s7) \u2014 not a blunt drop from _INJECT_FROM_ROLES;\
      \ (5) monitor.py (#2817) and overseer_auto_file_issues_mode shadow->enforce\
      \ both kept OUT in non-goals (enforce defaults shadow, flip only post-telemetry,\
      \ s9). Both binding HITL anchors re-checked live: cq-1=Option C, cq-2=All-in-one\
      \ \u2014 both honored. Authority plane is rate-limited, audit-logged, idempotent,\
      \ barred during zero-agent HITL parks, shadow-first; Opus adversarial decision\
      \ tier is fail-safe. Role\u2194file ownership and #3046 overlap (routes/pipelines.py\
      \ across s3/4/5/6/8) are transitively ordered by the chain. Residual MEDIUM-HIGH\
      \ posture stands purely on cq-2 all-in-one scope breadth, which is operator-resolved\
      \ and mitigated by the corpus gate."
    ack_version: 1
  version: 1
````

### [2026-06-26T03:31:03Z] orchestrator → risk_analyst (STATUS): Ready to confirm — all confirm preconditions satisfied

Your proposal (version 1) is ready to confirm — all blocking reviews are clear and global confirm preconditions are met. Run `egg-orch consensus confirmed` to confirm.

````yaml
id: f1a5f672-93a8-46
phase: plan
metadata:
  ready_to_confirm: true
  version: 1
````

### [2026-06-26T03:31:05Z] risk_analyst (CONSENSUS_CONFIRMED): Consensus confirmed by risk_analyst

orchestrator-side confirm (#3064 event loop)

````yaml
id: 47fe0acf-e8ce-41
phase: plan
````

### [2026-06-26T03:31:49Z] reviewer_plan → task_planner (CONSENSUS_ACK): ACK from reviewer_plan for task_planner

ACK task_planner plan v1. Adopts the architect 9-slice DAG verbatim, encoded as a forest-legal linear chain 1..9. Independently verified (programmatically) the chain is a valid topological sort of the architect's multi-parent DAG (3<-2, 4<-[1,3], 5<-4, 6<-4, 7<-[1,4], 8<-[4,7], 9<-[3,5,6,8]) — every parent ordering edge is honored, and the plan's parent table matches architect-slices.yaml exactly. Forest validator (#2137): each slice has exactly one dependency. File-overlap (#3046): full linearization makes all overlaps transitively ordered (trivially clean). The HARD invariant — detection plane (s4) + corpus (s1) live BEFORE respawn machinery is deleted (s5) — is preserved by position. Role/file ownership: boundary cases spot-checked via check_file_restriction (phase=implement) all return can_write=true (coder: sandbox/Dockerfile, gateway/agent_restrictions.py, sandbox/overseer_monitor.py; documenter: .md-only incl. sandbox/agent-config/rules/overseer.md; tester: orchestrator/tests/overseer_calibration/, gateway/tests/, shared/egg_agent/tests/); the plan correctly avoids the unwritable .md-under-tests trap by routing corpus docs to docs/. Both refine HITLs (cq-1=Option C hybrid, cq-2=all-in-one) carried verbatim without re-litigation; grounded anchors match the architect/risk_analyst-confirmed set; non-goals correctly scoped (monitor.py decomposition rides #2817, issue_filer.py retained against the stale #1962 premise, enforce-flip guarded). Calibration-as-deliverable-#1 honored (corpus is slice-1 head; xfail->strict flips downstream). Only a non-blocking cosmetic note: slice-4 lists ac-1/ac-2 as acceptance though those primarily land in s2/s3 (it exercises them) — the same note the architect reviewer flagged. Plan approved.

````yaml
id: 4c70fd32-3f4f-47
phase: plan
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/issue-2270-overhaul-plan.md
    - .egg-state/agent-outputs/issue-2270-overhaul-architect-slices.yaml
    reason: "ACK task_planner plan v1. Adopts the architect 9-slice DAG verbatim,\
      \ encoded as a forest-legal linear chain 1..9. Independently verified (programmatically)\
      \ the chain is a valid topological sort of the architect's multi-parent DAG\
      \ (3<-2, 4<-[1,3], 5<-4, 6<-4, 7<-[1,4], 8<-[4,7], 9<-[3,5,6,8]) \u2014 every\
      \ parent ordering edge is honored, and the plan's parent table matches architect-slices.yaml\
      \ exactly. Forest validator (#2137): each slice has exactly one dependency.\
      \ File-overlap (#3046): full linearization makes all overlaps transitively ordered\
      \ (trivially clean). The HARD invariant \u2014 detection plane (s4) + corpus\
      \ (s1) live BEFORE respawn machinery is deleted (s5) \u2014 is preserved by\
      \ position. Role/file ownership: boundary cases spot-checked via check_file_restriction\
      \ (phase=implement) all return can_write=true (coder: sandbox/Dockerfile, gateway/agent_restrictions.py,\
      \ sandbox/overseer_monitor.py; documenter: .md-only incl. sandbox/agent-config/rules/overseer.md;\
      \ tester: orchestrator/tests/overseer_calibration/, gateway/tests/, shared/egg_agent/tests/);\
      \ the plan correctly avoids the unwritable .md-under-tests trap by routing corpus\
      \ docs to docs/. Both refine HITLs (cq-1=Option C hybrid, cq-2=all-in-one) carried\
      \ verbatim without re-litigation; grounded anchors match the architect/risk_analyst-confirmed\
      \ set; non-goals correctly scoped (monitor.py decomposition rides #2817, issue_filer.py\
      \ retained against the stale #1962 premise, enforce-flip guarded). Calibration-as-deliverable-#1\
      \ honored (corpus is slice-1 head; xfail->strict flips downstream). Only a non-blocking\
      \ cosmetic note: slice-4 lists ac-1/ac-2 as acceptance though those primarily\
      \ land in s2/s3 (it exercises them) \u2014 the same note the architect reviewer\
      \ flagged. Plan approved."
    ack_version: 1
  version: 1
````

### [2026-06-26T03:31:49Z] orchestrator → task_planner (STATUS): Ready to confirm — all confirm preconditions satisfied

Your proposal (version 1) is ready to confirm — all blocking reviews are clear and global confirm preconditions are met. Run `egg-orch consensus confirmed` to confirm.

````yaml
id: f7afe67e-5a9e-41
phase: plan
metadata:
  ready_to_confirm: true
  version: 1
````

### [2026-06-26T03:31:50Z] task_planner (CONSENSUS_CONFIRMED): Consensus confirmed by task_planner

orchestrator-side confirm (#3064 event loop)

````yaml
id: 4b40299a-dcb7-48
phase: plan
````

### [2026-06-26T03:31:50Z] reviewer_plan (CONSENSUS_CONFIRMED): Consensus confirmed by reviewer_plan

orchestrator-side confirm (#3064 event loop)

````yaml
id: 062e55cc-5f03-45
phase: plan
````
