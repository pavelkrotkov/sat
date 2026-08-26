"""Session identifiers.

Drill plans are reproducible from (mode, seed), so the identifier derived
from that pair is too - which means replaying a seed would otherwise land on
an existing session row and resurrect it. `session_id` takes the collision
check as a predicate so the rule lives here rather than inline in the
sampler, and stays testable without a database.
"""

import uuid

from typing import Callable


#: Salting is uuid4-based, so a genuine collision run is vanishingly
#: unlikely. A predicate that always reports a collision is not - it means
#: something is wrong with the caller, and spinning forever hides that.
MAX_ID_ATTEMPTS = 64


def session_id(mode: str, seed: str, exists: Callable[[str], bool] | None = None) -> str:
    """Stable id for (mode, seed), salted until it does not collide."""
    candidate = uuid.uuid5(uuid.NAMESPACE_URL, f"{mode}:{seed}").hex[:16]
    if exists is None:
        return candidate
    for _ in range(MAX_ID_ATTEMPTS):
        if not exists(candidate):
            return candidate
        candidate = uuid.uuid5(uuid.NAMESPACE_URL, f"{mode}:{seed}:{uuid.uuid4()}").hex[:16]
    raise RuntimeError(
        f"Could not mint a free session id for {mode}:{seed} in "
        f"{MAX_ID_ATTEMPTS} attempts; the collision check is likely broken."
    )


def opaque_id() -> str:
    """Unpredictable id, for plans with no seed to derive one from."""
    return uuid.uuid4().hex[:16]
