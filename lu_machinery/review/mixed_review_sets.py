"""The author family sets that take the mixed-family review pair. Every reader imports this module.

Anthropic/OpenAI, and all three critical-tier families (NEON-SOLTIER-1120), which leave no
critical seat outside the authors. Every other set keeps its single outside-family reviewer.
It stays free of imports so that importing it never changes a reader's import closure:
reviewer_resolver re-exports it, and integration_sweep reads it without loading the resolver.
"""

MIXED_REVIEW_SETS = frozenset({frozenset({"anthropic", "openai"}), frozenset({"anthropic", "google", "openai"})})
