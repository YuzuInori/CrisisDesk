"""
CrisisDesk Benchmark Runner
============================
Runs the same scenario through both pipelines and scores them on
DECISION QUALITY — not API wall-clock time (which measures network latency,
not agent intelligence). Quality score (0-100) is computed from:
  - Rule compliance (no violations): 40 pts
  - Correct escalation (CRITICAL incidents escalated correctly): 30 pts
  - Resolution rate (incidents resolved vs total): 30 pts
"""
import asyncio
import json
import random
import uuid
import time
from datetime import datetime

from backend.simulation.world import get_fixed_scenario, generate_random_scenario, severity_tier, INCIDENT_TYPES
from backend.db.database import (
    get_conn, insert_incident, insert_resource, get_incidents_by_run,
    get_dispatches_by_run, create_benchmark_run, complete_benchmark_run,
    get_resources_by_run, get_messages_by_run,
)
from backend.orchestrator.orchestrator import (
    process_incident_multi_agent, process_incident_single_agent,
    process_incident_batch_multi_agent, process_incident_batch_single_agent,
)


def build_scenario(scenario_name: str = "random", resource_counts: dict = None, seed: int = None):
    """
    Resolves a scenario_name into concrete (incidents_def, resources_def).
    "random" (the default) generates a fresh, seedable scenario honoring
    resource_counts (e.g. from the live-session resource panel) instead of
    always using the same hardcoded 7 incidents / 2-1-1 resource pool.
    Any other name falls back to the fixed named scenarios in world.py.
    """
    if scenario_name == "random":
        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        return generate_random_scenario(resource_counts, seed=seed), seed
    return get_fixed_scenario(scenario_name), None


async def _seed_scenario(run_id: str, incidents_def: list, resources_def: dict):
    for rtype, items in resources_def.items():
        for r in items:
            insert_resource(f"{run_id}-{r['id']}", run_id, rtype, r["x"], r["y"])

    seeded = []
    for i, inc in enumerate(incidents_def):
        incident_id = f"{run_id}-INC-{i+1:02d}"
        insert_incident(incident_id, run_id, inc["incident_type"],
                        inc["incident_type"].replace("_", " ").title(), inc["x"], inc["y"])
        seeded.append({
            "incident_id": incident_id,
            "incident_type": inc["incident_type"],
            "description": inc["incident_type"].replace("_", " ").title(),
            "x": inc["x"], "y": inc["y"],
            "delay_seconds": inc.get("delay_seconds", 0),
        })
    return seeded


def _compute_quality_score(run_id: str, results: list) -> dict:
    """
    Computes a 0-100 quality score based on decision quality, not speed.

    - Rule compliance (40 pts): deduct 10pts per priority violation, 15pts per double-assignment
    - Correct escalation (30 pts): CRITICAL incidents that couldn't get a resource should be
      escalated (not silently dropped). Check that all escalated incidents actually had no
      resource available at that time.
    - Resolution rate (30 pts): resolved / total * 30

    Also detects priority violations and double-assignments post-hoc.
    """
    incidents = get_incidents_by_run(run_id)
    dispatches = get_dispatches_by_run(run_id)

    total = len(incidents)
    if total == 0:
        return {"quality_score": 0, "priority_violations": 0, "double_assignments": 0,
                "resolution_rate": 0, "escalation_accuracy": 0}

    # Resolution rate
    resolved = [i for i in incidents if i["status"] == "resolved"]
    escalated = [i for i in incidents if i["status"] == "escalated_no_resource"]
    resolution_rate = len(resolved) / total

    # Priority violations: a LOW/URGENT incident got RESOLVED using a resource type
    # that a still-open CRITICAL incident of the SAME resource type also needed.
    # (Resolving a flooding case with a rescue boat while an unrelated fire truck
    # incident is escalated is NOT a violation — they were never competing for the
    # same pool of resources, so we only flag genuine same-type contention.)
    def _rtype_of(incident_type):
        return INCIDENT_TYPES.get(incident_type, {}).get("resource")

    priority_violations = 0
    resolved_sorted = sorted([i for i in resolved if i["resolved_at"]], key=lambda x: x["resolved_at"])
    for inc in resolved_sorted:
        if inc.get("priority_tier") in ("LOW", "URGENT"):
            inc_rtype = _rtype_of(inc.get("incident_type"))
            for other in incidents:
                if other["incident_id"] == inc["incident_id"] or other.get("priority_tier") != "CRITICAL":
                    continue
                if _rtype_of(other.get("incident_type")) != inc_rtype:
                    continue  # different resource pools — no real contention
                still_open = (not other.get("resolved_at")) or (other["resolved_at"] > inc["resolved_at"])
                reported_before = other["reported_at"] < inc["resolved_at"]
                if still_open and reported_before:
                    priority_violations += 1
                    break

    # Double-assignments
    double_assignments = 0
    by_resource = {}
    for d in dispatches:
        by_resource.setdefault(d["resource_id"], []).append(d)
    for resource_id, dlist in by_resource.items():
        dlist_s = sorted(dlist, key=lambda d: d["dispatched_at"])
        for i in range(len(dlist_s) - 1):
            t1 = datetime.fromisoformat(dlist_s[i]["dispatched_at"])
            eta1 = dlist_s[i]["eta_seconds"] or 0
            t2 = datetime.fromisoformat(dlist_s[i+1]["dispatched_at"])
            if t2.timestamp() < t1.timestamp() + (eta1 * 2):
                double_assignments += 1

    # Quality score
    rule_compliance_pts = max(0, 40 - priority_violations * 10 - double_assignments * 15)
    resolution_pts = round(resolution_rate * 30, 1)

    # Escalation accuracy: was each escalation actually justified (no suitable resource free
    # at decision time), or did the pipeline give up despite a resource being available?
    # The orchestrator logs an "escalation_check" system message with the count of available
    # resources it saw right before escalating — we verify against that ground truth here.
    if not escalated:
        escalation_accuracy = 1.0  # nothing was escalated, trivially nothing to get wrong
    else:
        messages = get_messages_by_run(run_id)
        checks_by_incident = {}
        for m in messages:
            if m.get("agent_role") == "system" and m.get("message_type") == "escalation_check":
                try:
                    payload = json.loads(m["content"]) if isinstance(m["content"], str) else m["content"]
                except Exception:
                    payload = {}
                checks_by_incident[m["incident_id"]] = payload.get("available_count", 0)

        justified = sum(
            1 for inc in escalated
            if checks_by_incident.get(inc["incident_id"], 0) == 0
        )
        escalation_accuracy = justified / len(escalated)

    escalation_pts = round(escalation_accuracy * 30, 1)

    quality_score = round(rule_compliance_pts + resolution_pts + escalation_pts, 1)

    return {
        "quality_score": quality_score,
        "priority_violations": priority_violations,
        "double_assignments": double_assignments,
        "resolution_rate": round(resolution_rate * 100, 1),
        "escalation_accuracy": round(escalation_accuracy * 100, 1),
    }


