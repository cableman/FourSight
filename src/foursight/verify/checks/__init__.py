"""One module per check category.

- ``structural`` — syntax, modal-group conflicts, unknown codes, unsupported motion codes (T1.7)
- ``process`` — feed, spindle, offsets, tool changes, coolant, program framing (T1.8)
- ``geometry`` — arc validity, travel limits, rotary limits (T1.9, extended in T2.8)

**Every check module must be imported here.** ``@register_rule`` only runs when the defining module
is imported, and `rules.load_builtin_checks()` imports just this package — so a module missing from
the list below registers nothing and its checks silently never run. `test_every_check_module_is_
imported` in `tests/test_verify.py` compares this against the files on disk.
"""

# ruff: noqa: F401 — imported for the registration side effect, not for use here.
from foursight.verify.checks import structural
