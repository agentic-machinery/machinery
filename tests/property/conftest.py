"""Bounded, reproducible input-guard exploration in local and PR test runs."""

from hypothesis import settings

settings.register_profile(
    "input-guards", derandomize=True, database=None, deadline=None, max_examples=40,
)
settings.load_profile("input-guards")
