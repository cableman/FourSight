"""``MachineState``: live position and modal groups during simulation.

Tracks G53 non-modal machine coords, G28/G30 reference return, G43/G44 with H and G49,
G54–G59 offsets, and G4 dwell (seconds, per LinuxCNC; warn if P > 60 as likely ms/s confusion).

Implemented in T2.2.
"""
