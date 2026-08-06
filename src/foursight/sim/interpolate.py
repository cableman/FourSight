"""Lines and arcs → segments.

G2/G3 in both IJK and R form, including helical (the plane-normal axis interpolates linearly
across the sweep, and an A-word may move simultaneously). R sign convention: positive selects
the arc ≤ 180°, negative the arc > 180°. Full circles are expressible in IJK but not in R;
R-format with coincident endpoints is an error, not a guess.

Plane-dependent IJK mapping: G17 → I,J; G18 → I,K; G19 → J,K. G18's direction convention is
counterintuitive and has its own fixture test.

Tessellation is adaptive on chord height, driven by ``tolerance.arc_chord`` — never a fixed step
count. Interpolation is per-step from the start rather than endpoint-only, so the M4 rotary
transform is a transform and not a rewrite.

Implemented in T2.3.
"""
