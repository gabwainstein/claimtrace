"""ProvSleuth — deterministic research provenance and claim verification.

A small, stdlib-only knowledge graph that tracks how your analysis fits together:
raw data -> preprocessing -> artifacts -> figures -> claims. Its job is PROPAGATION —
when a canonical choice changes (a dataset version, a model, a pipeline), it lists every
downstream result that is now stale, so nothing silently lags the decision. It also keeps a
lab-notebook journal of every attempt (including dead-ends and retractions) and can run
project-specific numeric verifiers that confirm your headline numbers still reproduce.

It verifies that the machinery is internally consistent (paths exist, no cycles/dangling edges,
claims cite on-backbone artifacts, headline numbers reproduce). It does NOT claim your science is
correct — that boundary is deliberate.
"""
from .verify import check, registry, approx  # noqa: F401

__version__ = "0.4.0"
__all__ = ["check", "registry", "approx", "__version__"]
