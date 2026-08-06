"""Fix transforms, each returning a text diff.

Fixes never write the original file — they modify the editor buffer and the user saves explicitly.

The contract is **one fix → full re-parse → re-verify → rebuild segments**. Applying a fix
invalidates every line number, so every ``Diagnostic`` and every ``SegmentStore.line`` entry is
stale afterwards, and a second fix's diff will not apply cleanly to an already-changed buffer.
No batch application, no diff rebasing.

Refusal is a feature: arc-center recomputation refuses beyond 10× tolerance, and IJK→R refuses
on full circles. N-word renumbering is off by default.

Implemented in T5.1–T5.3.
"""
