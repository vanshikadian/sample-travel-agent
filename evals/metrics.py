"""Business and operational metrics, the ones the pass/fail evaluators don't cover.
Conversion for product, token cost and latency for engineering.

Two sources, each where it's honest:
  - conversion and latency from the stored experiments, so they carry a real
    before/after on identical inputs
  - token and cost from the live traces (the HTTP path production runs), the ask
    was to *track* cost, not compare it across one change

Conversion is a proxy: no booking step exists, so a completed create_itinerary is
the closest thing. Reported two ways because the denominator is a real choice, intent-scoped is the fair one, since a weather lookup was never going to convert.

Prices are per-million tokens; update MODEL_PRICES if the model changes.
"""

import json
import os
from collections import defaultdict

from phoenix.client import Client

from evals.cases import CASES

# USD per 1M tokens. Sources: the agent runs on Haiku, the judges on Sonnet.
MODEL_PRICES = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
}

# Case ids whose user intent is "plan a trip", the denominator for a fair
# conversion rate. A weather lookup was never going to convert, so counting it
# against conversion understates the funnel the customer actually cares about.
ITINERARY_INTENT = {
    "itinerary-chicago-3day",
    "itinerary-paris-5day",
    "multiturn-miami-weekend",
    "premature-booking-miami",
    "premature-booking-vague-confirm",
}


def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _client() -> Client:
    return Client(base_url=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))


def _price(model: str) -> dict:
    for key, price in MODEL_PRICES.items():
        if model.startswith(key):
            return price
    return {"input": 0.0, "output": 0.0}


def _parse_output(task_run) -> dict:
    out = _field(task_run, "output")
    if isinstance(out, str):
        try:
            return json.loads(out)
        except (TypeError, ValueError):
            return {}
    return out or {}


def _iso_seconds(start: str, end: str) -> float:
    from datetime import datetime

    def parse(t):
        return datetime.fromisoformat(t.replace("Z", "+00:00"))

    return (parse(end) - parse(start)).total_seconds()


def conversion_and_latency(experiment_name: str) -> dict:
    """Conversion proxy and per-conversation latency from a stored experiment."""
    client = _client()
    dataset = client.datasets.get_dataset(dataset="travel-agent-v1")
    experiments = client.experiments.list(dataset_id=dataset.id)
    match = next(
        e for e in experiments
        if (_field(e, "name") or _field(e, "experiment_name")) == experiment_name
    )
    exp = client.experiments.get_experiment(experiment_id=_field(match, "id"))
    node_to_case = {e["node_id"]: e["id"] for e in dataset.examples}

    converted_overall = total = 0
    converted_intent = intent_total = 0
    latencies = []

    for task_run in exp["task_runs"]:
        case_id = node_to_case.get(_field(task_run, "dataset_example_id"))
        out = _parse_output(task_run)
        # A completed itinerary: create_itinerary ran and returned a real payload.
        completed = any(
            c.get("name") == "create_itinerary" and isinstance(c.get("result"), dict) and c["result"].get("days")
            for c in out.get("tool_calls", [])
        )
        total += 1
        converted_overall += completed
        if case_id in ITINERARY_INTENT:
            intent_total += 1
            converted_intent += completed

        start, end = _field(task_run, "start_time"), _field(task_run, "end_time")
        if start and end:
            latencies.append(_iso_seconds(start, end))

    latencies.sort()
    return {
        "conversion_overall": converted_overall / total if total else 0.0,
        "conversion_intent": converted_intent / intent_total if intent_total else 0.0,
        "intent_total": intent_total,
        "latency_mean_s": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_s": latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
        "conversations": total,
    }


def token_cost_from_traces(project_name: str = "arize-fde-travel-agent") -> dict:
    """Per-conversation token and cost from the live agent traces.

    Reads the production-path project (session-grouped API traffic), sums tokens
    per model, prices them, and divides by conversation count. This is the
    monitoring number An asked to track, captured on the real request path.
    """
    import urllib.request

    def gql(query: str) -> dict:
        request = urllib.request.Request(
            f"{os.getenv('PHOENIX_COLLECTOR_ENDPOINT', 'http://localhost:6006')}/graphql",
            data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        return json.loads(urllib.request.urlopen(request).read())

    projects = gql("{projects(first:20){edges{node{id name}}}}")
    pid = next(
        p["node"]["id"] for p in projects["data"]["projects"]["edges"]
        if p["node"]["name"] == project_name
    )
    spans_q = (
        '{node(id:"%s"){... on Project{spans(first:1000){edges{node{'
        'spanKind attributes}}}}}}' % pid
    )
    edges = gql(spans_q)["data"]["node"]["spans"]["edges"]

    per_model = defaultdict(lambda: {"input": 0, "output": 0})
    sessions = set()
    for edge in edges:
        node = edge["node"]
        attrs = json.loads(node["attributes"])
        sid = (attrs.get("session") or {}).get("id")
        if sid:
            sessions.add(sid)
        if node["spanKind"] != "llm":
            continue
        llm = attrs.get("llm") or {}
        tc = llm.get("token_count") or {}
        model = llm.get("model_name", "unknown")
        per_model[model]["input"] += tc.get("prompt") or 0
        per_model[model]["output"] += tc.get("completion") or 0

    total_cost = 0.0
    total_tokens = 0
    breakdown = {}
    for model, toks in per_model.items():
        price = _price(model)
        cost = toks["input"] / 1e6 * price["input"] + toks["output"] / 1e6 * price["output"]
        total_cost += cost
        total_tokens += toks["input"] + toks["output"]
        breakdown[model] = {**toks, "cost_usd": cost}

    n = len(sessions) or 1
    return {
        "conversations": len(sessions),
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "tokens_per_conversation": total_tokens / n,
        "cost_per_conversation_usd": total_cost / n,
        "by_model": breakdown,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--before", default="baseline")
    parser.add_argument("--after", default="fix-3-competitor")
    args = parser.parse_args()

    before = conversion_and_latency(args.before)
    after = conversion_and_latency(args.after)
    cost = token_cost_from_traces()

    print("=" * 62)
    print("  BUSINESS VIEW (An), conversion")
    print("=" * 62)
    print(f"  {'metric':32} {args.before:>12} {args.after:>14}")
    print(f"  {'conversion (itinerary-intent)':32} {before['conversion_intent']:>11.1%} {after['conversion_intent']:>14.1%}")
    print(f"  {'conversion (all conversations)':32} {before['conversion_overall']:>11.1%} {after['conversion_overall']:>14.1%}")
    print(f"  intent-scoped denominator: {before['intent_total']} conversations")

    print()
    print("=" * 62)
    print("  TECHNICAL VIEW (Luke), latency + cost")
    print("=" * 62)
    print(f"  {'metric':32} {args.before:>12} {args.after:>14}")
    print(f"  {'latency mean (s/conversation)':32} {before['latency_mean_s']:>12.2f} {after['latency_mean_s']:>14.2f}")
    print(f"  {'latency p95 (s/conversation)':32} {before['latency_p95_s']:>12.2f} {after['latency_p95_s']:>14.2f}")
    print()
    print(f"  token/cost telemetry (live production path, {cost['conversations']} conversations):")
    print(f"    tokens per conversation : {cost['tokens_per_conversation']:,.0f}")
    print(f"    cost per conversation   : ${cost['cost_per_conversation_usd']:.5f}")
    print(f"    total tracked           : {cost['total_tokens']:,} tokens, ${cost['total_cost_usd']:.4f}")
    for model, b in cost["by_model"].items():
        print(f"      {model:28} in={b['input']:>8,} out={b['output']:>7,} ${b['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
