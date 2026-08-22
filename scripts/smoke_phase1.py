"""Run the complete phase-one workflow with the smallest live request budget."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from jobagent.config import Settings
from jobagent.tools.interview_evidence import (
    InterviewEvidenceDiscovery,
    InterviewEvidenceTarget,
)


async def smoke(arguments: argparse.Namespace) -> None:
    settings = Settings(
        jobagent_state_db=Path("data/smoke-phase1.db"),
        jobagent_artifact_dir=Path("data/smoke-phase1"),
        jobagent_research_max_iterations=1,
        jobagent_research_queries_per_iteration=1,
        jobagent_research_results_per_query=1,
        jobagent_research_minimum_evidence=1,
        jobagent_research_required_topics=1,
        jobagent_research_timeout=arguments.timeout,
    )
    discovery = InterviewEvidenceDiscovery(settings)
    result = await discovery.discover(
        InterviewEvidenceTarget(
            company=arguments.company,
            role=arguments.role,
            city=arguments.city,
            job_description=arguments.jd,
        )
    )
    result.pop("evidence_artifact_ids", None)
    print(result, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--city")
    parser.add_argument("--jd", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(smoke(arguments))


if __name__ == "__main__":
    main()
