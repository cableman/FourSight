"""Controller dialects: the starting conditions a program cannot state for itself.

LinuxCNC is normative (PLAN.md § Reference dialect). A dialect exists for the handful of settings
that live in the *controller* rather than in the G-code. Mach3's arc-centre mode is the motivating
case: it is a radio button in Config → General ("IJ Mode"), so no amount of reading the file can
tell us which one is set, and guessing wrong draws every arc in the program wrongly with no
diagnostic at all — the confidently-wrong output the plan exists to prevent.

This module lives in ``parser/`` because ``parse()`` needs it and ``parser/`` must not import
``machine/`` (PLAN.md § Conventions). ``MachineProfile`` builds one of these from its ``[dialect]``
section, which is the legal direction.

``Dialect`` is a **value, not a flag**: the next divergence becomes a field here rather than another
keyword argument on ``parse``. ``block_delete`` stays a separate keyword on purpose — it is a
control-panel switch an operator flips per run, not part of the controller's identity.
"""

from dataclasses import dataclass
from enum import StrEnum

from foursight.parser.model import DEFAULT_ARC_DISTANCE

#: The two spellings of arc-centre mode, as ``ModalState.arc_distance`` values.
ARC_CENTRE_INCREMENTAL = "91.1"
ARC_CENTRE_ABSOLUTE = "90.1"

#: The human spellings used in a profile's ``[dialect].arc_centre``, and their G-code equivalents.
ARC_CENTRE_CODES: dict[str, str] = {
    "incremental": ARC_CENTRE_INCREMENTAL,
    "absolute": ARC_CENTRE_ABSOLUTE,
}


class DialectName(StrEnum):
    LINUXCNC = "linuxcnc"
    MACH3 = "mach3"


class DwellUnits(StrEnum):
    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"


@dataclass(slots=True, frozen=True)
class Dialect:
    """What the parse and simulation layers need to know about the target controller.

    ``arc_distance`` is the value ``ModalState.arc_distance`` *starts* at. It sets the start, never a
    lock: an explicit G90.1 or G91.1 in the program still switches, because a program that states
    its arc mode is unambiguous and no controller setting overrides it.

    ``dwell_units`` is read by ``machine/state.py``, not by the parser. It is here rather than on
    ``MachineProfile`` alone so that everything a *program's interpretation* depends on sits in one
    value, and so ``parse`` and the stepper cannot be given different answers.
    """

    name: str = DialectName.LINUXCNC
    arc_distance: str = DEFAULT_ARC_DISTANCE
    dwell_units: str = DwellUnits.SECONDS
    #: Inch/mm spellings this dialect accepts beyond G20/G21, as ``((code, 'inch' | 'mm'), ...)``.
    #: A tuple of pairs rather than a dict, so ``Dialect`` stays frozen and hashable.
    unit_aliases: tuple[tuple[str, str], ...] = ()
    #: Whether G80 conflicts with a motion code in the same block. LinuxCNC puts G80 in modal
    #: group 1 with G0/G1/G2/G3, so `G0 G80` is an error there.
    cycle_cancel_conflicts: bool = True

    @property
    def extra_gcodes(self) -> frozenset[str]:
        """Codes this dialect interprets beyond the normative subset, so `verify` stays quiet."""
        return frozenset(code for code, _ in self.unit_aliases)


#: The normative dialect, and the default everywhere.
LINUXCNC = Dialect(name=DialectName.LINUXCNC)

#: Mach3. Its *controller settings* default to the same values LinuxCNC uses — it ships with
#: incremental I/J and specifies G4 P in seconds — and what the name buys there is the ability to
#: say otherwise, since `[dialect].arc_centre` and `[dialect].dwell_units` are only meaningful once
#: a dialect admits they are settings. Its **code table** genuinely differs:
#:
#: - **G70/G71 are Mach3's inch/mm codes**, documented alongside G20/G21. Vectric's Mach2/3 post
#:   emits `G71` for metric on the line after `G21`, and under LinuxCNC that is an unknown code.
#:   Deliberately *not* interpreted globally: in Fanuc, G71 is a turning roughing cycle — very much
#:   motion-affecting — so reading it as "millimetres" is only safe once the user has named Mach3.
#: - **G80 does not conflict with a motion code.** `G00 G21 G17 G90 G40 G49 G80` is the safe-start
#:   line nearly every post emits, Mach3 accepts it, and it is unambiguous: G80 cancels any canned
#:   cycle while G0 selects the motion mode. LinuxCNC does treat it as a group-1 conflict, and stays
#:   strict, because it is the normative dialect.
MACH3 = Dialect(
    name=DialectName.MACH3,
    unit_aliases=(("70", "inch"), ("71", "mm")),
    cycle_cancel_conflicts=False,
)

PRESETS: dict[str, Dialect] = {
    DialectName.LINUXCNC: LINUXCNC,
    DialectName.MACH3: MACH3,
}

DIALECT_NAMES: tuple[str, ...] = tuple(PRESETS)


def preset(name: str) -> Dialect:
    """The canonical dialect for a name.

    ``ValueError`` for anything else, never a silent fallback to LinuxCNC: a typo'd dialect that
    quietly selected the default would draw a Mach3 program under LinuxCNC rules and say nothing.
    Callers that need a domain error (``ProfileError``, an argparse usage error) translate it.
    """
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown dialect {name!r}; expected one of {', '.join(PRESETS)}"
        ) from None
