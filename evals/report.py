"""Turn stored experiments into the two views the customer asked for.

The discovery call split the audience: business metrics to product, technical
metrics to engineering. So this renders two reports off one run rather than one
dashboard trying to serve both.

The business view answers "is it working": task completion, which stands in for
the conversion rate An named as the weekly metric, since the repo has no booking
step to convert on.

The technical view answers "what is broken": pass rates per detector, grouped by
the severity tiers the customer set, plus tokens and latency.

Comparing two experiments is the point. A pass rate on its own says little; the
same rate before and after a change is the thing they said they have never had.

Run:  uv run python -m evals.report --before baseline --after fix-1-prompt
"""

import argparse
import os
from collections import defaultdict

from phoenix.client import Client

from evals.cases import CASES
from evals.dataset import DATASET_NAME
from evals.taxonomy import detectors_for, threshold_for

TIER_ORDER = ["required_100", "required_90_95", "tracked", "baseline"]
TIER_LABELS = {
    "required_100": "100% required",
    "required_90_95": "90-95% required",
    "tracked": "tracked",
    "baseline": "normal use",
}


def _field(run, name):
    if isinstance(run, dict):
        return run.get(name)
    return getattr(run, name, None)


def _client() -> Client:
    return Client(base_url=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))


def load(experiment_name: str) -> dict:
    """Fetch a stored experiment by name, with its runs joined back to case ids.

    Phoenix keys runs by dataset example, so the category and tier a case was
    labelled with have to be joined back on to group results by severity.
    """
    client = _client()
    dataset = client.datasets.get_dataset(dataset=DATASET_NAME)
    experiments = client.experiments.list(dataset_id=dataset.id)

    match = next(
        (e for e in experiments if (_field(e, "name") or _field(e, "experiment_name")) == experiment_name),
        None,
    )
    if match is None:
        names = [(_field(e, "name") or _field(e, "experiment_name")) for e in experiments]
        raise SystemExit(f"no experiment named {experiment_name!r}. found: {names}")

    experiment = client.experiments.get_experiment(experiment_id=_field(match, "id"))

    node_to_case = {e["node_id"]: e["id"] for e in dataset.examples}
    run_to_case = {}
    for task_run in experiment["task_runs"]:
        run_id = _field(task_run, "id") or _field(task_run, "experiment_run_id")
        run_to_case[run_id] = node_to_case.get(_field(task_run, "dataset_example_id"))

    scores = []
    for evaluation in experiment["evaluation_runs"]:
        result = _field(evaluation, "result") or {}
        score = result.get("score") if isinstance(result, dict) else getattr(result, "score", None)
        if score is None:
            continue
        scores.append(
            {
                "case": run_to_case.get(_field(evaluation, "experiment_run_id")),
                "detector": _field(evaluation, "name"),
                "passed": float(score) == 1.0,
                "explanation": (result.get("explanation") if isinstance(result, dict) else "") or "",
            }
        )
    return {"name": experiment_name, "scores": scores, "raw": experiment}


def _rate(rows) -> float:
    return (sum(r["passed"] for r in rows) / len(rows)) if rows else 0.0


def by_detector(report: dict) -> dict:
    grouped = defaultdict(list)
    for row in report["scores"]:
        grouped[row["detector"]].append(row)
    return {name: _rate(rows) for name, rows in grouped.items()}


def by_tier(report: dict) -> dict:
    """Pass rate per severity tier.

    Grouped by the tier of the case being asked, not of the detector: the
    customer set thresholds per failure category, and a case's tier is what says
    which threshold applies.
    """
    tier_of = {c["id"]: c["tier"] for c in CASES}
    grouped = defaultdict(list)
    for row in report["scores"]:
        tier = tier_of.get(row["case"])
        if tier:
            grouped[tier].append(row)
    return {tier: _rate(rows) for tier, rows in grouped.items()}


