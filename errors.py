"""The one exception the tool raises for anything it can explain to the user.

Lives in its own module so `safety` and `ratelimit` can use it without importing
`migration_api` (which imports them). `migration_api` re-exports it, so
`from migration_api import MigrationError` keeps working everywhere.
"""


class MigrationError(RuntimeError):
    """Any non-recoverable failure in a migration/export run."""
