"""The repeatable evaluation pipeline: one command, five stages, one exit code.

This is what an orchestrator calls, an Airflow DAG or K8s CronJob would invoke
exactly this. In production the scheduler changes, not the command.

    capture   run the frozen case list through the agent, traced
    evaluate  score every reply: deterministic detectors, then LLM judges
    report    render the two views the customer asked for
    measure   conversion, latency, token cost
    gate      compare against baseline, fail if anything regressed

The gate's exit code is the rollback trigger. A human still approves what passes.

Run:
    uv run python -m evals.harness --name my-change
    uv run python -m evals.harness --name my-change --gate     # CI mode
    uv run python -m evals.harness --name cheap --no-judges     # free, fast
"""

import argparse
import os
import sys

from evals import metrics, report
from evals.dataset import DATASET_NAME
from evals.taxonomy import threshold_for

BASELINE_EXPERIMENT = "baseline"

# Judges are non-deterministic, so a hair of movement is noise, not a regression.
REGRESSION_TOLERANCE = 0.02

# Coarser: the intent-scoped denominator is small, so one flipped conversation moves
# it several points. Set above that quantum so we catch a real drop, not run noise.
CONVERSION_TOLERANCE = 0.10


def stage(name: str) -> None:
    print(f"\n{'=' * 62}\n  {name}\n{'=' * 62}")


def capture_and_evaluate(experiment_name: str, repetitions: int, use_judges: bool) -> None:
    """Run the frozen dataset through the agent and score it.

    Delegates to evals.experiment rather than duplicating it, so the harness and
    a manual run can never drift into measuring different things.
    """
    from phoenix.client import Client
    from phoenix.client.experiments import run_experiment

    from evals.experiment import build_evaluators, task

    client = Client(base_url=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    dataset = client.datasets.get_dataset(dataset=DATASET_NAME)
    run_experiment(
        dataset=dataset,
        task=task,
        evaluators=build_evaluators(include_judges=use_judges),
        experiment_name=experiment_name,
        repetitions=repetitions,
    )


def gate(after_name: str, before_name: str = BASELINE_EXPERIMENT) -> list[str]:
    """Return the reasons this change should not ship. Empty means it may.

    Threshold and regression are both needed: a category can clear the bar and
    still have got worse. Conversion is checked separately because quality can
    read 100% while bookings halve.
    """
    before = report.by_category(report.load(before_name))
    after = report.by_category(report.load(after_name))

    failures = []
    for category, score in sorted(after.items()):
        bar = threshold_for(category)
        if bar is not None and score < bar:
            failures.append(f"{category}: {score:.1%} below the customer's {bar:.0%} bar")

        prior = before.get(category)
        if prior is not None and score < prior - REGRESSION_TOLERANCE:
            failures.append(
                f"{category}: {score:.1%} regressed from {prior:.1%} in {before_name}"
            )

    before_conv = metrics.conversion_and_latency(before_name)["conversion_intent"]
    after_conv = metrics.conversion_and_latency(after_name)["conversion_intent"]
    if after_conv < before_conv - CONVERSION_TOLERANCE:
        failures.append(
            f"conversion: {after_conv:.1%} regressed from {before_conv:.1%} in {before_name} "
            f"(quality can look perfect while this falls)"
        )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Repeatable evaluation pipeline.")
    parser.add_argument("--name", required=True, help="experiment name for this run")
    parser.add_argument("--baseline", default=BASELINE_EXPERIMENT)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--no-judges", action="store_true", help="deterministic only: free and fast")
    parser.add_argument("--gate", action="store_true", help="exit non-zero on regression (CI mode)")
    parser.add_argument("--skip-capture", action="store_true", help="score/report an existing run")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if not args.skip_capture:
        stage(f"CAPTURE + EVALUATE  ({args.name}, {args.repetitions} reps)")
        capture_and_evaluate(args.name, args.repetitions, not args.no_judges)

    stage("REPORT")
    out = args.out or f"reports/{args.name}.md"
    before, after = report.load(args.baseline), report.load(args.name)
    markdown = report.render(before, after)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(markdown)
    print(f"  written to {out}")

    stage("MEASURE")
    b = metrics.conversion_and_latency(args.baseline)
    a = metrics.conversion_and_latency(args.name)
    cost = metrics.token_cost_from_traces()
    print(f"  {'metric':34}{args.baseline:>14}{args.name:>18}")
    print(f"  {'conversion (itinerary-intent)':34}{b['conversion_intent']:>13.1%}{a['conversion_intent']:>18.1%}")
    print(f"  {'latency mean (s)':34}{b['latency_mean_s']:>14.2f}{a['latency_mean_s']:>18.2f}")
    print(f"  {'latency p95 (s)':34}{b['latency_p95_s']:>14.2f}{a['latency_p95_s']:>18.2f}")
    print(f"  cost/conversation: ${cost['cost_per_conversation_usd']:.5f}   "
          f"tokens/conversation: {cost['tokens_per_conversation']:,.0f}")

    stage("GATE")
    reasons = gate(args.name, args.baseline)
    if reasons:
        print("  BLOCKED, this change must not ship:")
        for r in reasons:
            print(f"    - {r}")
    else:
        print(f"  PASS, no category below its bar, none regressed against {args.baseline}.")
        print("  A human still reviews the PR before merge.")

    if args.gate and reasons:
        sys.exit(1)


if __name__ == "__main__":
    main()
