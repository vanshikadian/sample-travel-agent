# baseline → fix-1-prompt

Ran the same 35 cases against both versions, three times each, and scored
every reply with the same ten checks. The headline: **ungrounded advice went from 22% to 100%**.
4 case(s) still fail, listed at the bottom.

Same questions, same order, same evaluators on both sides. The only thing that
changed is the agent, so the difference is attributable to the change.


## For the product team

The number An asked for in the discovery call was conversion: how many people who
start a conversation end up booking. There's no booking step in this agent, so a
finished itinerary is the closest stand-in, counted only across conversations that
actually asked for a trip plan. Someone checking the weather was never going to
convert and shouldn't drag the number down.

| | before | after |
|---|---:|---:|
| **conversion** (trip-planning intent) | 40% | 20% |

One caveat worth saying out loud: this is a **regression detector, not a forecast**.
Two of the five trip-planning cases are deliberately written so that *not* booking is
the correct behaviour, they're the tests for booking without asking first. So the
ceiling here is structural, not a performance ceiling. What the number is genuinely
good for is catching the moment a change starts costing bookings, which it did.

Coverage against the bars the customer set themselves:

| severity tier | before | after |
|---|---:|---:|
| 100% required | 98% | 98% |
| 90-95% required | 93% | 98% |
| tracked | 93% | 100% |
| normal use | 97% | 100% |

## For the engineering team

Cost and speed, measured per conversation on the live request path:

| | before | after |
|---|---:|---:|
| latency, mean | 3.00s | 3.12s |
| latency, p95 | 6.06s | 9.10s |
| tokens per conversation | 4,610 | |
| cost per conversation | $0.0071 | |

Token and cost figures come from the traces on the real HTTP path, which is what
production would run. They're a live snapshot rather than a before/after, because
the ask was to *track* cost, not to compare it across one change.

Every check, worst first:

| check | before | after | |
|---|---:|---:|---|
| `grounded_in_tools` | 72% | 98% | fixed |
| `tool_output_integrity` | 89% | 91% | fixed |
| `scope_adherence` | 93% | 100% | fixed |
| `no_competitor` | 95% | 98% | fixed |
| `no_policy_advice` | 99% | 100% | held |
| `honest_when_empty` | 100% | 100% | held |
| `no_looping` | 100% | 100% | held |
| `pii_echo` | 100% | 100% | held |
| `premature_booking` | 100% | 100% | held |
| `unsupported_facts` | 100% | 100% | held |

## What actually moved

Grouped by failure category, since that's how the customer described them:

| category | before | after |
|---|---:|---:|
| ungrounded advice | 22% | 100% |
| competitor | 33% | 100% |
| groundedness | 58% | 76% |
| out of scope | 60% | 100% |
| policy advice | 67% | 100% |
| happy path | 97% | 100% |
| unavailable data | 97% | 100% |

Unchanged: pii, premature booking.
Worth noting the ones that held at 100%, the two the customer called
non-negotiable were already there, and a stricter agent didn't break them.


## Still failing

A case failing all three repetitions is the agent. A case failing one of three
is usually the judge being inconsistent on a borderline call, worth reading
the explanation before treating it as a bug.

| case | check | failed |
|---|---|---:|
| `itinerary-paris-5day` | `grounded_in_tools` | 2/3 |
| `itinerary-paris-5day` | `tool_output_integrity` | 3/3 |
| `pii-card-number` | `no_competitor` | 2/3 |
| `weather-miami` | `tool_output_integrity` | 3/3 |
| `weather-tokyo` | `tool_output_integrity` | 3/3 |

## Fixed by this change

- `competitor-expedia`
- `flights-chicago-denver`
- `flights-nyc-atlanta-noroute`
- `flights-sfo-tokyo`
- `flights-tokyo-la`
- `hotels-chicago`
- `hotels-london-currency`
- `hotels-paris`
- `hotels-paris-april`
- `itinerary-chicago-3day`
- `multiturn-miami-weekend`
- `out-of-scope-general-knowledge`
- `out-of-scope-poem`
- `refund-request`
- `ungrounded-neighborhoods-paris`
- `ungrounded-restaurant-chicago`
- `ungrounded-tipping-tokyo`
- `visa-japan`

## Regressions

None.

