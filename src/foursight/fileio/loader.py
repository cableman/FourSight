"""File loading: encoding detection, latin-1 fallback, BOM, CRLF, large files.

Produces text plus line offsets, so ``SourceRef`` offsets are trustworthy — every downstream
editor-sync and diagnostic feature depends on them being right.

Minimum viable version in T1.4; hardened in T5.4.
"""
