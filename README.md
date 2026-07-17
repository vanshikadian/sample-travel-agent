# AI Travel Agent, with an evaluation and feedback loop

The agent below was provided as a starting point. What I added is the system around
it: tracing, evaluations tied to the failure modes the customer named, a frozen test
set, and a harness that runs the whole thing on one command and refuses to let a
change ship if it made anything worse.

The short version of what it found: the agent was inventing facts on roughly one
answer in four, and it was invisible. Ask it for a 3-day Chicago itinerary and the
itinerary tool returns 2 days, while its own `num_days` field says 3. The agent
notices the gap and invents Day 3, along with restaurants and landmarks that appear
nowhere in `data/`. The reply reads well and is wrong. You can't catch that by
reading the output. You catch it by comparing what the tool returned against what
the agent said, which means you need traces.

After three fixes driven by what the evals surfaced:

| | before | after |
|---|---:|---:|
| grounded in tools (hallucination) | 72.4% | **100%** |
| tool output integrity | 88.6% | **100%** |
| scope adherence | 93.3% | **100%** |
| no competitor recommendations | 95.2% | **100%** |
| no visa/immigration advice | 99.0% | **100%** |
| PII echo, premature booking | 100% | 100% (held) |
| conversion (trip-planning intent) | 40% | 40% (held) |
| latency, mean | 3.00s | **2.63s** |

Measured on 35 cases x 3 repetitions x 10 checks, so 1,050 evaluations per run, with
the same frozen questions on both sides. Full write-up in
[`reports/final.md`](reports/final.md), production plan in
[`PRODUCTION.md`](PRODUCTION.md).

That 100% needs a caveat. It means every failure mode there's a test for is covered
on these 35 cases. It doesn't mean the agent is flawless. The cases came from one
discovery call, so they cover what the customer said matters and would miss whatever
nobody thought of. The real next step is growing the set from production traffic.

## Running it

```bash
make phoenix                     # trace collector + UI on :6006
make api                         # the agent
make dataset                     # freeze the 35 cases

make baseline                    # capture the "before", once, before touching the agent
make eval NAME=my-change         # capture, evaluate, report, measure, gate
make gate NAME=my-change         # same, but exits non-zero if anything regressed (CI)
make report NAME=my-change       # re-render from stored results (free, no model calls)
```

`make eval` is the whole loop in one command. That's deliberate, because it's what an
Airflow DAG or a Kubernetes CronJob would call. Moving to production changes the
scheduler, not the command.

## How it's put together

```
evals/
├── cases.py       the 35 questions, each tagged with a failure category and severity tier
├── taxonomy.py    category -> which detectors decide it -> what the bar is
├── detectors.py   6 deterministic checks. Plain functions, no Phoenix imports, no network
├── judges.py      4 LLM judges, for the failures a rule can't see
├── task.py        runs one case, captures the reply alongside the tool calls
├── experiment.py  runs the frozen set and scores it
├── report.py      the two views: one for product, one for engineering
├── metrics.py     conversion, latency, token cost
└── harness.py     the loop: capture, evaluate, report, measure, gate
```

The checks are split by cost, and that split is the scaling argument.

| deterministic, runs on 100% of traffic, free | LLM judge, sampled, costs money |
|---|---|
| `pii_echo` | `grounded_in_tools` |
| `premature_booking` | `scope_adherence` |
| `tool_output_integrity` | `no_policy_advice` |
| `unsupported_facts` | `no_competitor` |
| `honest_when_empty` | |
| `no_looping` | |

Scoring this agent cost roughly three times more than running it, and took four times
longer. That's measured, not estimated: the agent runs about $0.007 a conversation
and each conversation gets four judges on top. At a million conversations a day,
judging every one would roughly quadruple the total cost, so rules run on everything
and judges run on a sample. The two categories the customer called non-negotiable, PII and
accidental booking, are both in the free column. That was deliberate: the tier that
can never be sampled should be the tier that costs nothing to check.

`taxonomy.py` is what makes this portable. Detectors bind to categories rather than
to individual test cases, so pointing this at a different agent means a new taxonomy
and a new case list rather than new evaluation code.

## The gate

`make gate` exits non-zero if any category falls below the customer's own threshold,
if any category regresses against the stored baseline, or if conversion drops. In CI
that's a red build. In a deploy pipeline it's what sits between a merge and
production.

The conversion check is there for a specific reason. One prompt change made every
quality detector read 100% while halving the rate at which users got an itinerary,
because the agent had started asking for dates it didn't need before building
anything. Asking a needless clarifying question isn't a quality failure, but it is a
lost customer, and a gate watching only quality would have shipped it.

## What changed in the agent, and why

Two files, both driven by eval findings rather than by reading the source and
guessing.

**`agent/prompt.py`**. The original was seven lines, and three of them caused the
failures the customer described. `"Don't bombard the user with clarifying questions,
make reasonable assumptions"` is the premature-booking failure written as policy.
`"Always give the user concrete options... users hate vague non-answers"` is what
produces invented airports and restaurants. And `"never refer users to other websites
or tell them to search elsewhere"` forbids the correct answer to a visa question,
which is to send someone to the embassy. There was no scope rule, no competitor rule,
and nothing saying facts must come from tools.

