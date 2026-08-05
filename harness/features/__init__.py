"""Feature primitive (L2 three-layer state model).

A feature is the smallest unit of user-visible behavior tracked across
sessions, with its own verification and evidence.  Layer separation:

- ``harness.todos``  — in-session step planning (agent's own checklist);
- ``harness.tasks``  — cross-session scheduling (owner / dependencies);
- ``harness.features`` — durable behavior + verification + state + evidence.

Public API::

    create_feature(name, behavior, verification) -> Feature
    claim_feature(id) -> Feature            # not_started -> active
    verify_feature(id, passed, evidence)    # ONLY gate into passing
    block_feature(id, reason)               # -> blocked
    reopen_feature(id)                      # -> active
    list_features() / get_feature(id)
"""

from harness.features.schema import (
    TRANSITIONS,
    Feature,
    VerificationEvidence,
    can_transition,
)
from harness.features.state import (
    FEATURES_DIRNAME,
    block_feature,
    claim_feature,
    clear_features,
    corrupt_feature_files,
    create_feature,
    feature_is_stale,
    features_dir,
    get_feature,
    list_features,
    record_evaluation,
    reopen_feature,
    verify_feature,
)

__all__ = [
    "TRANSITIONS",
    "FEATURES_DIRNAME",
    "Feature",
    "VerificationEvidence",
    "can_transition",
    "features_dir",
    "create_feature",
    "get_feature",
    "list_features",
    "claim_feature",
    "block_feature",
    "reopen_feature",
    "verify_feature",
    "record_evaluation",
    "feature_is_stale",
    "corrupt_feature_files",
    "clear_features",
]
