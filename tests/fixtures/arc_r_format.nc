(R-format sign convention: positive R selects the arc <= 180 degrees, negative R the arc > 180.)
(Both arcs below share the same endpoints, so only the sign of R distinguishes them.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X40.0 Y20.0
N60 G1 Z-1.0 F300
N70 G2 X60.0 Y20.0 R10.0 F600
N80 G0 Z25.0
N90 G0 X40.0 Y20.0
N100 G1 Z-1.0 F300
N110 G2 X60.0 Y20.0 R-10.0 F600
N120 G0 Z25.0
N130 M5
N140 M30