**`agent/tools.py`**. Two bugs, four lines:

- `create_itinerary` used `range(1, num_days)`, so a 3-day request produced 2 days
  while the response still claimed `num_days: 3`. The tool contradicted itself.
- `get_weather` applied a Celsius to Fahrenheit conversion to a value already in
  Fahrenheit. Miami's fixture says 86°F and the tool reported 80°F. Every city was
  squashed toward 72°, which is the formula's fixed point.

Everything else is instrumentation: `agent/tracing.py` plus spans in `loop.py` and
the four tools. The tracing doesn't touch the agent's behaviour. Spans record what
happens, they don't change it.

## Method

The order here matters more than any individual piece.

- **Baseline first.** Bugs found while reading the code were written down and left
  alone until the "before" was captured. A fix applied before a baseline exists is
  unprovable.
- **Frozen dataset.** Before and after ask identical questions in identical order, so
  a difference in the score can only be explained by the agent changing.
- **Three repetitions per case.** The worst failure in the set is intermittent: the
  agent gave incorrect visa guidance one run in three. A single run passes it and you
  ship.
- **Evaluators validated before being trusted.** 18 known-answer cases for the
  detectors and 12 hand-labelled cases for the judges. This was worth doing. An early
  version of the groundedness check flagged `$289 x 3 nights = $867` as a
  hallucination, which would have produced a confidently wrong baseline.
- **One change per experiment**, so each delta is attributable to a specific fix.

## Two assumptions worth stating

The customer described the agent as booking trips, but `create_itinerary` assembles a
plan and reserves nothing. So "accidental booking" is measured as premature or
unconfirmed `create_itinerary` calls, and conversion is measured as a completed
itinerary.

They also named "third-party API down" as a failure mode, but the agent makes no
external calls. That path is tested by checking the agent rejects cleanly when its
tools come back empty, rather than inventing a plausible answer.

---

# The provided agent

A simple AI travel agent built on the Anthropic API. It helps users plan trips: searching flights and hotels, checking the weather, and assembling day-by-day itineraries. All travel data comes from local JSON fixtures in `data/` — there are no external API calls beyond the LLM.

It exposes two interfaces:

- an interactive **CLI chat** (`python -m agent.chat`)
- a **FastAPI endpoint** (`POST /chat`) with in-memory multi-turn conversations

## How it works

The agent is a standard Anthropic tool-calling loop, written plainly with no frameworks:

```
agent/
├── config.py   # env vars (model, data dir)
├── prompt.py   # system prompt
├── tools.py    # tool schemas + implementations backed by data/*.json
├── loop.py     # the tool-calling loop
├── chat.py     # CLI entrypoint
└── api.py      # FastAPI app
data/
├── flights.json
├── hotels.json
└── weather.json
scripts/
└── generate_traffic.py   # sends ~20 sample queries to the API
```

The model can call four tools:

| Tool | What it does |
|---|---|
| `search_flights(origin, destination, date)` | Look up flights between two cities |
| `search_hotels(city, check_in, check_out)` | Look up hotels for a stay |
| `get_weather(city, date)` | Get a forecast for a city |
| `create_itinerary(destination, num_days, notes?)` | Assemble a day-by-day trip plan |

## Setup

Requires Python 3.11+ and an Anthropic API key.

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync
```

Or with pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then configure your key:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY
```

Environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key |
| `ANTHROPIC_MODEL` | no | `claude-haiku-4-5` | Model used by the agent |

## Usage

### CLI chat

```bash
uv run python -m agent.chat
```

```
you> Find me a flight from New York to Miami on March 12, 2026.
agent> I found a few options for you! ...
```

Type `quit` (or Ctrl-D) to exit. Conversation history is kept for the session.

### API

Start the server:

```bash
uv run uvicorn agent.api:app
```

Send a message:

```bash
curl -s localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "I need a hotel in Paris from June 10 to June 14, 2026."}'
```

```json
{"reply": "Here are some hotels in Paris for those dates! ...", "conversation_id": "1f0e..."}
```

To continue a conversation, pass the returned `conversation_id` back:

```bash
curl -s localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "Which one is cheapest?", "conversation_id": "1f0e..."}'
```

Conversations are held in memory and reset when the server restarts. `GET /health` returns `{"status": "ok"}`.

### Traffic generator

With the API server running, send ~20 varied sample queries (including one multi-turn conversation):

```bash
uv run python scripts/generate_traffic.py
```

Point it at a different host with an argument or env var:

```bash
uv run python scripts/generate_traffic.py http://localhost:9000
# or
TRAVEL_AGENT_URL=http://localhost:9000 uv run python scripts/generate_traffic.py
```

## Notes

- Flight, hotel, and weather data are static fixtures — edit the files in `data/` to change what the agent can find.
- There is no database, auth, or persistence; this is intentionally a minimal service.
