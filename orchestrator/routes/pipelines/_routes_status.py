"""status-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _get_pipeline_status_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Get pipeline status summary.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "id": "issue-123",
                "status": "running",
                "current_phase": "implement",
                "pending_decisions": 0
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    # Validate ``slice_id`` BEFORE the StateStore disk read in
    # ``_resolve_pipeline`` — a malformed value is going to 400 anyway,
    # and the read is wasted (#2764 review). ``InvalidPipelineIdError``
    # / ``PipelineNotFoundError`` from ``_resolve_pipeline`` still
    # naturally take precedence on the happy path: this validator only
    # fires when a slice scope is supplied at all.
    raw_slice_id = _pkg.request.args.get("slice_id")
    try:
        status_slice_id = _pkg.extract_slice_id(
            {"slice_id": raw_slice_id} if raw_slice_id is not None else {}
        )
    except ValueError as e:
        return _pkg.make_error_response(str(e), status_code=400)

    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)

        pending = pipeline.get_pending_decisions()

        data = {
            "id": pipeline.id,
            "status": pipeline.status.value,
            "current_phase": pipeline.current_phase.value,
            "pending_decisions": len(pending),
            "updated_at": pipeline.updated_at.isoformat(),
        }

        # Include first pending decision details so the collaborator
        # doesn't need a second round-trip to fetch it
        if pending:
            d = pending[0]
            data["pending_decision"] = {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "created_at": d.created_at.isoformat(),
            }

        # Include PR info once the PR phase has created a PR (#1625) so
        # monitoring clients don't need to scrape `gh pr list` by title.
        pr_url, pr_number = _pkg._get_pr_info(pipeline)
        if pr_url:
            data["pr_url"] = pr_url
            if pr_number is not None:
                data["pr_number"] = pr_number

        # Include concurrent execution monitoring when enabled. The
        # ``?slice_id=`` query param (validated above before the
        # StateStore read) scopes the consensus block to one slice's
        # BRC tracker in a slice-DAG implement phase (#2761); without
        # it, only pipeline-level consensus is reported.
        concurrent_data = _pkg._get_concurrent_status(pipeline, slice_id=status_slice_id)
        if concurrent_data:
            data["concurrent"] = concurrent_data

        # Surface the orchestrator-process-wide slice-admission state
        # (#2241 gap 1) so operators can see when slices are queued
        # behind the global cap rather than wedged. The shape is
        # {cap, admitted, admitted_keys}; ``admitted_keys`` lists
        # ``"<pipeline_id>/<slice_id>"`` so the operator can tell
        # which slices currently hold the budget.
        try:
            try:
                from orchestrator import global_slice_admit
            except ImportError:
                import global_slice_admit  # type: ignore[no-redef]

            data["slice_admit"] = global_slice_admit.snapshot()
        except Exception:  # noqa: BLE001
            # Defensive: never let admit-state collection crash the
            # status endpoint — the cap is advisory, not load-bearing
            # for the pipeline's own progress.
            pass

        # Issue #1962 TASK-1-2: include the overseer-relevant config
        # subset in the status payload so the sandbox-side overseer
        # monitor can read PipelineConfig values (advisor model,
        # threshold knobs, host-detection flag) without a separate
        # endpoint. Only the new + load-bearing knobs are exposed
        # here to keep the response compact; full config is available
        # via the dedicated config endpoint.
        try:
            cfg = getattr(pipeline, "config", None)
            if cfg is not None:
                data["config"] = {
                    "overseer_advisor_model": getattr(cfg, "overseer_advisor_model", None),
                    "overseer_advisor_recent_log_bytes_cap": getattr(
                        cfg, "overseer_advisor_recent_log_bytes_cap", None
                    ),
                    "overseer_auto_file_issues_mode": getattr(
                        cfg, "overseer_auto_file_issues_mode", None
                    ),
                    "overseer_owns_host_detection": getattr(
                        cfg, "overseer_owns_host_detection", False
                    ),
                    "overseer_stuck_phase_transition_seconds": getattr(
                        cfg, "overseer_stuck_phase_transition_seconds", 180
                    ),
                    "overseer_agent_stall_seconds": getattr(
                        cfg, "overseer_agent_stall_seconds", 180
                    ),
                    "overseer_silent_agent_threshold_seconds": getattr(
                        cfg, "overseer_silent_agent_threshold_seconds", 600
                    ),
                    "overseer_long_running_phase_seconds": getattr(
                        cfg, "overseer_long_running_phase_seconds", 3600
                    ),
                    "overseer_nack_unresolved_seconds": getattr(
                        cfg, "overseer_nack_unresolved_seconds", 180
                    ),
                }
        except AttributeError, TypeError:
            # Defensive: never let a config-shape change crash the
            # status endpoint.
            pass

        return _pkg.make_success_response("Status retrieved", data=data)

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


def _wait_pipeline_status_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """Block up to ``wait`` seconds on the next pipeline-relevant event.

    Query params:
        wait: seconds to block, default 25, clamped to
              ``GET_STATUS_MAX_WAIT`` (25) so the caller stays
              safely inside the Claude Code MCP tool-call timeout.
        since: opaque cursor ``msg:<id>|evt:<seq>`` from a prior
              response.  An empty / missing cursor snaps to the tip
              on both sources (first-call semantics).  Returns 400
              if the cursor is syntactically malformed.

    Responses:
        200 — either a ``changed=true`` envelope (event or message
              fired before the timeout) or a ``changed=false,
              no_change=true`` envelope (timeout elapsed with no
              pipeline-relevant event).  Always carries ``cursor``
              so the caller can seed the next request.
        400 — malformed cursor or malformed ``wait``.
        404 — pipeline does not exist.

    Implementation:
        * ``queue.Queue(maxsize=16)`` coordinates the two sources:
          a wildcard EventBus handler (synchronous) and a daemon
          thread running ``message_store.get_messages(wait=...)``.
        * First source wins.  On return the EventBus handler is
          unsubscribed in ``finally``; the daemon thread is left
          lame-duck for up to ``wait`` seconds (accepted per plan
          risk R14 — bounded, non-blocking on shutdown).
        * ``egg_inflight_host_waits`` gauge is incremented at entry
          and decremented on return.

    Args:
        pipeline_id: Pipeline ID from the URL.
    """
    # Validate pipeline exists before doing any expensive setup.
    repo_path = _pkg.get_repo_path()
    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    # Parse + clamp ``wait``.  ``GET_STATUS_MAX_WAIT`` lives in
    # ``mcp_server`` — importing it here keeps the cap in one place.
    try:
        from mcp_server import GET_STATUS_MAX_WAIT
    except ImportError:
        try:
            from ..mcp_server import GET_STATUS_MAX_WAIT  # type: ignore[no-redef]
        except ImportError:
            GET_STATUS_MAX_WAIT = 25  # conservative fallback
    try:
        requested_wait = int(_pkg.request.args.get("wait", str(GET_STATUS_MAX_WAIT)))
    except ValueError, TypeError:
        return _pkg.make_error_response(
            "Invalid 'wait' query parameter: must be an integer",
            status_code=400,
        )
    timeout = min(max(requested_wait, 1), GET_STATUS_MAX_WAIT)

    # Parse the opaque compound cursor.  ``ok=False`` is the only
    # 400 path here — unknown cursors on either source are tolerated
    # and degrade to "snap to tip".
    ok, msg_since_id, event_since_seq = _pkg._parse_status_wait_cursor(
        _pkg.request.args.get("since")
    )
    if not ok:
        return _pkg.make_error_response(
            "Invalid 'since' cursor — expected 'msg:<id>|evt:<seq>' (either half may be empty).",
            status_code=400,
        )

    # Lazy imports keep the route cheap to load at module import and
    # match the pattern used elsewhere in this file.  We compare events
    # against ``_STATUS_WAIT_EVENT_TYPES`` by the string value of
    # ``event.event_type`` — the ``EventType`` class itself is not
    # needed here.
    try:
        from events import get_event_bus
    except ImportError:  # pragma: no cover
        try:
            from ..events import get_event_bus  # type: ignore[no-redef]
        except ImportError:
            return _pkg.make_error_response("Event bus not available", status_code=500)

    try:
        from routes.messages import _apply_delphi_filter as _delphi
    except ImportError:  # pragma: no cover
        try:
            from ..messages import _apply_delphi_filter as _delphi  # type: ignore[no-redef]
        except ImportError:
            _delphi = None  # type: ignore[assignment]

    import queue as _queue

    event_bus = get_event_bus()

    # Synchronous up-front cursor-staleness probe (issue #2464). The route
    # used to silently keep re-emitting ``msg_since_id`` whenever the store
    # tip was empty (post-phase-clear), so a polling client kept feeding
    # the dead cursor back forever. Probe once at entry with ``wait=0`` so
    # we can both stop re-emitting it and surface ``since_id_stale: True``
    # in the envelope, letting consumers (sandbox CLI cursor file, agent
    # wait_loop) drop the stale cursor and re-snap to tip. Done before
    # the terminal short-circuit below so a request that arrives after
    # both a phase clear and pipeline completion still sees the flag.
    since_id_stale = False
    if msg_since_id is not None:
        try:
            store_fn = _pkg._get_message_store()
            store = store_fn()
            _msgs, meta = store.get_messages_with_meta(
                pipeline_id,
                since_id=msg_since_id,
                limit=1,
                wait=0,
                # Suppress the "since_id not found in store" warning on
                # this probe so a single ``/status/wait`` request that
                # hits a stale cursor doesn't double-log: the long-poll
                # daemon below makes its own ``get_messages`` call with
                # the same cursor and emits the warning once. Pre-PR
                # cadence was one warning per request; we preserve that.
                _suppress_stale_warning=True,
            )
            since_id_stale = meta.since_id_stale
        except Exception as exc:  # pragma: no cover
            _pkg.logger.debug(
                "status_wait staleness probe error",
                pipeline_id=pipeline_id,
                error=str(exc),
            )

    # Late-subscriber short-circuit (issue #2378): if the pipeline is
    # already terminal at request time, the relevant ``pipeline.*``
    # event was emitted before this call could subscribe — and the
    # snap-to-tip below would cement that miss.  Synthesize a Path-A
    # envelope so callers don't loop until the 1-hour cap.  This covers
    # the common path where ``mark FAILED`` succeeds; the synthetic
    # emit at ``_run_pipeline``'s mark-FAILED-failed branch covers the
    # rarer case where the FAILED-mark itself raises.
    _TERMINAL_EVENT_TYPES = {
        _pkg.PipelineStatus.COMPLETE: "pipeline.completed",
        _pkg.PipelineStatus.FAILED: "pipeline.failed",
        _pkg.PipelineStatus.CANCELLED: "pipeline.cancelled",
    }
    if pipeline.status in _TERMINAL_EVENT_TYPES:
        # Issue #2464: don't fall back to ``msg_since_id`` when the tip
        # is empty — that's exactly the post-clear state that perpetuates
        # the dead cursor.
        terminal_cursor = _pkg._build_status_wait_cursor(
            _pkg._message_store_tip_id(pipeline_id),
            event_bus.current_sequence(),
        )
        terminal_envelope = _pkg._build_minimal_status_envelope(pipeline, terminal_cursor)
        terminal_envelope.update(
            {
                "changed": True,
                "trigger": "event",
                "event_type": _TERMINAL_EVENT_TYPES[pipeline.status],
            }
        )
        if since_id_stale:
            terminal_envelope["since_id_stale"] = True
        return _pkg.make_success_response("Pipeline already terminal", data=terminal_envelope)

    # Snap event_since_seq to the current tip on first call.  This
    # preserves the "events before the call are already seen"
    # semantic and matches the message-bus ``from_tip`` behaviour
    # used by ``/messages/wait`` (issue #1925).
    if event_since_seq is None:
        event_since_seq = event_bus.current_sequence()

    wake_q: _queue.Queue[tuple[str, _pkg.Any]] = _queue.Queue(maxsize=16)

    def _on_event(event) -> None:  # pragma: no cover - exercised via tests
        if event.pipeline_id != pipeline_id:
            return
        if event.event_type.value not in _pkg._STATUS_WAIT_EVENT_TYPES:
            return
        if event.sequence <= event_since_seq:
            return
        try:
            wake_q.put_nowait(("event", event))
        except _queue.Full:
            _pkg.logger.warning(
                "status_wait event queue full; dropping event",
                pipeline_id=pipeline_id,
                event_type=event.event_type.value,
            )

    def _on_message_store_wake() -> None:  # pragma: no cover - exercised via tests
        try:
            store_fn = _pkg._get_message_store()
            store = store_fn()
            messages = store.get_messages(
                pipeline_id,
                since_id=msg_since_id,
                limit=100,
                wait=timeout,
                wait_for_types=list(_pkg._STATUS_WAIT_MESSAGE_TYPES),
                from_tip=msg_since_id is None,
            )
        except Exception as exc:  # pragma: no cover
            _pkg.logger.debug(
                "status_wait daemon error",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return
        if not messages:
            return
        try:
            wake_q.put_nowait(("message", messages))
        except _queue.Full:
            _pkg.logger.warning(
                "status_wait message queue full; dropping message",
                pipeline_id=pipeline_id,
            )

    _pkg._track_host_wait_start()
    event_bus.subscribe(None, _on_event)
    daemon: _pkg.threading.Thread | None = None
    try:
        daemon = _pkg.threading.Thread(
            target=_on_message_store_wake,
            name=f"status-wait-msg-{pipeline_id}",
            daemon=True,
        )
        daemon.start()

        try:
            source, payload = wake_q.get(timeout=timeout)
        except _queue.Empty:
            source = None
            payload = None

        # Re-load the pipeline once here so both paths share a
        # consistent snapshot for the minimal envelope.
        try:
            _store2, fresh_pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
        except _pkg.InvalidPipelineIdError, _pkg.PipelineNotFoundError:
            fresh_pipeline = pipeline

        if source == "event":
            event = payload
            # Issue #2464: never fall back to ``msg_since_id`` when the
            # tip is empty. After a phase-boundary clear the caller's
            # cursor is dead; re-emitting it here is what kept the
            # ``since_id not found in store`` warning firing on every
            # subsequent poll. ``since_id_stale: True`` in the envelope
            # tells the consumer to drop its cached cursor.
            tip_msg_id = _pkg._message_store_tip_id(pipeline_id)
            cursor = _pkg._build_status_wait_cursor(tip_msg_id, event.sequence)
            envelope = _pkg._build_minimal_status_envelope(fresh_pipeline, cursor)
            envelope.update(
                {
                    "changed": True,
                    "trigger": "event",
                    "event_type": event.event_type.value,
                }
            )
            if since_id_stale:
                envelope["since_id_stale"] = True
            return _pkg.make_success_response("Event wake", data=envelope)

        if source == "message":
            messages = payload
            # Issue #2464: same as the event path — fall back to None
            # when the message half is unavailable rather than re-emitting
            # the stale ``msg_since_id``.
            last_id = messages[-1].id if messages else None
            # Delphi filter pass — currently a no-op for the host caller
            # (role=None returns messages unchanged) but plumbed here so a
            # future role parameter can enable reviewer-redaction (R13).
            if _delphi is not None:
                try:
                    messages = _delphi(pipeline_id, None, messages)
                except Exception:  # pragma: no cover
                    pass
            tip_evt_seq = event_bus.current_sequence()
            cursor = _pkg._build_status_wait_cursor(last_id, tip_evt_seq)
            envelope = _pkg._build_minimal_status_envelope(fresh_pipeline, cursor)
            envelope.update(
                {
                    "changed": True,
                    "trigger": "message",
                    "messages": [m.to_dict() for m in messages],
                }
            )
            if since_id_stale:
                envelope["since_id_stale"] = True
            return _pkg.make_success_response("Message wake", data=envelope)

        # Timeout path — minimal envelope only.
        tip_msg_id = _pkg._message_store_tip_id(pipeline_id)
        tip_evt_seq = event_bus.current_sequence()
        cursor = _pkg._build_status_wait_cursor(tip_msg_id, tip_evt_seq)
        envelope = _pkg._build_minimal_status_envelope(fresh_pipeline, cursor)
        envelope.update({"changed": False, "no_change": True})
        if since_id_stale:
            envelope["since_id_stale"] = True
        return _pkg.make_success_response("No change within wait window", data=envelope)
    finally:
        try:
            event_bus.unsubscribe(None, _on_event)
        except Exception:  # pragma: no cover — unsubscribe is best-effort
            pass
        _pkg._track_host_wait_end()


def _get_pipeline_visualization_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Get pipeline DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        format: Output format - "full" (default), "compact", "text", "json"
        ascii: Use ASCII-only characters (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-123",
                "visualization": {
                    "dag": "...",  // Full DAG visualization
                    "compact": "...",  // Single-line status
                    "progress": "..."  // Progress bar
                },
                "phases": {...},  // Phase status summary
                "status": "running",
                "current_phase": "implement"
            }
        }
    """
    # Check if visualization module is available (imported at module level)
    if not _pkg._DAG_VISUALIZER_AVAILABLE:
        return _pkg.make_error_response(
            "Visualization module not available",
            status_code=500,
        )

    repo_path = _pkg.get_repo_path()
    output_format = _pkg.request.args.get("format", "full")
    use_ascii = _pkg.request.args.get("ascii", "false").lower() == "true"

    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)

        if output_format == "json":
            # Return structured JSON report
            report = _pkg.generate_status_report(pipeline, use_ascii=use_ascii)
            return _pkg.make_success_response(
                "Visualization generated",
                data=report,
            )

        elif output_format == "text":
            # Return plain text DAG
            dag_text = _pkg.render_pipeline_dag(pipeline, use_ascii=use_ascii)
            return _pkg.Response(
                dag_text,
                mimetype="text/plain",
                status=200,
            )

        elif output_format == "compact":
            # Return compact single-line status
            compact = _pkg.render_compact_status(pipeline, use_ascii=use_ascii)
            progress = _pkg.render_progress_bar(pipeline, use_ascii=use_ascii)
            return _pkg.make_success_response(
                "Visualization generated",
                data={
                    "pipeline_id": pipeline.id,
                    "compact": compact,
                    "progress": progress,
                    "status": pipeline.status.value,
                    "current_phase": pipeline.current_phase.value,
                },
            )

        else:
            # Full format with all visualizations
            report = _pkg.generate_status_report(pipeline, use_ascii=use_ascii)
            return _pkg.make_success_response(
                "Visualization generated",
                data=report,
            )

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
