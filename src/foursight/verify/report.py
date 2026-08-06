"""``Diagnostic(severity, line, message, fix_ids)``.

Severity is three-tier, and the distinction is load-bearing:

- ``error`` — malformed, or would break the machine.
- ``unsupported`` — well-formed, recognized, **affects motion**, not interpreted by v1.
  The affected span is marked or suppressed, never drawn as if understood.
- ``warning`` — suspicious, or unrecognized but inert. Rendered normally.

An unrecognized code that never touches position is a warning. One that changes how subsequent
motion is interpreted is ``unsupported``, never a warning.

``fix_ids`` is a **list**: some diagnostics have several candidate fixes, and some fixes are tied
to no diagnostic at all. Positions are reported in the program's declared units.

Implemented in T1.6.
"""
