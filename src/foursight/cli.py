"""Headless CLI: ``foursight parse`` and ``foursight check``.

Flags ``--profile`` and ``--block-delete`` (default off, meaning deleted blocks execute, matching
the common control-panel default). Exits non-zero when errors are present.

Must import no Qt — this path is the M1 deliverable and runs without the ``[gui]`` extra.

Implemented in T1.11.
"""


def main() -> int:
    """Console-script entry point declared in pyproject.toml."""
    raise NotImplementedError("The foursight CLI is implemented in T1.11 — see TASKS.md.")
