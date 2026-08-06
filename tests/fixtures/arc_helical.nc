(Helical arc: G2/G3 with motion on the plane-normal axis. The normal-axis component)
(interpolates linearly across the sweep. An A-word may move simultaneously.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X50.0 Y10.0
N60 G1 Z0.0 F300
N70 G2 X40.0 Y20.0 Z-2.0 I0.0 J10.0 F600
N80 G2 X30.0 Y10.0 Z-4.0 I0.0 J-10.0
N90 G3 X40.0 Y20.0 Z-6.0 A45.0 I10.0 J0.0
N100 G0 Z25.0
N110 M5
N120 M30
