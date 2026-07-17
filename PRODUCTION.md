# Running this in production

Everything here runs on a laptop today. The brief said local was fine, and the
deployment was never the hard part of this project. But the eval system is what has
to survive millions of requests a day, so this is how it gets there and what breaks
first.

Where I have a real measurement I've used it. Where I'm estimating, I say so.

---

## The shape of it

Three things run, and they're separated so they can fail independently.

**The agent** is a FastAPI app. It's stateless apart from an in-memory conversation
dict, which is the first thing that has to change in production. Move it to Redis
and the app scales horizontally behind a normal Deployment and HPA. There's nothing
unusual about operating it; it's a web service that calls an API.

**The collector** is where traces land. Locally that's `phoenix serve` with SQLite.
In a cluster it's Phoenix on Postgres, or Arize AX if they'd rather not run it
themselves. The instrumentation is OpenInference either way, so that's a config
change rather than a rewrite. This is the piece I'd put on managed storage first,
since it's the system of record for everything else.

**The eval harness** is a batch job. It has no business being in the request path
and isn't. `make eval NAME=x` becomes a CronJob, or an Airflow DAG with one task per
stage. The five stages already exist as separate runnable steps (capture, evaluate,
report, measure, gate), which is what makes it schedulable without restructuring
anything.

```
              ┌─────────────┐
   users ────▶│ agent (k8s) │────▶ Anthropic
              └──────┬──────┘
                     │ OTel spans (async, batched)
                     ▼
              ┌─────────────┐        ┌──────────────────┐
              │  collector  │◀───────│  eval harness    │
              │  (Phoenix)  │        │  (CronJob)       │
              └─────────────┘        └────────┬─────────┘
                                              │ exit 1 = block
                                              ▼
                                          PR / rollback
```

Note the span arrow. Spans go out on a background batch processor, so the agent
never blocks on the collector and keeps serving if the collector is down. There's a
cost to that, covered under *When the telemetry lies* below.

---

## The scale problem is the evals, not the agent

This is the number that should drive the design, and it's measured rather than
estimated:

| | measured |
|---|---|
| agent, per conversation | **$0.0068** |
| judge spend, all runs | **$8.33** over 1,571 calls |
| per judge call | **$0.0053** |
| judges, per conversation (four of them) | **$0.021** |
| 105 conversations | ~6 minutes |
| 1,050 evaluations of those conversations | **~25 minutes** |

Scoring the agent cost roughly three times more than running it, and took four times
longer. That's the opposite of what most people expect: the observability is the
expensive part, not the thing being observed. The agent figure is what `metrics.py`
computes from the live traces and prints on every gate run. The judge figure comes
from pricing the spans in the `evaluators` project.

Extrapolating: a judge call costs about $0.0053, and every conversation gets judged
by four of them, so judging works out at about $0.021 per conversation. At a million
conversations a day that's about $21k/day in judge tokens, on top of roughly $7k/day
of agent inference. That doesn't double the cost of the product in order to watch it,
it roughly quadruples the total. The wall clock doesn't work either. 1,050 evaluations took
25 minutes, so four million judge calls a day would need a fleet whose only job is
judging. It's an architecture problem before it's a budget one.

So the split between the two layers is the design rather than a compromise:

**Deterministic detectors run on 100% of traffic.** They're regex and dict lookups,
so they cost microseconds and no money: `pii_echo`, `premature_booking`,
`tool_output_integrity`, `unsupported_facts`, `honest_when_empty`, `no_looping`. Six
of the ten. Both of the customer's 100%-required categories (PII and accidental
booking) are in this set, which was deliberate. The tier that can never be sampled
should be the tier that costs nothing to run on everything, so the deterministic
layer was built first.

**LLM judges run on a sample, plus every trace the cheap layer already flagged.**
Scope, policy advice, competitors and groundedness need reading comprehension. At 1%
sampling that's about $210/day rather than $21k, which is a rounding error against
the agent's own bill. Sampling should be stratified by category rather than uniform:
`happy_path` dominates volume and teaches you very little, while `policy_advice` is
rare and expensive when it's wrong.

