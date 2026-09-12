(Spindle-synchronized motion. G33 threads at a distance per revolution rather than at F, so the)
(path is a straight line to the programmed endpoint and v1 draws it exactly right - it is the)
(only refused motion mode that is drawn. What is not modelled is the clock: the time estimate)
(for the span comes from F rather than from spindle speed and pitch. G33 is a motion mode, so)
(the bare block after one is a further pass and belongs to the same span.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S1000 M3
N50 G0 X10.0 Y10.0
N60 G1 Z-1.0 F300
N70 G33 Z-20.0 K1.5
N80 Z-25.0
N90 G0 Z25.0
N100 M5
N110 M30
