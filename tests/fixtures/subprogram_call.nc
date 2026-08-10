(Subprogram call. v1 does not expand M98, so the blocks it runs are not simulated and the machine)
(position afterwards is genuinely unknown - the same situation as an undrawable G28. Drawing the)
(next block would draw a straight line from wherever the main program left off to wherever the)
(subprogram happened to end. Motion resumes only once X, Y and Z are all restated absolutely,)
(and the first block that restates them is itself undrawable because its own start is unknown.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
N70 G1 X50.0 Y10.0 F600
N80 M98 P1000
N90 G1 X50.0 Y30.0
N100 G1 X10.0 Y30.0
N110 G0 X10.0 Y10.0 Z25.0
N120 G1 Z-1.0 F300
N130 G1 X50.0 Y10.0 F600
N140 G0 Z25.0
N150 M5
N160 M30
