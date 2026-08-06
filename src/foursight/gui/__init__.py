"""Qt/PySide6 layer — the only package permitted to import Qt.

Requires the ``[gui]`` extra. Kept deliberately thin so that all logic stays testable outside Qt;
GUI verification is a manual test script per milestone.

This ``__init__`` must stay import-light: importing ``foursight.gui`` should not drag in Qt, so
that tooling can enumerate the package without the extra installed.
"""
