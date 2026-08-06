%
O1000 (FourSight baseline: known-clean 4-axis program)
(Verified against profiles/default_4axis.toml. Every rule in PLAN.md's checklist must pass here.)
(Broken fixtures are derived from THIS file by exactly one mutation - see tests/conftest.py.)
N10 G21 G90 G17 G94 G54
N20 G91.1
N30 G0 Z25.0
N40 T1 M6
N50 S8000 M3
N60 M8
N70 G0 X10.0 Y10.0
N80 G1 Z-1.0 F300
N90 G1 X50.0 Y10.0 F600
N100 G2 X40.0 Y20.0 I0.0 J10.0
N110 G3 X50.0 Y30.0 I0.0 J10.0
N120 G1 X10.0 Y30.0
N130 G1 A90.0 F1800
N140 G1 X50.0 A180.0
N150 G0 Z25.0
N160 M9
N170 M5
N180 G0 X0.0 Y0.0
N190 M30
%
