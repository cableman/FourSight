(G20 inch program. Converted to mm at parse time, but diagnostics must report INCHES:)
("X exceeds 400 mm" against this file would not be actionable.)
N10 G20 G90 G17 G94 G54
N20 G0 Z1.0
N30 T1 M6
N40 S8000 M3
N50 G0 X0.5 Y0.5
N60 G1 Z-0.04 F12.0
N70 G1 X2.0 Y0.5 F24.0
N80 G2 X1.6 Y0.9 I0.0 J0.4
N90 G1 A90.0 F72.0
N100 G0 Z1.0
N110 M5
N120 M30