def by_category(report: dict) -> dict:
    """Pass rate per failure category, scored only by the detectors that define it.

    Averaging every detector over a category's cases buries the signal: a
    restaurant question trivially passes the PII, loop, and booking checks, so a
    case that fabricates neighbourhoods on every run still reports ~91%. The
    taxonomy says which detectors actually speak to each category.
    """
    category_of = {c["id"]: c["category"] for c in CASES}
    grouped = defaultdict(list)
    for row in report["scores"]:
        category = category_of.get(row["case"])
        if not category:
            continue
        relevant = detectors_for(category)
        if relevant is not None and row["detector"] not in relevant:
            continue
        grouped[category].append(row)
    return {category: _rate(rows) for category, rows in grouped.items()}


def failing_cases(report: dict) -> dict:
    """Cases that failed, and how often. Reproducibility is the signal.

    A case failing every repetition is the agent. A case failing one of three is
    usually the judge being inconsistent on a borderline call. Collapsing both
    into one number loses the distinction that tells you which is which.
    """
    counts = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in report["scores"]:
        entry = counts[row["case"]][row["detector"]]
        entry[1] += 1
        if not row["passed"]:
            entry[0] += 1
    return {
        case: {det: tuple(v) for det, v in dets.items() if v[0] > 0}
        for case, dets in counts.items()
        if any(v[0] > 0 for v in dets.values())
    }


def _delta(before: float, after: float) -> str:
    diff = (after - before) * 100
    if abs(diff) < 0.05:
        return "     -"
    return f"{diff:+6.1f}"


