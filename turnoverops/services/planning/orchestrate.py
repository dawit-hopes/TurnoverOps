"""One planning entry point for the overview button, the daily job, and calendar changes."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.domain import BLOCKING_TASK_STATUSES, LIVE_ASSIGNMENT_STATES, AssignmentState, TaskStatus
from turnoverops.integrations.scheduling_agent import SYSTEM_INSTRUCTION, AgentCompletion, AgentError, GeminiSchedulingClient
from turnoverops.models import (
    Assignment,
    CalendarChangeEvent,
    Cleaner,
    CleaningTask,
    DailyPlanningRun,
    Host,
    HostSettings,
    Property,
)
from turnoverops.scheduling.engine import BusyInterval, describe
from turnoverops.scheduling.plan_schema import (
    AssignmentProposal,
    PlanParseError,
    PreserveProposal,
    SchedulePlan,
    UnassignedProposal,
    parse_plan,
)
from turnoverops.scheduling.validate_plan import validate_plan
from turnoverops.scheduling.windows import as_utc
from turnoverops.services.audit import audit
from turnoverops.services.cleaner_calendar import calendar_busy_intervals
from turnoverops.services.evaluation import cleaner_inputs
from turnoverops.services.host_policy import ensure_settings
from turnoverops.services.planning.alerts import alert_unassigned, alert_unassigned_batch, release_assignment
from turnoverops.services.planning.policy import (
    SKIP_DUPLICATE,
    SKIP_EMPTY,
    SKIP_FAST_PATH,
    SKIP_IMPOSSIBLE,
    SKIP_SETTLED,
    SKIP_UNCHANGED,
    TaskView,
    WorkItem,
    assignment_feasible,
    expand_scope,
    gate,
    needs_decision,
    scope_fingerprint,
)
from turnoverops.services.sync import rebuild_tasks, sync_property_feed
from turnoverops.services.workflow import bump, current_assignment

logger = logging.getLogger("turnoverops")

STAGES = (
    "synchronizing_calendars",
    "reconciling_reservations",
    "reviewing_assignments",
    "coordinating_schedule",
    "validating_plan",
    "saving_changes",
)


def assignment_source(trigger: str, *, fast: bool) -> str:
    if fast:
        return "fast_path"
    if trigger == "calendar_change":
        return "calendar_change"
    return "daily_planning"


def orchestrate(
    db: Session,
    host_id: str,
    trigger: str,
    settings: Settings,
    *,
    task_ids: list[str] | None = None,
    client=None,
    force_review: bool = False,
    now: datetime | None = None,
) -> DailyPlanningRun:
    moment = now or datetime.now(UTC)
    host_settings = ensure_settings(db, host_id)
    zone_name = host_settings.timezone
    from zoneinfo import ZoneInfo

    operational = moment.astimezone(ZoneInfo(zone_name)).date()
    run, created = _acquire(db, host_id, trigger, operational.isoformat(), zone_name, moment, settings)
    if not created:
        return run
    started = run.started_at
    try:
        return _execute(
            db,
            run,
            host_settings,
            settings,
            trigger=trigger,
            task_ids=task_ids,
            client=client,
            force_review=force_review,
            now=moment,
            started=started,
        )
    except Exception as exc:
        logger.exception("planning_failed")
        run.status = "failed"
        run.error = "Today's plan could not be completed. Existing assignments were kept."
        run.details = {"exception": str(exc)[:500]}
        run.finished_at = datetime.now(UTC)
        run.duration_ms = _duration(started, run.finished_at)
        db.commit()
        raise


def _acquire(
    db: Session,
    host_id: str,
    trigger: str,
    operational_date: str,
    timezone_name: str,
    now: datetime,
    settings: Settings,
) -> tuple[DailyPlanningRun, bool]:
    cutoff = now - timedelta(minutes=settings.planning_lock_minutes)
    stale = db.scalars(
        select(DailyPlanningRun).where(
            DailyPlanningRun.host_id == host_id,
            DailyPlanningRun.status == "running",
            DailyPlanningRun.started_at < cutoff,
        )
    ).all()
    for row in stale:
        row.status = "failed"
        row.error = "Planning lock expired"
        row.finished_at = now
    if stale:
        db.commit()
    active = _running(db, host_id)
    if active is not None:
        return active, False
    run = DailyPlanningRun(
        host_id=host_id,
        trigger=trigger,
        status="running",
        stage=STAGES[0],
        operational_date=operational_date,
        timezone=timezone_name,
        started_at=now,
        tasks_considered=0,
        assignments_created=0,
        assignments_preserved=0,
        assignments_changed=0,
        unassigned_count=0,
        conflicts=[],
        warnings=[],
        requires_human_review=False,
        llm_calls=0,
        repair_count=0,
        details={},
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        active = _running(db, host_id)
        if active is not None:
            return active, False
        raise
    db.commit()
    return run, True


def _running(db: Session, host_id: str) -> DailyPlanningRun | None:
    return db.scalar(
        select(DailyPlanningRun).where(DailyPlanningRun.host_id == host_id, DailyPlanningRun.status == "running")
    )


def _stage(db: Session, run: DailyPlanningRun, stage: str) -> None:
    run.stage = stage
    db.commit()


def _duration(started: datetime, finished: datetime) -> int:
    return max(0, int((finished - started).total_seconds() * 1000))


def _execute(
    db: Session,
    run: DailyPlanningRun,
    host_settings: HostSettings,
    settings: Settings,
    *,
    trigger: str,
    task_ids: list[str] | None,
    client,
    force_review: bool,
    now: datetime,
    started: datetime,
) -> DailyPlanningRun:
    host = db.get(Host, run.host_id)
    properties = db.scalars(select(Property).where(Property.host_id == run.host_id, Property.active.is_(True))).all()
    _stage(db, run, "synchronizing_calendars")
    sync_failures: list[dict] = []
    new_events: list[CalendarChangeEvent] = []
    synced: list[Property] = []
    for prop in properties:
        outcome = sync_property_feed(db, prop, settings, now)
        if not outcome["ok"] and outcome.get("error"):
            sync_failures.append({"property_id": prop.id, "property_name": prop.name, "error": outcome["error"]})
        elif outcome["ok"] and outcome.get("synced"):
            synced.append(prop)
            new_events.extend(outcome["events"])
    run.details = {"sync_failures": sync_failures, "notification_deliveries": []}
    db.commit()

    _stage(db, run, "reconciling_reservations")
    for prop in synced:
        rebuild_tasks(db, prop, now)
    linked = []
    from turnoverops.services.planning.changes import link_event_tasks

    if new_events:
        linked = link_event_tasks(db, new_events)
    db.commit()

    seeds = None
    if trigger == "calendar_change":
        seeds = set(task_ids or [])
        seeds.update(linked)
        if not seeds:
            return _complete(
                db,
                run,
                started,
                status="skipped",
                llm_skipped_reason=SKIP_DUPLICATE,
                validation_outcome="skipped",
                reasoning_summary="Calendar poll had no new reservation change.",
                result=_result(run, preserve=[], create=[], change=[], unassigned=[], conflicts=[], warnings=[], conflict_free=True),
                sync_failures=sync_failures,
            )

    _stage(db, run, "reviewing_assignments")
    views = _views(db, run.host_id, properties, now, settings.planning_horizon_hours)
    calendar_busy = calendar_busy_intervals(
        db,
        run.host_id,
        settings,
        now,
        now + timedelta(hours=settings.planning_horizon_hours),
    )
    cleaners = cleaner_inputs(db, run.host_id)
    _record_locked_conflicts(db, views, cleaners, now, calendar_busy)
    views = _views(db, run.host_id, properties, now, settings.planning_horizon_hours)
    busy = _busy(views) + calendar_busy
    scope = expand_scope(views, seeds, cleaners, busy)
    fingerprint = scope_fingerprint(scope, cleaners, busy)
    run.tasks_considered = len(scope)
    run.scope_fingerprint = fingerprint
    db.commit()

    fingerprint_seen = _fingerprint_seen(db, run)
    pending_unassigned = _still_unassigned(db, scope, cleaners, busy) if fingerprint_seen else []
    if fingerprint_seen and not pending_unassigned:
        preserved = _preserve_payload(scope, cleaners, busy)
        unassigned_rows = pending_unassigned
        warnings = _removed_today(db, run.host_id, now)
        summary = "Nothing changed, so the previous plan was kept."
        return _complete(
            db,
            run,
            started,
            status="completed",
            llm_skipped_reason=SKIP_UNCHANGED,
            validation_outcome="skipped",
            llm_calls=0,
            requires_human_review=bool(unassigned_rows),
            reasoning_summary=summary,
            assignments_preserved=len(preserved),
            unassigned_count=len(unassigned_rows),
            result=_result(
                run,
                preserve=preserved,
                create=[],
                change=[],
                unassigned=unassigned_rows,
                conflicts=[],
                warnings=warnings,
                conflict_free=not unassigned_rows,
            ),
            sync_failures=sync_failures,
        )

    if not scope:
        return _complete(
            db,
            run,
            started,
            status="completed",
            llm_skipped_reason=SKIP_EMPTY,
            validation_outcome="skipped",
            reasoning_summary="No open turnovers are inside the planning horizon.",
            result=_result(run, preserve=[], create=[], change=[], unassigned=[], conflicts=[], warnings=[], conflict_free=True),
            sync_failures=sync_failures,
        )

    decision = gate(scope, cleaners, busy, allow_fast_path=host_settings.allow_fast_path)
    _stage(db, run, "coordinating_schedule")
    by_id = {task.id: task for task in scope}
    cleaner_map = {cleaner.id: cleaner for cleaner in cleaners}
    plans: list[tuple[SchedulePlan, str]] = []
    failures_left: list[dict] = []
    prompt_tokens = None
    output_tokens = None
    calls = 0
    repairs = 0
    provider = None
    model = None
    summaries: list[str] = []
    human = False
    skipped_reason = None

    agent_items = [item for item in decision.items if item.action == "agent"]
    local_items = [item for item in decision.items if item.action != "agent"]
    if not agent_items:
        skipped_reason = _local_reason(local_items)

    for item in local_items:
        if item.action == "preserve":
            continue
        if item.action == "fast_path":
            task = by_id[item.task_ids[0]]
            plan = _fast_plan(run, item, task)
            summaries.append(plan.reasoning_summary)
            plans.append((plan, assignment_source(trigger, fast=True)))
        elif item.action == "escalate":
            human = True
            for task_id in item.task_ids:
                plan = _escalate_plan(run, by_id[task_id], cleaners, busy)
                summaries.append(plan.reasoning_summary)
                plans.append((plan, assignment_source(trigger, fast=False)))

    _stage(db, run, "validating_plan")
    accepted: list[tuple[SchedulePlan, str]] = []
    for plan, source in plans:
        problems = validate_plan(
            plan,
            tasks=by_id,
            cleaners=cleaner_map,
            busy=busy,
            expected_run_id=run.id,
            expected_date=date.fromisoformat(run.operational_date),
            expected_timezone=run.timezone,
        )
        if problems:
            failures_left.extend(item.as_dict() for item in problems)
            human = True
        else:
            accepted.append((plan, source))

    for item in agent_items:
        component = [by_id[task_id] for task_id in item.task_ids if task_id in by_id]
        outcome = _coordinate_component(
            run,
            component,
            cleaners,
            cleaner_map,
            by_id,
            busy,
            settings,
            client,
        )
        calls += outcome["calls"]
        repairs += outcome["repairs"]
        prompt_tokens = _add_tokens(prompt_tokens, outcome["prompt_tokens"])
        output_tokens = _add_tokens(output_tokens, outcome["output_tokens"])
        provider = outcome["provider"] or provider
        model = outcome["model"] or model
        if outcome["summary"]:
            summaries.append(outcome["summary"])
        if outcome["plan"] is not None:
            accepted.append((outcome["plan"], assignment_source(trigger, fast=False)))
        else:
            human = True
            failures_left.extend(outcome["failures"])

    if calls:
        skipped_reason = None
    run.llm_calls = calls
    run.repair_count = repairs
    run.prompt_tokens = prompt_tokens
    run.output_tokens = output_tokens
    run.llm_provider = provider
    run.llm_model = model
    run.llm_skipped_reason = skipped_reason
    run.estimated_cost = _cost(settings, prompt_tokens, output_tokens)

    _stage(db, run, "saving_changes")
    if failures_left and not accepted:
        preserved = _preserve_payload(scope, cleaners, busy)
        return _complete(
            db,
            run,
            started,
            status="partial" if preserved else "failed",
            validation_outcome="failed",
            requires_human_review=True,
            conflicts=failures_left,
            warnings=[],
            reasoning_summary="The proposed schedule failed validation. Existing assignments were kept.",
            assignments_preserved=len(preserved),
            result=_result(
                run,
                preserve=preserved,
                create=[],
                change=[],
                unassigned=[],
                conflicts=failures_left,
                warnings=[],
                conflict_free=False,
            ),
            sync_failures=sync_failures,
        )

    created, changed, preserved_ids, unassigned_rows, warnings, conflicts, create_rows, change_rows = _apply(
        db,
        run,
        host,
        host_settings,
        settings,
        accepted,
        scope,
        cleaners,
        busy,
        now,
        force_review=force_review,
        email_client=getattr(client, "email_client", None) if client is not None else None,
    )
    if unassigned_rows:
        items = _notify_unassigned_batch(db, host, scope, unassigned_rows)
        alert_unassigned_batch(db, host, items, settings, email_client=getattr(client, "email_client", None) if client is not None else None)
    conflicts = [*conflicts, *failures_left]
    human = human or bool(unassigned_rows) or bool(conflicts)
    preserved_rows = [{"task_id": task_id, "explanation": "Existing assignment remains feasible"} for task_id in preserved_ids]
    status = "partial" if failures_left or unassigned_rows else "completed"
    validation = "passed" if not failures_left else "failed"
    for event in new_events:
        event.status = "processed"
        event.processed_at = now
    return _complete(
        db,
        run,
        started,
        status=status,
        validation_outcome=validation,
        requires_human_review=human,
        conflicts=conflicts,
        warnings=warnings,
        reasoning_summary=" ".join(summaries)[:500] or "Planning finished.",
        assignments_created=created,
        assignments_changed=changed,
        assignments_preserved=len(preserved_rows),
        unassigned_count=len(unassigned_rows),
        result=_result(
            run,
            preserve=preserved_rows,
            create=create_rows,
            change=change_rows,
            unassigned=unassigned_rows,
            conflicts=conflicts,
            warnings=warnings,
            conflict_free=validation == "passed" and not unassigned_rows and not conflicts,
        ),
        sync_failures=sync_failures,
    )


def _coordinate_component(run, component, cleaners, cleaner_map, by_id, busy, settings, client) -> dict:
    from turnoverops.services.planning.policy import feasible_ids

    relevant = []
    for cleaner in cleaners:
        if any(cleaner.id in feasible_ids(task, cleaners, []) for task in component) or any(
            task.assigned_cleaner_id == cleaner.id for task in component
        ):
            relevant.append(cleaner)
    user = {
        "kind": "plan",
        "planning_run_id": run.id,
        "operational_date": run.operational_date,
        "timezone": run.timezone,
        "tasks": [_task_payload(task) for task in component],
        "cleaners": [_cleaner_payload(cleaner) for cleaner in relevant],
        "preserve_task_ids": [
            task.id for task in component if assignment_feasible(task, cleaners, busy)
        ],
    }
    calls = 0
    repairs = 0
    prompt = None
    output = None
    provider = None
    model = None
    failures: list[dict] = []
    plan = None
    summary = ""
    agent = client or GeminiSchedulingClient()
    attempts = settings.scheduling_max_repair_attempts
    try:
        completion = agent.complete(system=SYSTEM_INSTRUCTION, user=json.dumps(user), settings=settings)
    except AgentError as exc:
        return _agent_failure(str(exc), component)
    calls += 1
    prompt = _add_tokens(prompt, completion.prompt_tokens)
    output = _add_tokens(output, completion.output_tokens)
    provider, model = completion.provider, completion.model
    plan, failures = _interpret(completion, run, by_id, cleaner_map, busy)
    while failures and repairs < attempts:
        seen_ids: list[str] = []
        affected = []
        for item in failures:
            task_id = item.get("task_id")
            if task_id in by_id and task_id not in seen_ids:
                seen_ids.append(task_id)
                affected.append(_task_payload(by_id[task_id]))
        repair_user = {
            "kind": "repair",
            "planning_run_id": run.id,
            "operational_date": run.operational_date,
            "timezone": run.timezone,
            "failed_constraints": failures,
            "affected_tasks": affected or [_task_payload(task) for task in component],
        }
        # A malformed plan has no task ids; send only the tasks in this component, still not the rest of the horizon.
        if failures and all(item.get("task_id") is None for item in failures):
            repair_user["affected_tasks"] = [_task_payload(task) for task in component]
        try:
            completion = agent.complete(system=SYSTEM_INSTRUCTION, user=json.dumps(repair_user), settings=settings)
        except AgentError as exc:
            failures = [{"code": "agent_unavailable", "task_id": None, "explanation": str(exc)[:300]}]
            break
        repairs += 1
        calls += 1
        prompt = _add_tokens(prompt, completion.prompt_tokens)
        output = _add_tokens(output, completion.output_tokens)
        provider, model = completion.provider, completion.model
        plan, failures = _interpret(completion, run, by_id, cleaner_map, busy)
    if failures:
        fallback = _deterministic_component_plan(run, component, cleaners, busy)
        problems = validate_plan(
            fallback,
            tasks=by_id,
            cleaners=cleaner_map,
            busy=busy,
            expected_run_id=run.id,
            expected_date=date.fromisoformat(run.operational_date),
            expected_timezone=run.timezone,
        )
        if not problems:
            plan = fallback
            failures = []
            summary = fallback.reasoning_summary
        else:
            plan = None
            failures = [item.as_dict() for item in problems]
    elif plan is not None:
        summary = plan.reasoning_summary
    return {
        "plan": plan,
        "failures": failures,
        "calls": calls,
        "repairs": repairs,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "provider": provider,
        "model": model,
        "summary": summary,
    }


def _deterministic_component_plan(run, component, cleaners, busy) -> SchedulePlan:
    """Build a validated ranked schedule when the model cannot return a usable plan."""
    from turnoverops.scheduling.engine import decide
    from turnoverops.services.planning.policy import _window

    component_ids = {task.id for task in component}
    blocked = [item for item in busy if item.task_id not in component_ids]
    proposals: list[AssignmentProposal] = []
    unassigned: list[UnassignedProposal] = []
    from turnoverops.services.planning.policy import feasible_ids

    ordered = sorted(
        component,
        key=lambda task: (
            len(feasible_ids(task, cleaners, [item for item in blocked if item.task_id != task.id])),
            task.window_end,
            task.window_start,
            task.id,
        ),
    )
    for task in ordered:
        decision = decide(cleaners, _window(task), [item for item in blocked if item.task_id != task.id])
        chosen = decision.selected
        if chosen is None or chosen.scheduled_start is None or chosen.scheduled_end is None:
            alternatives = [
                f"{item.cleaner_name}: {', '.join(item.reasons)}"
                for item in decision.rejected
            ]
            unassigned.append(
                UnassignedProposal(
                    task_id=task.id,
                    reason_code="no_feasible_cleaner",
                    explanation="No eligible cleaner can take this turnover",
                    attempted_alternatives=alternatives or ["No active cleaner is eligible"],
                    next_action="Review availability or arrange coverage",
                )
            )
            continue
        proposals.append(
            AssignmentProposal(
                task_id=task.id,
                cleaner_id=chosen.cleaner_id,
                start=chosen.scheduled_start,
                end=chosen.scheduled_end,
                deadline=task.window_end,
                explanation=f"Automatically selected the highest ranked feasible cleaner: {describe(chosen)}"[:500],
            )
        )
        blocked.append(BusyInterval(chosen.cleaner_id, task.id, chosen.scheduled_start, chosen.scheduled_end))

    changing = {task.id for task in component if task.assigned_cleaner_id}
    creates = [item for item in proposals if item.task_id not in changing]
    changes = [item for item in proposals if item.task_id in changing]
    return SchedulePlan(
        planning_run_id=run.id,
        operational_date=date.fromisoformat(run.operational_date),
        timezone=run.timezone,
        assignments_to_create=creates,
        assignments_to_change=changes,
        assignments_to_preserve=[],
        unassigned_tasks=unassigned,
        conflicts=[],
        warnings=["The agent response could not be used; a validated deterministic schedule was applied."],
        reasoning_summary="Applied the highest ranked feasible cleaners after the agent response could not be used.",
        requires_human_review=bool(unassigned),
    )


def _notify_unassigned_batch(db, host, scope, rows):
    """Gather unresolved items for one grouped host email after this run."""
    by_id = {task.id: task for task in scope}
    items = []
    for row in rows:
        task = by_id.get(row.get("task_id"))
        if task is None:
            continue
        prop = db.get(Property, task.property_id)
        if prop is not None:
            items.append((task, prop, str(row.get("explanation") or "No validated cleaner is available"), []))
    return items


def _agent_failure(message: str, component: list[TaskView]) -> dict:
    return {
        "plan": None,
        "failures": [{"code": "agent_unavailable", "task_id": task.id, "explanation": message[:300]} for task in component],
        "calls": 0,
        "repairs": 0,
        "prompt_tokens": None,
        "output_tokens": None,
        "provider": None,
        "model": None,
        "summary": "",
    }


def _interpret(completion: AgentCompletion, run, by_id, cleaner_map, busy) -> tuple[SchedulePlan | None, list[dict]]:
    try:
        plan = parse_plan(completion.text)
    except PlanParseError as exc:
        return None, [{"code": "malformed_plan", "task_id": None, "explanation": str(exc)}]
    problems = validate_plan(
        plan,
        tasks=by_id,
        cleaners=cleaner_map,
        busy=busy,
        expected_run_id=run.id,
        expected_date=date.fromisoformat(run.operational_date),
        expected_timezone=run.timezone,
    )
    if problems:
        return None, [item.as_dict() for item in problems]
    return plan, []


def _apply(
    db,
    run,
    host,
    host_settings,
    settings,
    accepted,
    scope,
    cleaners,
    busy,
    now,
    *,
    force_review: bool,
    email_client,
) -> tuple[int, int, list[str], list[dict], list[str], list[dict], list[dict], list[dict]]:
    # Once a plan has been validated, the agent has enough information to act.
    # Keep onboarding's explicit first-plan review, then dispatch future decisions.
    dispatch = not force_review
    created = 0
    changed = 0
    preserved_ids: list[str] = []
    create_rows: list[dict] = []
    change_rows: list[dict] = []
    unassigned_rows: list[dict] = []
    warnings: list[str] = []
    conflicts: list[dict] = []
    handled: set[str] = set()
    by_id = {task.id: task for task in scope}
    for plan, source in accepted:
        warnings.extend(plan.warnings)
        conflicts.extend(item.model_dump() for item in plan.conflicts)
        for item in plan.assignments_to_preserve:
            handled.add(item.task_id)
            if item.task_id not in preserved_ids:
                preserved_ids.append(item.task_id)
        for proposal in plan.assignments_to_create:
            if _save_proposal(db, run, proposal, source, now, settings, replacing=False, dispatch=dispatch, notify=host_settings.notify_cleaners):
                created += 1
                handled.add(proposal.task_id)
                create_rows.append(_labeled(db, proposal.model_dump(mode="json"), proposal.task_id, proposal.cleaner_id))
        for proposal in plan.assignments_to_change:
            if _save_proposal(db, run, proposal, source, now, settings, replacing=True, dispatch=dispatch, notify=host_settings.notify_cleaners):
                changed += 1
                handled.add(proposal.task_id)
                change_rows.append(_labeled(db, proposal.model_dump(mode="json"), proposal.task_id, proposal.cleaner_id))
        for item in plan.unassigned_tasks:
            task = by_id.get(item.task_id)
            if task is None:
                continue
            _leave_unassigned(db, host, task, settings, now, item.reason_code, item.explanation, cleaners, email_client)
            unassigned_rows.append(_labeled(db, item.model_dump(mode="json"), item.task_id))
            handled.add(item.task_id)
            conflicts.append(_labeled(db, {"code": item.reason_code, "task_id": item.task_id, "explanation": item.explanation}, item.task_id))
    for task in scope:
        if task.id in handled:
            continue
        if assignment_feasible(task, cleaners, busy) or task.started or task.status == "in_progress":
            preserved_ids.append(task.id)
            continue
        if task.status in {"completed", "blocked"}:
            continue
        _leave_unassigned(
            db,
            host,
            task,
            settings,
            now,
            "unassigned",
            "No validated assignment was produced for this turnover",
            cleaners,
            None,
        )
        unassigned_rows.append(
            _labeled(
                db,
                {
                    "task_id": task.id,
                    "reason_code": "unassigned",
                    "explanation": "No validated assignment was produced for this turnover",
                    "attempted_alternatives": [],
                    "next_action": "Review cleaner availability or arrange additional coverage.",
                },
                task.id,
            )
        )
    return created, changed, preserved_ids, unassigned_rows, warnings, conflicts, create_rows, change_rows


def _save_proposal(db, run, proposal: AssignmentProposal, source: str, now, settings, *, replacing: bool, dispatch: bool, notify: bool) -> bool:
    task = db.get(CleaningTask, proposal.task_id)
    if task is None or task.host_id != run.host_id:
        return False
    if task.started_at is not None or task.status == TaskStatus.in_progress:
        return False
    bump(db, task)
    if replacing:
        current = current_assignment(db, task)
        if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
            current.state = AssignmentState.cancelled
            current.responded_at = now
            from turnoverops.services.cleaner_mail import revoke_rejection_tokens

            revoke_rejection_tokens(db, current.id, now)
    assignment = Assignment(
        host_id=task.host_id,
        task_id=task.id,
        cleaner_id=proposal.cleaner_id,
        state=AssignmentState.accepted,
        reason=proposal.explanation[:500],
        explanation={"actor": "planning", "source": source},
        attempt_number=1,
        window_start=task.window_start,
        window_end=task.window_end,
        scheduled_start=proposal.start,
        scheduled_end=proposal.end,
        proposed_at=now,
        deadline_at=now + timedelta(minutes=settings.confirmation_timeout_minutes),
        responded_at=now,
        source=source,
    )
    db.add(assignment)
    db.flush()
    task.assigned_cleaner_id = proposal.cleaner_id
    task.current_assignment_id = assignment.id
    task.status = TaskStatus.confirmed
    task.last_evaluated_at = now
    task.host_resolved_at = now
    task.conflict_details = {"summary": proposal.explanation, "selected_cleaner_id": proposal.cleaner_id}
    audit(
        db,
        host_id=task.host_id,
        event_type="assignment_proposed",
        entity_type="assignment",
        entity_id=assignment.id,
        reason=proposal.explanation[:500],
        details={"cleaner_id": proposal.cleaner_id, "actor": "planning", "task_id": task.id, "source": source},
    )
    if dispatch:
        release_assignment(db, assignment, settings, now, notify=notify)
    return True


def _leave_unassigned(db, host, task: TaskView, settings, now, reason: str, explanation: str, cleaners, email_client) -> None:
    row = db.get(CleaningTask, task.id)
    prop = db.get(Property, task.property_id) if row else None
    if row is None or prop is None or row.started_at is not None or row.status == TaskStatus.in_progress:
        return
    bump(db, row)
    current = current_assignment(db, row)
    if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
        current.state = AssignmentState.cancelled
        current.responded_at = now
        from turnoverops.services.cleaner_mail import revoke_rejection_tokens

        revoke_rejection_tokens(db, current.id, now)
    row.assigned_cleaner_id = None
    row.current_assignment_id = None
    row.status = TaskStatus.unassigned
    row.host_resolved_at = None
    row.last_evaluated_at = now
    from turnoverops.services.host_copy import plain_brief

    row.conflict_details = {"summary": explanation, "brief": plain_brief(explanation), "reason_code": reason, "rejected": []}
    audit(
        db,
        host_id=row.host_id,
        event_type="recovery_exhausted",
        entity_type="cleaning_task",
        entity_id=row.id,
        reason=explanation[:500],
        details={"reason_code": reason},
    )
    if host is not None and prop is not None:
        alert_unassigned(
            db,
            host,
            task,
            prop,
            settings,
            reason=reason,
            cleaner_ids=[cleaner.id for cleaner in cleaners],
            email_client=lambda **_: {"suppressed": "included in planning run digest"},
        )


def _record_locked_conflicts(db, views: list[TaskView], cleaners, now: datetime, calendar_busy=None) -> None:
    busy = _busy(views) + list(calendar_busy or [])
    for task in views:
        if not (task.started or task.status == "in_progress"):
            continue
        if assignment_feasible(task, cleaners, busy):
            continue
        row = db.get(CleaningTask, task.id)
        if row is None:
            continue
        row.status = TaskStatus.at_risk
        row.host_resolved_at = None
        row.conflict_details = {
            "summary": "Cleaning is in progress and can no longer be finished in the updated window",
            "signature": "in-progress-window",
        }
        row.last_evaluated_at = now
        audit(
            db,
            host_id=row.host_id,
            event_type="host_intervention_required",
            entity_type="cleaning_task",
            entity_id=row.id,
            reason="Cleaning is in progress and can no longer be finished in the updated window",
        )


def _views(db, host_id: str, properties: list[Property], now: datetime, horizon_hours: int) -> list[TaskView]:
    from zoneinfo import ZoneInfo

    zones = {prop.id: prop for prop in properties}
    rows = db.scalars(select(CleaningTask).where(CleaningTask.host_id == host_id)).all()
    assignment_ids = [row.current_assignment_id for row in rows if row.current_assignment_id]
    assignments = {}
    if assignment_ids:
        assignments = {item.id: item for item in db.scalars(select(Assignment).where(Assignment.id.in_(assignment_ids))).all()}
    views: list[TaskView] = []
    for row in rows:
        prop = zones.get(row.property_id)
        if prop is None:
            continue
        zone = ZoneInfo(prop.timezone)
        if not _in_horizon(row, now, zone, horizon_hours):
            continue
        if row.status == TaskStatus.completed:
            continue
        assignment = assignments.get(row.current_assignment_id) if row.current_assignment_id else None
        views.append(
            TaskView(
                id=row.id,
                property_id=row.property_id,
                property_name=prop.name,
                property_timezone=prop.timezone,
                status=row.status,
                window_start=row.window_start,
                window_end=row.window_end,
                duration_minutes=row.duration_minutes,
                assigned_cleaner_id=assignment.cleaner_id if assignment else row.assigned_cleaner_id,
                assignment_id=assignment.id if assignment else None,
                assignment_state=assignment.state if assignment else None,
                scheduled_start=assignment.scheduled_start if assignment else None,
                scheduled_end=assignment.scheduled_end if assignment else None,
                started=row.started_at is not None or row.status == TaskStatus.in_progress,
            )
        )
    return views


def _in_horizon(task: CleaningTask, now: datetime, zone, hours: int) -> bool:
    if task.status == TaskStatus.blocked:
        return False
    start_day = as_utc(task.window_start).astimezone(zone).date()
    today = now.astimezone(zone).date()
    if start_day <= today:
        return True
    return as_utc(task.window_end) <= now + timedelta(hours=hours)


def _busy(views: list[TaskView]) -> list[BusyInterval]:
    intervals = []
    for task in views:
        if task.status not in BLOCKING_TASK_STATUSES:
            continue
        if task.assignment_state not in LIVE_ASSIGNMENT_STATES:
            continue
        if task.scheduled_start is None or task.scheduled_end is None or not task.assigned_cleaner_id:
            continue
        intervals.append(BusyInterval(task.assigned_cleaner_id, task.id, task.scheduled_start, task.scheduled_end))
    return intervals


def _fingerprint_seen(db: Session, run: DailyPlanningRun) -> bool:
    if not run.scope_fingerprint:
        return False
    previous = db.scalar(
        select(DailyPlanningRun).where(
            DailyPlanningRun.host_id == run.host_id,
            DailyPlanningRun.id != run.id,
            DailyPlanningRun.scope_fingerprint == run.scope_fingerprint,
            DailyPlanningRun.validation_outcome.in_(("passed", "skipped")),
            DailyPlanningRun.status.in_(("completed", "partial")),
        )
    )
    return previous is not None


def _preserve_payload(tasks: list[TaskView], cleaners, busy) -> list[dict]:
    rows = []
    for task in tasks:
        if assignment_feasible(task, cleaners, busy):
            rows.append({"task_id": task.id, "explanation": "The existing assignment still fits, so it was kept."})
    return rows


def _still_unassigned(db: Session, tasks: list[TaskView], cleaners, busy) -> list[dict]:
    rows = []
    for task in tasks:
        if not needs_decision(task, cleaners, busy):
            continue
        stored = db.get(CleaningTask, task.id)
        details = stored.conflict_details or {} if stored is not None else {}
        from turnoverops.services.host_copy import plain_brief

        explanation = plain_brief(str(details.get("summary") or "No eligible cleaner can take this turnover"))
        rows.append(
            _labeled(
                db,
                {
                    "task_id": task.id,
                    "reason_code": details.get("reason_code") or "no_feasible_cleaner",
                    "explanation": explanation,
                    "attempted_alternatives": [],
                    "next_action": "Review cleaner availability or arrange additional coverage.",
                },
                task.id,
            )
        )
    return rows


def _removed_today(db: Session, host_id: str, now: datetime) -> list[str]:
    from zoneinfo import ZoneInfo

    settings = db.scalar(select(HostSettings).where(HostSettings.host_id == host_id))
    zone = ZoneInfo(settings.timezone if settings is not None else "UTC")
    today = now.astimezone(zone).date()
    notes: list[str] = []
    rows = db.scalars(
        select(CleaningTask).where(CleaningTask.host_id == host_id, CleaningTask.status == TaskStatus.blocked)
    ).all()
    for task in rows:
        if as_utc(task.window_start).astimezone(zone).date() != today:
            continue
        summary = str((task.conflict_details or {}).get("summary") or "")
        if "removed" not in summary.lower() and "reservation" not in summary.lower():
            continue
        prop = db.get(Property, task.property_id)
        name = prop.name if prop is not None else "A property"
        notes.append(f"{name} is no longer on today's schedule because the reservation changed, so it was not assigned.")
    return notes


def _fast_plan(run: DailyPlanningRun, item: WorkItem, task: TaskView) -> SchedulePlan:
    evaluation = item.evaluation
    proposal = AssignmentProposal(
        task_id=task.id,
        cleaner_id=evaluation.cleaner_id,
        start=evaluation.scheduled_start,
        end=evaluation.scheduled_end,
        deadline=task.window_end,
        explanation=describe(evaluation)[:500],
    )
    changing = bool(task.assigned_cleaner_id)
    return SchedulePlan(
        planning_run_id=run.id,
        operational_date=date.fromisoformat(run.operational_date),
        timezone=run.timezone,
        assignments_to_create=[] if changing else [proposal],
        assignments_to_change=[proposal] if changing else [],
        assignments_to_preserve=[],
        unassigned_tasks=[],
        conflicts=[],
        warnings=[],
        reasoning_summary=describe(evaluation)[:500],
        requires_human_review=False,
    )


def _escalate_plan(run, task: TaskView, cleaners, busy) -> SchedulePlan:
    from turnoverops.scheduling.engine import decide
    from turnoverops.services.planning.policy import _window

    decision = decide(cleaners, _window(task), [item for item in busy if item.task_id != task.id])
    alternatives = [f"{item.cleaner_name}: {', '.join(item.reasons)}" for item in decision.rejected]
    return SchedulePlan(
        planning_run_id=run.id,
        operational_date=date.fromisoformat(run.operational_date),
        timezone=run.timezone,
        assignments_to_create=[],
        assignments_to_change=[],
        assignments_to_preserve=[],
        unassigned_tasks=[
            UnassignedProposal(
                task_id=task.id,
                reason_code="no_feasible_cleaner",
                explanation="No eligible cleaner can take this turnover",
                attempted_alternatives=alternatives or ["No active cleaner is eligible"],
                next_action="Review availability or arrange coverage",
            )
        ],
        conflicts=[],
        warnings=[],
        reasoning_summary="No eligible cleaner can take this turnover, so it was left unassigned.",
        requires_human_review=True,
    )


def _local_reason(items: list[WorkItem]) -> str:
    actions = {item.action for item in items}
    if "fast_path" in actions and "escalate" not in actions and "agent" not in actions:
        return SKIP_FAST_PATH
    if "escalate" in actions and "fast_path" not in actions:
        return SKIP_IMPOSSIBLE
    if actions <= {"preserve"}:
        return SKIP_SETTLED
    if "fast_path" in actions:
        return SKIP_FAST_PATH
    return SKIP_SETTLED


def _task_payload(task: TaskView) -> dict:
    current = None
    if task.assigned_cleaner_id:
        current = {
            "cleaner_id": task.assigned_cleaner_id,
            "state": task.assignment_state,
            "start": task.scheduled_start.isoformat() if task.scheduled_start else None,
            "end": task.scheduled_end.isoformat() if task.scheduled_end else None,
        }
    return {
        "task_id": task.id,
        "property_id": task.property_id,
        "property_name": task.property_name,
        "window_start": task.window_start.isoformat(),
        "window_end": task.window_end.isoformat(),
        "duration_minutes": task.duration_minutes,
        "status": task.status,
        "current_assignment": current,
    }


def _cleaner_payload(cleaner) -> dict:
    return {
        "id": cleaner.id,
        "name": cleaner.name,
        "timezone": cleaner.timezone,
        "working_days": sorted(cleaner.working_days),
        "work_start": cleaner.work_start.isoformat(),
        "work_end": cleaner.work_end.isoformat(),
        "max_jobs_per_day": cleaner.max_jobs_per_day,
        "eligible_property_ids": sorted(cleaner.eligible_property_ids) if cleaner.eligible_property_ids is not None else None,
        "preferred_property_ids": sorted(cleaner.preferred_property_ids),
        "unavailable_dates": sorted(item.isoformat() for item in cleaner.unavailable_dates),
    }


def _add_tokens(current: int | None, extra: int | None) -> int | None:
    if extra is None:
        return current
    return (current or 0) + extra


def _cost(settings: Settings, prompt: int | None, output: int | None) -> float | None:
    if settings.scheduling_input_usd_per_million is None or settings.scheduling_output_usd_per_million is None:
        return None
    return ((prompt or 0) / 1_000_000) * settings.scheduling_input_usd_per_million + (
        (output or 0) / 1_000_000
    ) * settings.scheduling_output_usd_per_million


def _labeled(db: Session, row: dict, task_id: str | None, cleaner_id: str | None = None) -> dict:
    labeled = dict(row)
    task = db.get(CleaningTask, task_id) if task_id else None
    prop = db.get(Property, task.property_id) if task is not None else None
    cleaner = db.get(Cleaner, cleaner_id) if cleaner_id else None
    if prop is not None:
        labeled["property_name"] = prop.name
    if cleaner is not None:
        labeled["cleaner_name"] = cleaner.name
    return labeled


def _result(run, *, preserve, create, change, unassigned, conflicts, warnings, conflict_free: bool) -> dict:
    return {
        "planning_run_id": run.id,
        "operational_date": run.operational_date,
        "timezone": run.timezone,
        "assignments_to_create": create,
        "assignments_to_change": change,
        "assignments_to_preserve": preserve,
        "unassigned_tasks": unassigned,
        "conflicts": conflicts,
        "warnings": warnings,
        "reasoning_summary": run.reasoning_summary,
        "requires_human_review": run.requires_human_review,
        "conflict_free": conflict_free,
    }


def _complete(db, run, started, *, status: str, result: dict, sync_failures: list[dict], **fields) -> DailyPlanningRun:
    finished = datetime.now(UTC)
    for key, value in fields.items():
        setattr(run, key, value)
    result = dict(result)
    result["reasoning_summary"] = run.reasoning_summary
    result["requires_human_review"] = run.requires_human_review
    result["sync_failures"] = sync_failures
    from turnoverops.models import NotificationDelivery

    deliveries = db.scalars(
        select(NotificationDelivery).where(NotificationDelivery.host_id == run.host_id, NotificationDelivery.created_at >= started)
    ).all()
    result["notification_deliveries"] = [
        _labeled(
            db,
            {"id": item.id, "task_id": item.task_id, "channel": item.channel, "status": item.status},
            item.task_id,
        )
        for item in deliveries
    ]
    result["conflicts"] = [_labeled(db, dict(item), item.get("task_id")) for item in result.get("conflicts") or []]
    run.result = result
    run.conflicts = result.get("conflicts") or []
    run.warnings = result.get("warnings") or []
    run.details = {"sync_failures": sync_failures, "notification_deliveries": result["notification_deliveries"]}
    run.status = status
    run.finished_at = finished
    run.duration_ms = _duration(started, finished)
    if run.stage != "saving_changes":
        run.stage = "saving_changes"
    audit(
        db,
        host_id=run.host_id,
        event_type="planning_completed",
        entity_type="daily_planning_run",
        entity_id=run.id,
        reason=(run.reasoning_summary or status)[:500],
        details={"status": status, "validation_outcome": run.validation_outcome, "llm_calls": run.llm_calls},
    )
    db.commit()
    return run
