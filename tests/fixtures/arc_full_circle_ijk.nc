(Full circle: expressible in IJK when start == end, but NOT in R-format.)
(The IJK->R fix must refuse on this file rather than emit garbage.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X50.0 Y20.0
N60 G1 Z-1.0 F300
N70 G2 X50.0 Y20.0 I-10.0 J0.0 F600
N80 G0 Z25.0
N90 M5
N100 M30
