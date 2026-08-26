"""Wall-clock access, in one place.

`utc_now` lived in `ingest` and was imported from there by the sampler, the
archive, the tagger, the session layer and the tag ledger - none of which
ingest anything. A clock is a leaf: it belongs to no side of the corpus /
training seam, so both may depend on it and neither owns it.
"""

from datetime import datetime, timezone


def utc_now() -> str:
    """Second-resolution UTC timestamp, the format stored in every table."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_local() -> datetime:
    """Timezone-aware local now, for interval and recency arithmetic."""
    return datetime.now().astimezone()
