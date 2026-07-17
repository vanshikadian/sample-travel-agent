"""Send a batch of sample user queries to the travel agent API.

Usage:
    python scripts/generate_traffic.py [base_url]

The API server must be running first:
    uvicorn agent.api:app
"""

import os
import sys
import time

import httpx

from evals.cases import CASES

BASE_URL = (
    sys.argv[1] if len(sys.argv) > 1 else os.getenv("TRAVEL_AGENT_URL", "http://localhost:8000")
)


def main():
    with httpx.Client(base_url=BASE_URL, timeout=120) as client:
        health = client.get("/health")
        health.raise_for_status()

        total = sum(len(c["messages"]) for c in CASES)
        sent = 0
        for case in CASES:
            conversation_id = None
            for message in case["messages"]:
                sent += 1
                print(f"[{sent}/{total}] ({case['id']}) you> {message}")
                resp = client.post(
                    "/chat",
                    json={"message": message, "conversation_id": conversation_id},
                )
                resp.raise_for_status()
                body = resp.json()
                conversation_id = body["conversation_id"]
                print(f"agent> {body['reply']}\n")
                time.sleep(0.5)

    print(f"Done, sent {sent} messages across {len(CASES)} conversations.")


if __name__ == "__main__":
    main()
