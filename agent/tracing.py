"""OpenInference tracing. Importing this registers the tracer and auto-instruments
the Anthropic SDK; the four tools are our own code so tools.py wraps them explicitly.

This is for observation only, nothing here changes agent behaviour. 
"""

import os

from phoenix.otel import register

PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "arize-fde-travel-agent")

# Let register() read PHOENIX_COLLECTOR_ENDPOINT itself rather than passing endpoint=.
# Given the bare host it resolves transport and path; an explicit endpoint= is used
# verbatim and silently drops every span unless it already has /v1/traces on it.
tracer_provider = register(
    project_name=PROJECT_NAME,
    auto_instrument=True,
    batch=True,
    verbose=False,
)

tracer = tracer_provider.get_tracer("travel-agent")
