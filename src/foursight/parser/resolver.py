"""Words → ``Command``, with modal groups resolved.

G-codes canonicalize to strings — ``G01``, ``G1`` and ``G1.0`` all become ``'1'``, and ``G90.1``
stays ``'90.1'``. Never floats. A block carries several G- and M-words, so they live in
``Command.gcodes`` / ``Command.mcodes`` lists while ``words`` holds axis/parameter letters only.

G20 (inch) input converts to mm here, at parse time, while ``ModalState.units`` records the
program's declared units so diagnostics can be reported in them.

``ModalState`` is copy-on-write: emit a new instance only when something actually changes.

Implemented in T1.3.
"""
