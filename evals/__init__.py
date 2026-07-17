"""Evaluation layer: cases, detectors, judges, and the experiment runner.

Loads .env here so this package stands alone. It would otherwise only get config as
a side effect of importing agent.config, which breaks the moment something imports a
judge without touching the agent first.
"""

from dotenv import load_dotenv

load_dotenv()
