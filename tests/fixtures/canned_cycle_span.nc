(Canned cycle span. Under an active G81 a block of bare X/Y is a full drill cycle, not a)
(linear move. v1 detects the cycle, suppresses motion geometry until G80, and emits one)
(`unsupported` diagnostic per cycle span - never a straight line through the hole positions.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G81 Z-5.0 R2.0 F300
N70 X20.0 Y10.0
N80 X30.0 Y10.0
N90 X40.0 Y10.0
N100 G80
N110 G0 Z25.0
N120 G1 X50.0 Y10.0 F600
N130 G0 Z25.0
N140 M5
N150 M30
