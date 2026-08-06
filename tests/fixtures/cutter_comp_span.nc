(Cutter compensation span. While comp is active the real path is offset by the tool radius;)
(v1 renders the programmed centerline and marks the span rather than pretending to know better.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
N70 G41 D1
N80 G1 X50.0 Y10.0 F600
N90 G1 X50.0 Y30.0
N100 G40
N110 G1 X10.0 Y30.0
N120 G0 Z25.0
N130 M5
N140 M30