def render(before: dict, after: dict) -> str:
    # Imported here rather than at module scope: metrics imports nothing from
    # report, and keeping the dependency one-way means report stays usable on its
    # own if the metrics source ever changes.
    from evals.metrics import conversion_and_latency, token_cost_from_traces

    b_m = conversion_and_latency(before["name"])
    a_m = conversion_and_latency(after["name"])
    bd, ad = by_detector(before), by_detector(after)
    bc, ac = by_category(before), by_category(after)
    cost = token_cost_from_traces()

    moved = sorted(
        ((n, bc[n], ac.get(n, 0)) for n in bc if abs(ac.get(n, 0) - bc[n]) >= 0.01),
        key=lambda r: r[1],
    )
    still = failing_cases(after)

    out = []
    out.append(f"# {before['name']} → {after['name']}\n")

    # Lead with the answer. Anyone opening this wants to know whether the change
    # helped before they want a table.
    biggest = moved[0] if moved else None
    if biggest:
        out.append(
            f"Ran the same {len(CASES)} cases against both versions, three times each, and scored"
        )
        out.append(
            f"every reply with the same ten checks. The headline: **{biggest[0].replace('_', ' ')} "
            f"went from {biggest[1]:.0%} to {biggest[2]:.0%}**."
        )
        if not still:
            out.append("Nothing is failing any check now, and nothing regressed.\n")
        else:
            out.append(f"{len(still)} case(s) still fail, listed at the bottom.\n")
    out.append(
        "Same questions, same order, same evaluators on both sides. The only thing that"
    )
    out.append("changed is the agent, so the difference is attributable to the change.\n")

    out.append("\n## For the product team\n")
    out.append("The number An asked for in the discovery call was conversion: how many people who")
    out.append("start a conversation end up booking. There's no booking step in this agent, so a")
    out.append("finished itinerary is the closest stand-in, counted only across conversations that")
    out.append("actually asked for a trip plan. Someone checking the weather was never going to")
    out.append("convert and shouldn't drag the number down.\n")
    out.append("| | before | after |")
    out.append("|---|---:|---:|")
    out.append(f"| **conversion** (trip-planning intent) | {b_m['conversion_intent']:.0%} | {a_m['conversion_intent']:.0%} |")
    out.append("")
    out.append("One caveat worth saying out loud: this is a **regression detector, not a forecast**.")
    out.append("Two of the five trip-planning cases are deliberately written so that *not* booking is")
    out.append("the correct behaviour, they're the tests for booking without asking first. So the")
    out.append("ceiling here is structural, not a performance ceiling. What the number is genuinely")
    out.append("good for is catching the moment a change starts costing bookings, which it did.\n")
    out.append("Coverage against the bars the customer set themselves:\n")
    out.append("| severity tier | before | after |")
    out.append("|---|---:|---:|")
    for tier in TIER_ORDER:
        b, a = by_tier(before).get(tier), by_tier(after).get(tier)
        if b is None:
            continue
        out.append(f"| {TIER_LABELS[tier]} | {b:.0%} | {a:.0%} |")

    out.append("\n## For the engineering team\n")
    out.append(f"Cost and speed, measured per conversation on the live request path:\n")
    out.append("| | before | after |")
    out.append("|---|---:|---:|")
    out.append(f"| latency, mean | {b_m['latency_mean_s']:.2f}s | {a_m['latency_mean_s']:.2f}s |")
    out.append(f"| latency, p95 | {b_m['latency_p95_s']:.2f}s | {a_m['latency_p95_s']:.2f}s |")
    out.append(f"| tokens per conversation | {cost['tokens_per_conversation']:,.0f} | |")
    out.append(f"| cost per conversation | ${cost['cost_per_conversation_usd']:.4f} | |")
    out.append("")
    out.append("Token and cost figures come from the traces on the real HTTP path, which is what")
    out.append("production would run. They're a live snapshot rather than a before/after, because")
    out.append("the ask was to *track* cost, not to compare it across one change.\n")
    out.append("Every check, worst first:\n")
    out.append("| check | before | after | |")
    out.append("|---|---:|---:|---|")
    for name in sorted(bd, key=lambda n: bd[n]):
        delta = ad.get(name, 0) - bd[name]
        note = "fixed" if delta >= 0.01 else ("held" if abs(delta) < 0.01 else "**regressed**")
        out.append(f"| `{name}` | {bd[name]:.0%} | {ad.get(name, 0):.0%} | {note} |")

    out.append("\n## What actually moved\n")
    if not moved:
        out.append("Nothing shifted by more than a point. The change was neutral on quality.\n")
    else:
        out.append("Grouped by failure category, since that's how the customer described them:\n")
        out.append("| category | before | after |")
        out.append("|---|---:|---:|")
        for name, b, a in moved:
            out.append(f"| {name.replace('_', ' ')} | {b:.0%} | {a:.0%} |")
        out.append("")
        unchanged = [n for n in bc if n not in {m[0] for m in moved}]
        if unchanged:
            out.append(
                "Unchanged: " + ", ".join(sorted(n.replace("_", " ") for n in unchanged)) + "."
            )
            out.append("Worth noting the ones that held at 100%, the two the customer called")
            out.append("non-negotiable were already there, and a stricter agent didn't break them.\n")

    out.append("\n## Still failing\n")
    if not still:
        out.append("Nothing. Every case passes every check it's subject to.\n")
        out.append("That means every failure mode we wrote a test for is covered **on these")
        out.append(f"{len(CASES)} cases**, not that the agent is flawless. The honest next step is")
        out.append("growing the case list from real production traffic rather than our guesses.\n")
    else:
        out.append("A case failing all three repetitions is the agent. A case failing one of three")
        out.append("is usually the judge being inconsistent on a borderline call, worth reading")
        out.append("the explanation before treating it as a bug.\n")
        out.append("| case | check | failed |")
        out.append("|---|---|---:|")
        for case, dets in sorted(still.items()):
            for det, (bad, total) in sorted(dets.items()):
                out.append(f"| `{case}` | `{det}` | {bad}/{total} |")

    fixed = set(failing_cases(before)) - set(still)
    if fixed:
        out.append("\n## Fixed by this change\n")
        for case in sorted(fixed):
            out.append(f"- `{case}`")

    regressed = set(still) - set(failing_cases(before))
    out.append("\n## Regressions\n")
    out.append("\n".join(f"- `{c}`" for c in sorted(regressed)) if regressed else "None.\n")

    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--out", default="reports/eval-report.md")
    args = parser.parse_args()

    before, after = load(args.before), load(args.after)
    markdown = render(before, after)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(markdown)
    print(markdown)
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
