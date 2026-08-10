(Coordinate rotation span. G68 rotates the coordinate system about a point, and v1 does not)
(interpret it - so the coordinates in the span are the UNROTATED ones. Unlike cutter comp, the)
(error is a rigid transform of unbounded magnitude, so the span is suppressed rather than drawn)
(and marked. Cutting either side of the span is drawn normally.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
N70 G1 X50.0 Y10.0 F600
N80 G68 X0.0 Y0.0 R45.0
N90 G1 X50.0 Y30.0
N100 G1 X10.0 Y30.0
N110 G69
N120 G1 X10.0 Y10.0
N130 G0 Z25.0
N140 M5
N150 M30
