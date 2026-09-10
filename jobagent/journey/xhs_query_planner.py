"""Bounded XHS-only recruitment query planning."""

from __future__ import annotations

from jobagent.profile.context import JobSearchProfile


def plan_queries(profile: JobSearchProfile, history_features: tuple[str, ...]) -> tuple[str, ...]:
    """Return at most three XHS recruitment queries from confirmed local facts."""

    role = profile.desired_roles[0].strip()
    location = profile.preferred_locations[0].strip() if profile.preferred_locations else ""
    queries = [" ".join(part for part in (location, role, "招聘") if part)]
    if history_features:
        queries.append(f"{history_features[0]} 招聘")
    if len(profile.desired_roles) > 1:
        queries.append(f"{profile.desired_roles[1].strip()} 招人")
    return tuple(dict.fromkeys(query for query in queries if query))[:3]