async def run_benchmark_scenario(
    mode: str, scenario_name: str = "random", ws_callback=None,
    resource_counts: dict = None, seed: int = None,
    incidents_def: list = None, resources_def: dict = None,
) -> dict:
    run_id = f"{mode}-{uuid.uuid4().hex[:8]}"
    create_benchmark_run(run_id, mode, scenario_name)

    if incidents_def is None or resources_def is None:
        (incidents_def, resources_def), seed = build_scenario(scenario_name, resource_counts, seed)

    incidents = await _seed_scenario(run_id, incidents_def, resources_def)

    t0 = time.perf_counter()

    # All incidents in the scenario arrive AT ONCE — this is what forces the
    # agents to decide who deserves the scarce resource more, instead of just
    # serving whoever happened to be seeded first.
    if mode == "multi_agent":
        results = await process_incident_batch_multi_agent(incidents, run_id, ws_callback)
    else:
        results = await process_incident_batch_single_agent(incidents, run_id, ws_callback)

    total_decision_ms = (time.perf_counter() - t0) * 1000
    final = get_incidents_by_run(run_id)

    resolved_count = sum(1 for i in final if i["status"] == "resolved")
    escalated_count = sum(1 for i in final if i["status"] == "escalated_no_resource")
    response_times = [i["response_time_seconds"] for i in final if i.get("response_time_seconds")]
    avg_response = round(sum(response_times) / len(response_times), 2) if response_times else None

    quality = _compute_quality_score(run_id, results)

    complete_benchmark_run(
        run_id=run_id,
        total_incidents=len(incidents),
        resolved_incidents=resolved_count,
        escalated_incidents=escalated_count,
        priority_violations=quality["priority_violations"],
        double_assignments=quality["double_assignments"],
        avg_response_time_seconds=avg_response,
        quality_score=quality["quality_score"],
        total_decision_time_ms=total_decision_ms,
        raw_log=results,
    )

    if ws_callback:
        await ws_callback({
            "type": "benchmark_complete", "run_id": run_id, "mode": mode,
            "message": f"🏁 {mode} — {resolved_count}/{len(incidents)} resolved, "
                       f"quality score {quality['quality_score']}/100, "
                       f"{quality['priority_violations']} violations"
        })

    return {
        "run_id": run_id, "mode": mode, "scenario_name": scenario_name,
        "total_incidents": len(incidents), "resolved_incidents": resolved_count,
        "escalated_incidents": escalated_count,
        "priority_violations": quality["priority_violations"],
        "double_assignments": quality["double_assignments"],
        "avg_response_time_seconds": avg_response,
        "quality_score": quality["quality_score"],
        "total_decision_time_ms": total_decision_ms,
    }


async def run_full_comparison(
    scenario_name: str = "random", ws_callback=None,
    resource_counts: dict = None, seed: int = None,
) -> dict:
    from backend.db.database import compare_runs

    # Build the scenario once so both pipelines face IDENTICAL incidents/resources —
    # otherwise "multi-agent scored higher" could just mean it got an easier scenario.
    (incidents_def, resources_def), used_seed = build_scenario(scenario_name, resource_counts, seed)

    multi = await run_benchmark_scenario(
        "multi_agent", scenario_name, ws_callback,
        incidents_def=incidents_def, resources_def=resources_def,
    )
    single = await run_benchmark_scenario(
        "single_agent", scenario_name, ws_callback,
        incidents_def=incidents_def, resources_def=resources_def,
    )
    comparison = compare_runs(multi["run_id"], single["run_id"])
    return {"multi_agent": multi, "single_agent": single, "comparison": comparison, "seed": used_seed}
