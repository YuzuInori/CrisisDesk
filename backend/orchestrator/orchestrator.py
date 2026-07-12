"""
CrisisDesk Multi-Agent Orchestrator
Triage -> Allocator <-> Auditor -> Liaison pipeline with conflict resolution.
Uses escalate_incident() (not resolve_incident()) for no-resource cases.

Incidents that arrive together are processed as a BATCH, not one strict FIFO
queue item at a time:
  - Multi-agent: triages every incident in the batch CONCURRENTLY (parallel
    Qwen calls), then allocates resources in PRIORITY order (CRITICAL first),
    so a later-arriving cardiac arrest can still preempt an earlier-arriving
    minor incident for the last ambulance.
  - Single-agent baseline: has no such structural reprioritization — it just
    works through the batch in arrival order, one at a time. This is what
    makes the "auditor catches a priority violation" story actually happen
    in the benchmark instead of both pipelines behaving identically.

LOAD DISTRIBUTION: batching incidents concurrently means several Qwen calls
can be in flight at once, which can trip rate limits on a single API key.
Two mitigations, both optional / backward compatible:
  1. Each agent ROLE can use its own API key (QWEN_API_KEY_TRIAGE, _ALLOCATOR,
     _AUDITOR, _LIAISON, _SINGLE) so concurrent calls from different roles
     don't compete for the same account's quota. Any role left unset just
     falls back to the shared QWEN_API_KEY.
  2. A semaphore (QWEN_MAX_CONCURRENCY, default 4) caps how many Qwen calls
     are in flight AT ALL at once, plus a short exponential-backoff retry on
     transient errors (rate limits, timeouts).
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from dotenv import load_dotenv

from backend.agents.agents import (
    build_triage_prompt, parse_triage,
    build_allocator_prompt, parse_allocator,
    build_auditor_prompt, parse_auditor,
    build_liaison_prompt, parse_liaison,
    build_single_agent_prompt, parse_single_agent,
)
from backend.simulation.world import travel_time_seconds
from backend.db.database import (
    update_incident_triage, resolve_incident, escalate_incident,
    log_agent_message, log_conflict, resolve_conflict, log_dispatch,
    assign_resource, get_available_resources, get_incident, get_incidents_by_run,
)

load_dotenv()

QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")
MAX_AUDITOR_RETRIES = 2
QWEN_MAX_CONCURRENCY = int(os.getenv("QWEN_MAX_CONCURRENCY", "4"))
QWEN_MAX_RETRIES = 3

# Optional per-role API keys — unset ones fall back to QWEN_API_KEY, so a
# single-key setup keeps working exactly as before with zero config changes.
_ROLE_KEY_ENV = {
    "triage": "QWEN_API_KEY_TRIAGE",
    "allocator": "QWEN_API_KEY_ALLOCATOR",
    "auditor": "QWEN_API_KEY_AUDITOR",
    "liaison": "QWEN_API_KEY_LIAISON",
    "single": "QWEN_API_KEY_SINGLE",
}

_clients_by_key: dict = {}


def _client_for(role: str) -> AsyncOpenAI:
    api_key = os.getenv(_ROLE_KEY_ENV.get(role, ""), "") or QWEN_API_KEY
    if api_key not in _clients_by_key:
        _clients_by_key[api_key] = AsyncOpenAI(api_key=api_key, base_url=QWEN_BASE_URL)
    return _clients_by_key[api_key]


_qwen_semaphore = asyncio.Semaphore(QWEN_MAX_CONCURRENCY)


async def _call_qwen(messages: list, role: str = "triage", temperature: float = 0.15, max_tokens: int = 500) -> str:
    client = _client_for(role)
    delay = 1.0
    last_err = None
    for attempt in range(QWEN_MAX_RETRIES):
        try:
            async with _qwen_semaphore:
                response = await client.chat.completions.create(
                    model=QWEN_MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens,
                )
            return response.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            if attempt < QWEN_MAX_RETRIES - 1:
                await asyncio.sleep(delay)
                delay *= 2
    raise last_err


def _resource_type_for(incident_type: str) -> str:
    from backend.simulation.world import INCIDENT_TYPES
    info = INCIDENT_TYPES.get(incident_type)
    return info["resource"] if info else "ambulance"


def _pending_critical_for(run_id: str, exclude_incident_id: str = None) -> list:
    all_incidents = get_incidents_by_run(run_id)
    return [
        f"{i['incident_type']} at ({i['x']}, {i['y']})"
        for i in all_incidents
        if i["incident_id"] != exclude_incident_id
        and i.get("priority_tier") == "CRITICAL"
        and i["status"] not in ("resolved", "escalated_no_resource")
    ]


# ─── Multi-agent: phase 1 (triage) ─────────────────────────────────────────────

async def _triage_incident(incident: dict, run_id: str, ws_callback=None):
    """Runs ONLY the triage step, mutating `incident` in place with the result.
    Split out so a whole batch of incidents can be triaged concurrently."""
    incident_id = incident["incident_id"]

    async def emit(event: dict):
        if ws_callback:
            await ws_callback({**event, "incident_id": incident_id, "run_id": run_id})

    await emit({"type": "agent_step", "agent": "triage", "status": "thinking",
                "message": f"🩺 Triage assessing {incident['incident_type']}..."})
    triage_raw = await _call_qwen(build_triage_prompt(incident), role="triage", temperature=0.1)
    triage = parse_triage(triage_raw)
    log_agent_message(run_id, incident_id, "triage", "assessment", triage)
    update_incident_triage(incident_id, triage["severity_score"], triage["priority_tier"])
    incident.update(triage)
    incident["preferred_resource"] = _resource_type_for(incident["incident_type"])
    await emit({"type": "agent_step", "agent": "triage", "status": "done",
                "message": f"🩺 Triage: {triage['priority_tier']} (severity {triage['severity_score']}) — {triage['reasoning']}",
                "priority_tier": triage["priority_tier"]})
    await emit({"type": "incident_triaged", "priority_tier": triage["priority_tier"], "x": incident["x"], "y": incident["y"]})
    return incident


# ─── Multi-agent: phase 2 (allocate <-> audit -> dispatch) ─────────────────────

async def _allocate_and_dispatch(incident: dict, run_id: str, pending_critical: list, ws_callback=None) -> dict:
    incident_id = incident["incident_id"]
    t0 = time.perf_counter()

    async def emit(event: dict):
        if ws_callback:
            await ws_callback({**event, "incident_id": incident_id, "run_id": run_id})

    approved_proposal = None
    rejection_context = None
    conflicts_count = 0
    available = []

    for attempt in range(1, MAX_AUDITOR_RETRIES + 2):
        rtype = _resource_type_for(incident["incident_type"])
        available = get_available_resources(run_id, rtype)

        await emit({"type": "agent_step", "agent": "allocator", "status": "thinking",
                    "message": f"📋 Allocator proposing assignment (attempt {attempt})..."})

        alloc_messages = build_allocator_prompt(incident, available, len(pending_critical))
        if rejection_context:
            alloc_messages.append({"role": "user", "content": f"Your previous proposal was REJECTED: {rejection_context}. Revise."})

        proposal = parse_allocator(await _call_qwen(alloc_messages, role="allocator"))
        log_agent_message(run_id, incident_id, "allocator", "proposal", proposal)
        await emit({"type": "agent_step", "agent": "allocator", "status": "done",
                    "message": f"📋 Allocator proposes: {proposal.get('recommended_resource_id') or 'NO SUITABLE RESOURCE'} — {proposal['reasoning']}"})

        if not proposal.get("recommended_resource_id"):
            break

        already_assigned = not any(r["resource_id"] == proposal["recommended_resource_id"] for r in available)

        await emit({"type": "agent_step", "agent": "auditor", "status": "thinking",
                    "message": "🔍 Auditor reviewing against rulebook..."})
        audit = parse_auditor(await _call_qwen(build_auditor_prompt(incident, proposal, {
            "pending_critical_incidents": pending_critical,
            "resource_already_assigned": already_assigned,
        }), role="auditor", temperature=0.1))
        log_agent_message(run_id, incident_id, "auditor", "review", audit)

        if audit["approved"]:
            await emit({"type": "agent_step", "agent": "auditor", "status": "done",
                        "message": f"✅ Auditor approved: {audit['explanation']}"})
            approved_proposal = proposal
            break
        else:
            cid = log_conflict(run_id, incident_id, audit.get("violation_type") or "RULE_VIOLATION",
                               audit["explanation"], "allocator", "auditor")
            conflicts_count += 1
            await emit({"type": "conflict", "agent": "auditor", "status": "rejected",
                        "conflict_id": cid, "message": f"⚠️ Auditor REJECTED: {audit['explanation']}"})
            rejection_context = audit["explanation"]
            resolve_conflict(cid, f"Allocator asked to revise (attempt {attempt})")

    decision_ms = (time.perf_counter() - t0) * 1000

    if not approved_proposal:
        escalate_incident(incident_id)
        log_agent_message(run_id, incident_id, "system", "escalation_check",
                          {"available_count": len(available), "conflicts": conflicts_count})
        await emit({"type": "agent_step", "agent": "system", "status": "escalated",
                    "message": "🚨 No suitable resource — escalated to human dispatcher."})
        await emit({"type": "incident_escalated", "x": incident["x"], "y": incident["y"]})
        return {"incident_id": incident_id, "outcome": "escalated", "resource_id": None,
                "decision_time_ms": decision_ms, "conflicts": conflicts_count}

    resource_id = approved_proposal["recommended_resource_id"]
    matching = [r for r in get_available_resources(run_id) if r["resource_id"] == resource_id]
    resource = matching[0] if matching else {"resource_id": resource_id, "resource_type": "unknown", "x": incident["x"], "y": incident["y"]}

    await emit({"type": "agent_step", "agent": "liaison", "status": "thinking",
                "message": "📡 Field Liaison drafting dispatch instructions..."})
    liaison_msgs, eta = build_liaison_prompt(incident, resource)
    liaison = parse_liaison(await _call_qwen(liaison_msgs, role="liaison", temperature=0.3), eta)
    log_agent_message(run_id, incident_id, "liaison", "dispatch", liaison)

    busy_until = (datetime.utcnow() + timedelta(seconds=liaison["eta_seconds"] * 2)).isoformat()
    assign_resource(resource_id, incident_id, busy_until)
    log_dispatch(run_id, incident_id, resource_id, liaison["instructions"], liaison["eta_seconds"])
    resolve_incident(incident_id)

    await emit({"type": "agent_step", "agent": "liaison", "status": "done",
                "message": f"📡 Dispatched {resource_id} — ETA {liaison['eta_seconds']}s: {liaison['instructions']}"})
    await emit({"type": "incident_resolved", "resource_id": resource_id,
                "message": f"✅ {incident_id} resolved via {resource_id}"})

    return {"incident_id": incident_id, "outcome": "resolved", "resource_id": resource_id,
            "decision_time_ms": decision_ms, "conflicts": conflicts_count}


async def process_incident_multi_agent(
    incident: dict, run_id: str, pending_critical: list, ws_callback=None
) -> dict:
    """Single-incident entry point (kept for compatibility). For concurrent
    batches, use process_incident_batch_multi_agent instead."""
    incident_id = incident["incident_id"]
    if ws_callback:
        await ws_callback({"type": "incident_reported", "incident_id": incident_id, "run_id": run_id,
                           "x": incident["x"], "y": incident["y"]})
    await _triage_incident(incident, run_id, ws_callback)
    return await _allocate_and_dispatch(incident, run_id, pending_critical, ws_callback)


def _order_by_priority_with_tiebreak(incidents: list, run_id: str) -> list:
    """
    Sorts incidents by severity (desc) — the primary ordering used to decide
    who gets scarce resources first. When two or more ALREADY-TRIAGED incidents
    land on the same severity AND need the SAME resource type (meaning they are
    genuinely competing for the same scarce pool, not just coincidentally
    equal), the tie is broken by whichever one has the FASTEST reachable
    matching resource right now — serving the faster case first reduces total
    system-wide time-to-help. This is logged as a system message on the
    incident that lost the tie, so reports can explain in plain language why
    one severity-9.x incident got resourced before an equally severe one.
    """
    ordered = sorted(incidents, key=lambda i: i.get("severity_score", 0), reverse=True)

    def best_eta(inc):
        candidates = get_available_resources(run_id, _resource_type_for(inc["incident_type"]))
        if not candidates:
            return float("inf")
        return min(travel_time_seconds(inc["x"], inc["y"], r["x"], r["y"]) for r in candidates)

    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and abs(ordered[j + 1].get("severity_score", 0) - ordered[i].get("severity_score", 0)) < 0.05:
            j += 1
        if j > i:
            group = ordered[i:j + 1]
            by_type: dict = {}
            for inc in group:
                by_type.setdefault(_resource_type_for(inc["incident_type"]), []).append(inc)
            resolved_group = []
            for rtype, members in by_type.items():
                if len(members) > 1:
                    members = sorted(members, key=best_eta)
                    winner = members[0]
                    eta_w = best_eta(winner)
                    for loser in members[1:]:
                        eta_l = best_eta(loser)
                        log_agent_message(run_id, loser["incident_id"], "system", "tie_break", {
                            "note": (
                                f"Tied at severity {loser.get('severity_score')} with {winner['incident_id']} "
                                f"({winner['incident_type']}) — both need a {rtype.replace('_', ' ')}, but only "
                                f"a limited number were free. {winner['incident_id']} was served first because "
                                f"its nearest available {rtype.replace('_', ' ')} could reach it in "
                                f"{eta_w:.0f}s vs {'unreachable' if eta_l == float('inf') else f'{eta_l:.0f}s'} "
                                f"for this incident — serving the faster case first minimizes total exposure time."
                            )
                        })
                resolved_group.extend(members)
            ordered[i:j + 1] = resolved_group
        i = j + 1
    return ordered


async def process_incident_batch_multi_agent(
    incidents: list, run_id: str, ws_callback=None
) -> list:
    """
    Processes a batch of SIMULTANEOUSLY-arriving incidents:
      1. Announce all of them immediately (map shows the whole batch as pending).
      2. Triage ALL of them concurrently — genuine "same time" reasoning, not
         one-by-one.
      3. Allocate resources in PRIORITY order (most severe first), so the
         Allocator/Auditor are actually forced to choose who deserves the
         scarce ambulance/fire truck/boat more, instead of just serving
         whoever happened to be declared first. Ties on severity AND resource
         type are broken by fastest reachable resource, with the reasoning
         logged (see _order_by_priority_with_tiebreak).
    """
    if ws_callback:
        for inc in incidents:
            await ws_callback({"type": "incident_reported", "incident_id": inc["incident_id"],
                               "run_id": run_id, "x": inc["x"], "y": inc["y"]})

    await asyncio.gather(*[_triage_incident(inc, run_id, ws_callback) for inc in incidents])

    priority_order = _order_by_priority_with_tiebreak(incidents, run_id)

    results = []
    for inc in priority_order:
        pending_critical = _pending_critical_for(run_id, exclude_incident_id=inc["incident_id"])
        result = await _allocate_and_dispatch(inc, run_id, pending_critical, ws_callback)
        results.append(result)
    return results


# ─── Single-Agent Baseline ──────────────────────────────────────────────────────

async def process_incident_single_agent(
    incident: dict, run_id: str, pending_critical: list, ws_callback=None
) -> dict:
    incident_id = incident["incident_id"]
    t0 = time.perf_counter()

    async def emit(event: dict):
        if ws_callback:
            await ws_callback({**event, "incident_id": incident_id, "run_id": run_id})

    rtype = _resource_type_for(incident["incident_type"])
    available = get_available_resources(run_id, rtype)

    await emit({"type": "agent_step", "agent": "single_agent", "status": "thinking",
                "message": f"🤖 Single agent deciding on {incident['incident_type']}..."})

    result = parse_single_agent(await _call_qwen(
        build_single_agent_prompt(incident, available, pending_critical), role="single", temperature=0.2
    ))
    log_agent_message(run_id, incident_id, "single_agent", "full_decision", result)
    update_incident_triage(incident_id, result["severity_score"], result["priority_tier"])
    decision_ms = (time.perf_counter() - t0) * 1000

    resource_id = result.get("recommended_resource_id")
    matching = [r for r in available if r["resource_id"] == resource_id]

    if not resource_id or not matching:
        escalate_incident(incident_id)
        log_agent_message(run_id, incident_id, "system", "escalation_check",
                          {"available_count": len(available), "conflicts": 0})
        await emit({"type": "agent_step", "agent": "single_agent", "status": "escalated",
                    "message": f"🚨 Single agent escalated — {result['reasoning']}"})
        await emit({"type": "incident_escalated", "x": incident["x"], "y": incident["y"]})
        return {"incident_id": incident_id, "outcome": "escalated", "resource_id": None,
                "decision_time_ms": decision_ms, "conflicts": 0}

    resource = matching[0]
    eta = travel_time_seconds(incident["x"], incident["y"], resource["x"], resource["y"])
    busy_until = (datetime.utcnow() + timedelta(seconds=eta * 2)).isoformat()
    assign_resource(resource_id, incident_id, busy_until)
    log_dispatch(run_id, incident_id, resource_id, result["instructions"], eta)
    resolve_incident(incident_id)

    await emit({"type": "agent_step", "agent": "single_agent", "status": "done",
                "message": f"🤖 Dispatched {resource_id} — {result['instructions']}"})
    await emit({"type": "incident_resolved", "resource_id": resource_id,
                "message": f"✅ {incident_id} resolved via {resource_id} (single-agent)"})

    return {"incident_id": incident_id, "outcome": "resolved", "resource_id": resource_id,
            "decision_time_ms": decision_ms, "conflicts": 0}


async def process_incident_batch_single_agent(
    incidents: list, run_id: str, ws_callback=None
) -> list:
    """
    Processes the same simultaneously-arriving batch, but as the single-agent
    baseline naturally would: no separate triage phase, no reordering by
    severity — just strict arrival order, one at a time. This is the
    structural weakness the benchmark is meant to expose.
    """
    if ws_callback:
        for inc in incidents:
            await ws_callback({"type": "incident_reported", "incident_id": inc["incident_id"],
                               "run_id": run_id, "x": inc["x"], "y": inc["y"]})

    results = []
    for inc in incidents:
        pending_critical = _pending_critical_for(run_id, exclude_incident_id=inc["incident_id"])
        result = await process_incident_single_agent(inc, run_id, pending_critical, ws_callback)
        results.append(result)
    return results