The 90-95% coverage bar the customer set for the judged categories is the kind of
target sampling can hit. The 100% bar isn't, and doesn't need to be.

---

## Secrets

`.env` is fine on a laptop and wrong in a cluster. In production `ANTHROPIC_API_KEY`
is a Kubernetes Secret mounted as an env var, sourced from whatever they already
use. External Secrets Operator against Vault or AWS Secrets Manager is the common
shape. Nothing in the code changes, because `config.py` reads the environment and
doesn't care who set it.

Two things worth insisting on:

**Separate keys for the agent and the judges.** They have different blast radii and
very different spend profiles. If judge spend runs away overnight, you want to be
able to revoke that key without taking production down. We saw a small version of
this during the build: the judges quietly consumed 80% of the API budget while the
agent was almost free.

**The collector endpoint is not a secret.** `PHOENIX_COLLECTOR_ENDPOINT` is a
service address and belongs in a ConfigMap. Conflating config with secrets means you
can't change a hostname without a secrets rotation.

---

## When the telemetry lies

The most useful failure in this project was one I caused. I wired up tracing, the
app returned `200`, the agent answered correctly, and every span was silently
dropped. They were posted to the wrong path, rejected with a `405`, and the error
was buried in stderr. The API was green, the dashboards would have been empty, and
nothing alerted, because from the app's point of view nothing had failed.

That failure is inherent to doing telemetry properly. Spans export on a background
batch processor so that a slow collector can't hurt users, which also means a broken
collector can't tell them. You don't get one property without the other. An app
that's green while its observability is dead is worse than having no observability,
because you'll trust it.

So the pipeline has to monitor itself:

- **Alert on span volume, not span errors.** A drop in spans-per-minute measured
  against request rate catches the whole class of failures: misconfiguration,
  collector down, exporter wedged. Error-rate alerting catches none of them, because
  the error rate is zero. If the agent served 10k requests and the collector saw 200
  spans, something is wrong regardless of what the logs say.
- **Export failures should go to the app's own error metrics**, not only to stderr.
- **A synthetic canary**: one traced request a minute, assert that it lands. It's
  the cheapest check available and it would have caught my `405` in about a minute.

---

## The other failures we actually hit

These aren't hypothetical. They happened during the build.

