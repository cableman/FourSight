(G18 arc direction. PLAN.md: G18's convention is counterintuitive - in the XZ plane,)
(a G2 appears counter-clockwise when viewed from +Y, so this needs its own fixture.)
(G18 maps IJK as I,K - never J. Quarter arc from X50 Z0 to X40 Z-10 about X50 Z-10.)
N10 G21 G90 G18 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X50.0 Y0.0
N60 G1 Z0.0 F300
N70 G2 X40.0 Z-10.0 I0.0 K-10.0
N80 G3 X30.0 Z0.0 I-10.0 K0.0
N90 G0 Z25.0
N100 M5
N110 M30
