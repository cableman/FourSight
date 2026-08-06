"""Parse-layer dataclasses: ``SourceRef``, ``ModalState``, ``Command``.

All three use ``slots=True``; ``SourceRef`` and ``ModalState`` are additionally frozen so they
can be shared rather than copied. ``SourceRef`` holds offsets, not text — a per-line copy of
``raw`` duplicates the whole file and costs the parse-rate target.

Must not import from ``foursight.machine`` (PLAN.md § Conventions for Claude Code).

Implemented in T1.1.
"""
