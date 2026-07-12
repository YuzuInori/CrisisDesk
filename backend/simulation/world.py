"""
CrisisDesk Simulation World
============================
SIMULATION — invented rules, not a real EMS protocol.
"""
import math
import random

GRID_SIZE = 100
SECONDS_PER_UNIT = 8

INCIDENT_TYPES = {
    "cardiac_arrest":        {"base_severity": 10, "resource": "ambulance",   "label": "Cardiac Arrest"},
    "trapped_rising_water":  {"base_severity": 9,  "resource": "rescue_boat", "label": "Trapped, Rising Water"},
    "structure_fire":        {"base_severity": 9,  "resource": "fire_truck",  "label": "Structure Fire"},
    "severe_injury":         {"base_severity": 8,  "resource": "ambulance",   "label": "Severe Injury"},
    "gas_leak":              {"base_severity": 7,  "resource": "fire_truck",  "label": "Gas Leak"},
    "flooding_property":     {"base_severity": 5,  "resource": "rescue_boat", "label": "Property Flooding"},
    "minor_injury":          {"base_severity": 4,  "resource": "ambulance",   "label": "Minor Injury"},
    "downed_powerline":      {"base_severity": 6,  "resource": "fire_truck",  "label": "Downed Power Line"},
    "traffic_accident_minor":{"base_severity": 3,  "resource": "ambulance",   "label": "Minor Traffic Accident"},
    "smoke_smell":           {"base_severity": 4,  "resource": "fire_truck",  "label": "Smoke Smell"},
}

PRIORITY_RULES = {
    "CRITICAL": {"min_severity": 8, "rule": "Must be assigned a resource within one allocation cycle. May not be skipped for a lower-severity incident if a suitable resource is available."},
    "URGENT":   {"min_severity": 5, "rule": "Should be assigned before LOW tier incidents when resources are scarce."},
    "LOW":      {"min_severity": 0, "rule": "May wait if all available resources are needed for CRITICAL or URGENT incidents."},
}

HARD_RULES = [
    "A resource may never be assigned to more than one incident at the same time.",
    "If any CRITICAL incident is unresolved, no resource may be newly assigned to a LOW tier incident.",
    "The nearest suitable available resource is preferred, but proximity never overrides tier priority.",
    "Every incident must eventually be assigned a resource or explicitly escalated to a human dispatcher.",
    "A resource may only be assigned to an incident if its type matches that incident's required resource "
    "type (ambulance/fire_truck/rescue_boat). Never substitute a different resource type — if no "
    "matching-type resource is available, the incident must be escalated instead.",
]


RESOURCE_NEED_LABELS = {
    "ambulance": "needs an ambulance",
    "fire_truck": "needs a fire truck",
    "rescue_boat": "needs a rescue boat",
}


def resource_need_label(incident_type: str) -> str:
    info = INCIDENT_TYPES.get(incident_type, {})
    return RESOURCE_NEED_LABELS.get(info.get("resource"), "needs assistance")


def severity_tier(score: float) -> str:
    if score >= 8:
        return "CRITICAL"
    if score >= 5:
        return "URGENT"
    return "LOW"


def distance(x1, y1, x2, y2) -> float:
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def travel_time_seconds(x1, y1, x2, y2) -> float:
    return round(distance(x1, y1, x2, y2) * SECONDS_PER_UNIT, 1)


def random_coord():
    return round(random.uniform(0, GRID_SIZE), 1)


def rulebook_text() -> str:
    lines = ["PRIORITY TIERS:"]
    for tier, info in PRIORITY_RULES.items():
        lines.append(f"  - {tier} (severity >= {info['min_severity']}): {info['rule']}")
    lines.append("\nHARD RULES (must never be violated):")
    for i, rule in enumerate(HARD_RULES, 1):
        lines.append(f"  {i}. {rule}")
    return "\n".join(lines)


