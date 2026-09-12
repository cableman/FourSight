(Probing. G38.2 searches toward its programmed endpoint and stops where it touches the part,)
(so that endpoint is a limit on the search and not where the tool ends up - the file does not)
(contain the point the move actually reaches. The move is therefore suppressed and the machine)
(position is treated as lost afterwards, as it is after an undrawable G28 or an M98. G38.x is)
(a motion mode, so the bare block after one is a second probe rather than a straight move, and)
(the first block that restates X Y and Z absolutely is itself undrawable because its own start)
(is unknown.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G0 X10.0 Y10.0
N60 G38.2 Z-5.0 F50
N70 Z-6.0
N80 G0 X10.0 Y10.0 Z25.0
N90 G1 Z-1.0 F300
N100 G1 X50.0 Y10.0 F600
N110 G0 Z25.0
N120 M5
N130 M30