**Provider overload (`529`).** Anthropic returned `Overloaded` in bursts and killed
eval runs mid-flight, repeatedly. At production volume that's routine. Two
consequences. The agent needs retry with backoff and jitter on the request path (the
SDK does this by default, so don't disable it). And the harness has to distinguish
"the detector says the agent failed" from "the detector couldn't run". Those look
identical on a dashboard and are completely different incidents. The code does this
already: `summarise()` counts errored evaluations separately instead of scoring them
zero, which is why every report has an `errored` column sitting at zero. If you
collapse the two, a bad afternoon at your model provider looks exactly like a
quality regression, and someone gets paged for the wrong thing.

**Partial runs.** The session and the network both died mid-run at different points.
Neither cost anything, because conversations and scores are separate layers: the
expensive part (105 agent conversations) was already durable, and resuming only
re-scored the gap. That separation is worth keeping deliberately, since a scoring
job that crashes should never re-run the traffic.

**Rate limits at scale.** Not hit locally, but the judge fleet and the agent will
compete for the same org quota if they share a key. That's another argument for
separate keys, and for running judges in a separate queue at lower priority than
user traffic.

---

## Who gets woken up

The customer described their own org clearly, so this mirrors it rather than
inventing a process:

| severity | check | cadence | who |
|---|---|---|---|
| PII leak, premature booking | deterministic, 100% of traffic | real-time page | product triages, engineering fixes |
| hallucination, scope | judge, sampled | hourly rollup | product reviews trend |
| tone, latency, cost | metrics | weekly | engineering |

An said the product team gets the first notification and decides whether it's a
product problem or a bug. That's the routing rule rather than a detail, and it's why
the alerts carry a category and a tier: those are what map to the two teams.

One thing I'd push back on. Real-time paging on a sampled judge doesn't really work,
because you're paging on a coin flip. Real-time only makes sense on the
deterministic layer. If they want real-time on a judged category, that category
needs a cheap deterministic proxy first, even a crude one, with the judge confirming
afterwards. Better to say that than to quietly sample and call it real-time.

---

## Cost, tracked from day one

An asked for token and cost visibility rather than a budget, so it's captured on
every span from the first line of instrumentation rather than bolted on afterwards.
Cost per conversation currently runs a little under a cent, at roughly 4,500 tokens.
That figure is read live from the trace project and isn't scoped to a single
experiment, so it moves as the agent runs. Treat it as a snapshot rather than a
benchmark.

At a million conversations a day that's roughly $7k/day of agent inference, plus the
sampled judge spend on top. Both come out of the traces, split by model, which is
how these numbers exist at all instead of being estimated.

Levers, cheapest first:

1. **Prompt caching.** The system prompt is now considerably longer than the
   original seven lines and is identical on every request, which makes it a good
   cache prefix. This is the highest-value change available, and I haven't measured
   it, so I won't quote a number for it.
2. **A smaller judge model.** Validate it against the same hand-labelled cases
   first. The judge is doing harder work than the agent, so this is a real quality
   risk rather than a free win. Downgrade only where agreement holds.
3. **Sampling rate.** The blunt instrument, and the last one to reach for.

---

## Rollback

The gate is the rollback trigger, and it exists rather than being a plan. `make gate
NAME=x` exits non-zero when any category falls below the customer's own bar, when
any category regresses against the stored baseline, or when conversion drops. In CI
that's a red build. In a deploy pipeline it's what sits between a merge and
production.

The conversion check is there because of something the loop caught. A prompt change
made every quality detector read 100% while halving the rate at which users actually
got an itinerary. Asking a needless clarifying question isn't a quality failure, but
it is a lost customer, and a gate watching only quality would have shipped it. That
one incident is the argument for An's two views being enforced in code rather than
drawn on a dashboard.

For the automated loop the customer wants: detection is automatic, the fix is
drafted automatically, the gate blocks anything that regresses, and a human still
approves the PR. That's what they asked for. Full autonomy is on the roadmap, and
I'd want two things before removing the human. A baseline that's re-cut deliberately
rather than drifting, and enough history to know the noise floor of each check.
Right now I know `no_competitor` moves by about one evaluation run to run. I don't
know that for every category yet, and you can't automate a decision whose noise you
can't quantify.

---

## Extending to other agents

Nick asked how this works for the agents they build next. The answer is
`evals/taxonomy.py`, one config mapping category to detectors to threshold, plus
`evals/cases.py` for the questions. Detectors bind to categories rather than to
specific cases, so a new agent needs a new taxonomy and a new case list rather than
new eval code.

The genuinely travel-specific parts are the fixtures and the case list. The harness,
the report, the gate, the experiment machinery and the before/after comparison don't
know what a hotel is.

What doesn't transfer for free: `tool_output_integrity` knows this agent's tools
return `days` and `high_f`. That's the seam where a new agent needs real work, and
it's better to say so than to claim the whole thing is generic.

---

## What I'd do first, in order

1. **Redis for conversation state.** The in-memory dict is the only thing preventing
   horizontal scale, and moving it is roughly a day's work.
2. **Span-volume alerting.** I've now shipped a green app with dead telemetry once,
   and I'd rather not do it again.
3. **Prompt caching**, measured.
4. **Grow the eval set from real traffic.** The 35 cases are my guesses about what
   users will do. The customer has no production traffic yet, but the day they do,
   the case list should stop being hand-written and start being sampled from
   reality, especially from the traces the cheap detectors flag. It's the
   highest-value item on this list and the only one that can't be started today.