def generate_incident(incident_type=None):
    if incident_type is None:
        incident_type = random.choice(list(INCIDENT_TYPES.keys()))
    info = INCIDENT_TYPES[incident_type]
    severity_jitter = random.uniform(-0.5, 0.5)
    return {
        "incident_type": incident_type,
        "description": info["label"],
        "x": random_coord(),
        "y": random_coord(),
        "base_severity": round(max(0, min(10, info["base_severity"] + severity_jitter)), 1),
        "preferred_resource": info["resource"],
    }


def generate_resources(run_id, counts=None):
    counts = counts or {"ambulance": 2, "fire_truck": 1, "rescue_boat": 1}
    resources = []
    for rtype, n in counts.items():
        for i in range(1, n + 1):
            resources.append({
                "resource_id": f"{run_id}-{rtype.upper()}-{i}",
                "resource_type": rtype,
                "x": random_coord(),
                "y": random_coord(),
            })
    return resources


# Fixed reproducible scenarios for fair benchmark comparison
DEMO_SCENARIO_FLOOD_NIGHT = [
    {"incident_type": "cardiac_arrest",        "x": 12.0, "y": 88.0, "delay_seconds": 0},
    {"incident_type": "trapped_rising_water",  "x": 70.0, "y": 15.0, "delay_seconds": 5},
    {"incident_type": "traffic_accident_minor","x": 50.0, "y": 50.0, "delay_seconds": 10},
    {"incident_type": "structure_fire",        "x": 30.0, "y": 60.0, "delay_seconds": 15},
    {"incident_type": "flooding_property",     "x": 80.0, "y": 80.0, "delay_seconds": 20},
    {"incident_type": "severe_injury",         "x": 45.0, "y": 10.0, "delay_seconds": 25},
    {"incident_type": "smoke_smell",           "x": 60.0, "y": 40.0, "delay_seconds": 30},
]

DEMO_RESOURCES_FLOOD_NIGHT = {
    "ambulance":   [{"id": "AMBULANCE-1", "x": 20.0, "y": 50.0}, {"id": "AMBULANCE-2", "x": 60.0, "y": 60.0}],
    "fire_truck":  [{"id": "FIRE_TRUCK-1", "x": 35.0, "y": 35.0}],
    "rescue_boat": [{"id": "RESCUE_BOAT-1", "x": 75.0, "y": 25.0}],
}


def get_fixed_scenario(name="flood_night"):
    if name == "flood_night":
        return DEMO_SCENARIO_FLOOD_NIGHT, DEMO_RESOURCES_FLOOD_NIGHT
    raise ValueError(f"Unknown scenario: {name}")


def generate_random_scenario(resource_counts=None, num_incidents=7, seed=None):
    """
    Generates a randomized-but-seedable scenario: incident types/positions and
    resource positions all vary run-to-run, but passing the same seed reproduces
    the exact same scenario (used so a single Full Comparison run feeds IDENTICAL
    incidents/resources to both the multi-agent and single-agent pipelines —
    otherwise a "which one did better" comparison wouldn't be fair).

    resource_counts lets the caller (e.g. the live-session resource panel) actually
    control how many of each resource type exist, instead of a hardcoded pool.
    """
    rng = random.Random(seed)
    resource_counts = resource_counts or {"ambulance": 2, "fire_truck": 1, "rescue_boat": 1}
    incident_types = list(INCIDENT_TYPES.keys())

    incidents = []
    for i in range(num_incidents):
        itype = rng.choice(incident_types)
        incidents.append({
            "incident_type": itype,
            "x": round(rng.uniform(0, GRID_SIZE), 1),
            "y": round(rng.uniform(0, GRID_SIZE), 1),
            "delay_seconds": 0,
        })

    resources_def = {}
    for rtype, n in resource_counts.items():
        resources_def[rtype] = [
            {"id": f"{rtype.upper()}-{i+1}", "x": round(rng.uniform(0, GRID_SIZE), 1), "y": round(rng.uniform(0, GRID_SIZE), 1)}
            for i in range(max(0, int(n)))
        ]
    return incidents, resources_def
