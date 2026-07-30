"""Live-cluster contract tests (issue #109).

Every test in this package runs against the dedicated
``aks-korvid-contract-test`` cluster and proves real API-server
semantics: previews cause no persistent mutation, executes mutate
exactly once. The suite is opt-in — it only runs when
``KORVID_CONTRACT_RUN_ID`` is set (see ``conftest.py``).
"""
