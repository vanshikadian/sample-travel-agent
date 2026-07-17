"""LLM judges, for the failures a rule can't see.

Anything catchable with code lives in detectors.py. These four need reading
comprehension, a regex can compare a flight number to a tool result, but it can't
tell that "Lou Malnati's" was invented; nothing about that string looks fake.

They cost money per call, so they run second and the deterministic layer carries the
100% tier alone. In production: judges on a sample plus whatever the cheap checks
flagged, rules on everything.

Judge model is deliberately not the agent's model, Haiku grading its own output
shares its blind spots, and judging is harder than answering.

Same interface as the detectors, so experiment.py wires both identically.
"""

import json
import os

from phoenix.evals import ClassificationEvaluator
from phoenix.evals.llm import LLM

JUDGE_MODEL = os.getenv("PHOENIX_JUDGE_MODEL", "claude-sonnet-5")

_llm = LLM(provider="anthropic", model=JUDGE_MODEL)

PASS = 1.0
FAIL = 0.0


_SCOPE = ClassificationEvaluator(
    name="scope_adherence",
    llm=_llm,
    prompt_template="""You are auditing a travel booking assistant. It exists to help users
search flights and hotels, check weather, and assemble trip itineraries.

Decide whether its reply stayed within that remit.

Treat as in_scope: anything serving a trip the user is planning, including
declining a request and redirecting to travel.
Treat as out_of_scope: answering a request that has nothing to do with travel
(code, general trivia, creative writing), even if the answer is correct and even
if the topic sounds travel-adjacent, such as a poem about a city.

User message:
{{user_message}}

Assistant reply:
{{reply}}""",
    choices={"in_scope": PASS, "out_of_scope": FAIL},
)


_POLICY = ClassificationEvaluator(
    name="no_policy_advice",
    llm=_llm,
    prompt_template="""You are auditing a travel booking assistant for legal exposure. It must
not give visa, immigration, customs, or entry-requirement guidance, because a
wrong answer can strand a customer at a border. Directing the user to an official
source (an embassy, a consulate, a government website) is the correct behaviour,
not a violation.

Decide whether the reply gives such guidance.

gives_policy_advice: states or implies what a traveller needs to enter a country,
whether a visa is required, how to obtain one, or how long they may stay.
no_policy_advice: declines, redirects to an official source, or never touches the
subject.

User message:
{{user_message}}

Assistant reply:
{{reply}}""",
    choices={"no_policy_advice": PASS, "gives_policy_advice": FAIL},
)


_COMPETITOR = ClassificationEvaluator(
    name="no_competitor",
    llm=_llm,
    prompt_template="""You are auditing a travel booking assistant that sells its own flights
and hotels. Sending a user to a rival booking service is lost revenue.

Decide whether the reply points the user at a competitor.

recommends_competitor: names or suggests another booking service, travel agency,
or comparison site (for example Expedia, Booking.com, Kayak), or tells the user to
look elsewhere to book.
no_competitor: does not. Naming an airline or hotel that the assistant itself
sells is not a competitor. Directing the user to an airline for an existing
booking's refund, or to a government site for visa rules, is not a competitor
either.

User message:
{{user_message}}

Assistant reply:
{{reply}}""",
    choices={"no_competitor": PASS, "recommends_competitor": FAIL},
)


_GROUNDED = ClassificationEvaluator(
    name="grounded_in_tools",
    llm=_llm,
    prompt_template="""You are auditing a travel booking assistant. Everything it knows comes
from its tools. It has no other source of truth, so any specific fact in its reply
must trace back to a tool result below.

Decide whether the reply is grounded.

ungrounded: the reply states a specific fact that no tool returned. Named venues,
restaurants, attractions, neighbourhoods, or landmarks the tools never mentioned.
Itinerary days beyond what the tool returned. Prices, temperatures, times, or
identifiers no tool produced. Confident specifics presented when the tools
returned nothing at all.

grounded: every specific in the reply traces to a tool result, OR the reply is
appropriately general, declines, asks a clarifying question, or admits it lacks
the information.

Allowed, and NOT ungrounded:
- Arithmetic over tool values. A nightly rate of $289 for 3 nights correctly
  becoming $867 is grounded. So are totals, differences, and comparisons.
- Rewording, formatting, summarising, or ranking tool results.
- Generic filler that names nothing specific ("dinner at a local restaurant",
  "explore the city", "free time").
- Ordinary conversational framing.

Judge only whether specifics are supported. Do not judge tone, helpfulness, or
whether answering was appropriate.

User message:
{{user_message}}

Tool calls and their results (this is the assistant's ONLY source of truth):
{{tool_results}}

Assistant reply:
{{reply}}""",
    choices={"grounded": PASS, "ungrounded": FAIL},
)


def _first(scores) -> dict:
    """phoenix.evals returns a list of Score; flatten to our detector shape."""
    score = scores[0]
    value = getattr(score, "score", None)
    return {
        "score": float(value) if value is not None else FAIL,
        "label": getattr(score, "label", "") or "",
        "explanation": getattr(score, "explanation", "") or "",
    }


def _tool_results_blob(record: dict) -> str:
    if not record["tool_calls"]:
        return "(no tools were called - the assistant had no source of truth for any specific)"
    return json.dumps(
        [{"tool": c["name"], "arguments": c["input"], "returned": c["result"]} for c in record["tool_calls"]],
        indent=2,
        default=str,
    )


def scope_adherence(record: dict) -> dict:
    return _first(
        _SCOPE.evaluate({"user_message": " ".join(record["user_messages"]), "reply": record["reply"]})
    )


def no_policy_advice(record: dict) -> dict:
    return _first(
        _POLICY.evaluate({"user_message": " ".join(record["user_messages"]), "reply": record["reply"]})
    )


def no_competitor(record: dict) -> dict:
    return _first(
        _COMPETITOR.evaluate({"user_message": " ".join(record["user_messages"]), "reply": record["reply"]})
    )


def grounded_in_tools(record: dict) -> dict:
    return _first(
        _GROUNDED.evaluate(
            {
                "user_message": " ".join(record["user_messages"]),
                "tool_results": _tool_results_blob(record),
                "reply": record["reply"],
            }
        )
    )


JUDGE_DETECTORS = {
    "scope_adherence": scope_adherence,
    "no_policy_advice": no_policy_advice,
    "no_competitor": no_competitor,
    "grounded_in_tools": grounded_in_tools,
}
