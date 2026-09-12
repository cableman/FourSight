(Datum shift. G92 renames the current point rather than moving to one, so its X Y Z words are)
(values and not a destination - drawing them as a move cuts a line across the part that the)
(program never commands. The shift itself is not modelled either, so every block until G92.1)
(states its coordinates against a datum v1 does not know: that span is suppressed, and drawing)
(resumes at the cancel. Cutting either side of the span is drawn normally.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
N70 G1 X50.0 Y10.0 F600
N80 G92 X0.0 Y0.0 Z0.0
N90 G1 X20.0 Y20.0
N100 G1 X0.0 Y20.0
N110 G92.1
N120 G1 X10.0 Y10.0
N130 G0 Z25.0
N140 M5
N150 M30
