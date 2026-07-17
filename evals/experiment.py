"""Run the case list through the agent and score it, as a Phoenix experiment.

One named, stored, comparable run. Capture one before touching the agent and one
after; the difference is the number the customer says they don't have today.

Repetitions default to 3, the task is an LLM call on a small dataset, so single-run
noise can be bigger than the effect of a real fix.

Run:  uv run python -m evals.experiment --name baseline
"""

import argparse
import os

from phoenix.client import Client
from phoenix.client.experiments import run_experiment

from evals.dataset import DATASET_NAME
from evals.detectors import DETERMINISTIC_DETECTORS
from evals.task import run_case

DEFAULT_REPETITIONS = 3


def _as_evaluator(detector):
    """Adapt a detector to the signature Phoenix expects.

    Phoenix binds evaluator args by parameter name and passes the task's return
    value as `output`, so this just renames `record` to `output` and keeps the
    detectors themselves Phoenix-free.
    """

    def evaluator(output: dict) -> dict:
        return detector(output)

    return evaluator


def build_evaluators(include_judges: bool = True) -> dict:
    """Assemble the evaluator set.

    A dict, not a list: passed a list, Phoenix derives names from each function's
    qualname, so identical adapter closures collapse into one and still report
    success.
    """
    detectors = dict(DETERMINISTIC_DETECTORS)
    if include_judges:
        # Imported lazily: constructing a judge builds an LLM client, so a
        # deterministic-only run should not need a model key at all.
        from evals.judges import JUDGE_DETECTORS

        detectors.update(JUDGE_DETECTORS)
    return {name: _as_evaluator(fn) for name, fn in detectors.items()}


def task(input: dict) -> dict:
    """Run one dataset example through the agent."""
    return run_case({"input": input})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="experiment name, e.g. baseline")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--dry-run", type=int, default=0, help="run only N examples")
    parser.add_argument(
        "--no-judges",
        action="store_true",
        help="deterministic detectors only: fast and free, but leaves scope, policy, "
        "competitor and grounding unmeasured",
    )
    args = parser.parse_args()

    client = Client(base_url=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    dataset = client.datasets.get_dataset(dataset=DATASET_NAME)
    evaluators = build_evaluators(include_judges=not args.no_judges)

    experiment = run_experiment(
        dataset=dataset,
        task=task,
        evaluators=evaluators,
        experiment_name=args.name,
        repetitions=args.repetitions,
        dry_run=args.dry_run or False,
    )

    print()
    print(f"experiment : {args.name}")
    print(f"dataset    : {DATASET_NAME} ({len(dataset.examples)} examples)")
    print(f"repetitions: {args.repetitions}")
    print(f"experiment_id: {experiment.get('experiment_id')}")
    print()
    print(f"  {'detector':24} {'pass rate':>10}  {'n':>4}  {'errored':>7}")
    for name, summary in sorted(summarise(experiment).items()):
        print(
            f"  {name:24} {summary['pass_rate']:>9.1%}  {summary['n']:>4}  {summary['errored']:>7}"
        )


def _field(run, name):
    """Read a field off an evaluation run. Real runs yield objects, dry runs yield
    dicts, handling both means a dry run exercises the same summary code."""
    if isinstance(run, dict):
        return run.get(name)
    return getattr(run, name, None)


def summarise(experiment: dict) -> dict:
    """Mean score per detector across every example and repetition.

    run_experiment returns runs, not aggregates, so roll up here. An evaluator that
    raised is counted separately rather than scored zero, a crashed detector is a
    broken instrument, not a failing agent, and averaging them together hides both.
    """
    summary: dict[str, dict] = {}
    for run in experiment.get("evaluation_runs", []):
        entry = summary.setdefault(_field(run, "name"), {"scores": [], "errored": 0})
        if _field(run, "error"):
            entry["errored"] += 1
            continue
        result = _field(run, "result") or {}
        score = result.get("score") if isinstance(result, dict) else getattr(result, "score", None)
        if score is not None:
            entry["scores"].append(float(score))

    return {
        name: {
            "pass_rate": (sum(e["scores"]) / len(e["scores"])) if e["scores"] else 0.0,
            "n": len(e["scores"]),
            "errored": e["errored"],
        }
        for name, e in summary.items()
    }


if __name__ == "__main__":
    main()
