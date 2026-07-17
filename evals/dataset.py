"""Freeze the case list into a named Phoenix dataset.

This is what makes before/after mean anything, both runs pull from the same saved
snapshot, so a score that moves can only be the agent changing, not the questions.

Keyed on case id, so re-running updates rows rather than appending duplicates. Bump
DATASET_NAME if the case list changes enough to make old baselines incomparable.

Run:  uv run python -m evals.dataset
"""

import os

from phoenix.client import Client

from evals.cases import CASES

DATASET_NAME = "travel-agent-v1"


def build_examples() -> list[dict]:
    """Shape the cases the way Phoenix wants them.

    Expected output is deliberately empty, the evaluators are reference-free, checking
    replies against tool results and data/*.json rather than a hand-written ideal answer.
    Phoenix requires the key, so it's present and blank.
    """
    return [
        {
            "id": case["id"],
            "input": {"messages": case["messages"]},
            "output": {},
            "metadata": {"category": case["category"], "tier": case["tier"]},
        }
        for case in CASES
    ]


def main() -> None:
    client = Client(base_url=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    examples = build_examples()
    dataset = client.datasets.create_dataset(name=DATASET_NAME, examples=examples)
    print(f"dataset : {DATASET_NAME}")
    print(f"examples: {len(examples)}")
    print(f"id      : {dataset.id}")


if __name__ == "__main__":
    main()
