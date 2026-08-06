(Block delete. Simulated with block-delete OFF by default, so deleted blocks EXECUTE -)
(matching the common control-panel default. Exposed as a --block-delete CLI flag.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
/N70 G1 X50.0 Y10.0 F600
N80 G1 X50.0 Y30.0
/ N90 G1 X10.0 Y30.0
N100 G0 Z25.0
N110 M5
N120 M30
