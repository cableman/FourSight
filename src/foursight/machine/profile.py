"""``MachineProfile`` loaded from TOML via ``tomllib``.

Carries limits, tolerances, per-axis travel and rates, work offsets, kinematics, and safety
settings. Profile values are converted to mm on load using ``[machine].units``.

Two things that must not be got wrong:

- Refuse to load a ``rotary_mount = "head"`` profile without ``pivot_to_tip``.
- An unset work offset must stay distinguishable from ``0.0``. Work offsets live in the
  controller, not the G-code file; an *unset* offset downgrades travel-limit errors to
  warnings, so collapsing ``None`` into zero silently turns warnings into false errors.

Implemented in T1.5.
"""
