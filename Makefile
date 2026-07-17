.PHONY: help phoenix api traffic dataset baseline eval gate report demo clean-reports

# The evaluation pipeline, as targets an orchestrator can call. In production the
# scheduler changes (Airflow DAG, K8s CronJob) but the commands do not.

help:
	@echo "Setup"
	@echo "  make phoenix     start Phoenix locally (traces, datasets, experiments)"
	@echo "  make api         start the agent API"
	@echo "  make dataset     freeze the case list into a Phoenix dataset"
	@echo ""
	@echo "The loop"
	@echo "  make baseline    capture the 'before', run once, before touching the agent"
	@echo "  make eval NAME=x capture + score + report + measure a change"
	@echo "  make gate NAME=x same, but exit non-zero if quality regressed (CI)"
	@echo "  make report NAME=x  re-render a report from a stored run (no spend)"
	@echo ""
	@echo "Other"
	@echo "  make traffic     send live traffic through the API (populates traces)"
	@echo "  make demo        the whole loop end to end"

phoenix:
	PHOENIX_WORKING_DIR=$$HOME/.phoenix-arize-fde phoenix serve

api:
	uv run uvicorn agent.api:app --port 8000

# Live traffic through the HTTP path, which is what production runs. Populates
# the traces that the cost and latency telemetry reads.
traffic:
	uv run python scripts/generate_traffic.py

dataset:
	uv run python -m evals.dataset

# The 'before'. Capture this once, against the untouched agent, and never again:
# a baseline recaptured after a change is not a baseline.
baseline:
	uv run python -m evals.harness --name baseline --baseline baseline

# One turn of the loop: run the frozen cases, score, report, measure, gate.
eval:
	@test -n "$(NAME)" || (echo "usage: make eval NAME=my-change"; exit 1)
	uv run python -m evals.harness --name $(NAME)

# CI mode. Exits non-zero when a category falls below the customer's bar or
# regresses against baseline, the rollback trigger.
gate:
	@test -n "$(NAME)" || (echo "usage: make gate NAME=my-change"; exit 1)
	uv run python -m evals.harness --name $(NAME) --gate

# Re-render from stored results. Free, no model calls.
report:
	@test -n "$(NAME)" || (echo "usage: make report NAME=my-change"; exit 1)
	uv run python -m evals.harness --name $(NAME) --skip-capture

# Same gate, against a run that already happened. Free and instant, the CI check
# without paying to regenerate the evidence.
gate-stored:
	@test -n "$(NAME)" || (echo "usage: make gate-stored NAME=my-change"; exit 1)
	uv run python -m evals.harness --name $(NAME) --skip-capture --gate

demo: dataset
	$(MAKE) eval NAME=demo-$$(date +%H%M%S)

clean-reports:
	rm -rf reports/
