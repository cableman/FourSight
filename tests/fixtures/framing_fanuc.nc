%
O2345 (Fanuc framing: leading and trailing %, plus an Oxxxx program number.)
(All three are consumed silently and must never be flagged.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G1 Z-1.0 F300
N60 G1 X10.0 Y10.0 F600
N70 G0 Z25.0
N80 M5
N90 M30
%
