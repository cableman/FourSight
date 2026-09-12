(Offset table write. G10 L2 sets a work offset to the values in its X Y Z words; the machine)
(does not move. Consuming those words as a destination both drew a feed line to them and left)
(every later block hanging off that point. v1 does not model the write either, so from this)
(line on the profile's [offsets] describe a table the program has changed - machine-coordinate)
(checks after it are reported as warnings rather than as errors they cannot stand behind.)
N10 G21 G90 G17 G94 G54
N20 G0 Z25.0
N30 T1 M6
N40 S8000 M3
N50 G10 L2 P1 X50.0 Y50.0 Z-10.0
N60 G0 X10.0 Y10.0
N70 G1 Z-1.0 F300
N80 G1 X50.0 Y10.0 F600
N90 G1 X50.0 Y30.0
N100 G0 Z25.0
N110 M5
N120 M30
