"""Back-compat shim. The import stage is now the source-neutral
`app.services.receipt_import`, shared by mail and image; this re-exports it so
existing mail callers resolve unchanged."""
from app.services.receipt_import import (  # noqa: F401 — re-exported for callers
    ImportOutcome,
    Outcome,
    PlaceCache,
    import_receipt,
)
