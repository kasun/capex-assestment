"""Vendored harness generator (verbatim from instructions/harness/seed.py).

Kept verbatim (stdlib-only) so ``load_seed`` regenerates the exact
deterministic dataset the harness CLI would produce — same RNG call order,
same ids — while making the submission self-contained after the
instructions/ directory is dropped from the final archive.
"""

from capex.harness.seed import (  # noqa: F401
    APPROVAL_FLOW,
    BASE_DATE,
    CATEGORIES,
    FISCAL_PERIODS,
    STATUSES,
    make_budgets,
    make_orgs,
    make_projects,
    make_properties,
    make_users,
)
