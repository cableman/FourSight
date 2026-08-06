"""``Rule`` base class, the registry checks register into, and the ``verify`` driver.

A rule sees the whole ``Program`` rather than one command at a time, because many checks are
program-scoped — "no work offset before motion", "program lacks M30", "G91 active at program end"
cannot be answered from a single block. Rules that only care about individual commands iterate
themselves.

Registration happens at class-definition time via ``@register_rule``. Nothing is registered until
the module defining it is imported, so ``verify`` imports ``foursight.verify.checks`` lazily —
a module-level import here would be circular, since every check module imports this one.
"""

import importlib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from foursight.machine.profile import MachineProfile
from foursight.parser.model import Command, ParseError
from foursight.sim.segments import SegmentStore
from foursight.verify.report import Diagnostic, Severity, sort_diagnostics


@dataclass(slots=True, frozen=True)
class Program:
    """Everything a rule may look at.

    ``parse_errors`` is carried rather than pre-converted: turning a `ParseError` into a
    `Diagnostic` means choosing a severity, and that is a check's judgment (T1.7), not the driver's.

    There is deliberately no program-wide ``units`` field. A program may switch between G20 and G21
    mid-file, so the units that matter for a message are the ones in force at the offending block —
    available as ``command.modal_snapshot.units``.
    """

    commands: Sequence[Command]
    profile: MachineProfile
    parse_errors: Sequence[ParseError] = field(default_factory=tuple)
    block_delete: bool = False  # the mode the program was parsed under
    # Interpolated geometry, when a simulation has been run. Optional because `foursight check` must
    # work without one; travel limits fall back to block endpoints when it is absent, which cannot
    # see an arc that bulges past a limit mid-sweep (T2.8).
    segments: "SegmentStore | None" = None

    def units_at(self, index: int) -> str:
        """Declared units in force at `commands[index]`, for formatting that command's message."""
        if not self.commands:
            return "mm"
        return self.commands[max(0, min(index, len(self.commands) - 1))].modal_snapshot.units


class Rule:
    """Base class for a check.

    Subclasses set ``rule_id`` and ``description`` and implement ``check``. ``severity`` is the
    rule's *usual* tier; several rules legitimately vary it per finding — a travel-limit violation
    is an ``error`` normally but a ``warning`` when the work offset is unknown — so ``check`` sets
    the severity on each `Diagnostic` rather than inheriting it implicitly.
    """

    rule_id: str = ""
    description: str = ""
    severity: Severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.rule_id!r}>"


_REGISTRY: dict[str, type[Rule]] = {}


def register_rule(rule_class: type[Rule]) -> type[Rule]:
    """Class decorator adding a rule to the registry.

    Rejects a missing or duplicate ``rule_id``: ids are what tests assert on and what any future
    suppression keys on, so a silent collision would make one of two rules invisible.
    """
    rule_id = rule_class.rule_id
    if not rule_id:
        raise ValueError(f"{rule_class.__name__} must define a non-empty rule_id")
    existing = _REGISTRY.get(rule_id)
    if existing is not None and existing is not rule_class:
        raise ValueError(
            f"duplicate rule_id {rule_id!r}: {rule_class.__name__} collides with {existing.__name__}"
        )
    _REGISTRY[rule_id] = rule_class
    return rule_class


def registered_rules() -> tuple[type[Rule], ...]:
    """All registered rule classes, ordered by id so a run is reproducible."""
    load_builtin_checks()
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def load_builtin_checks() -> None:
    """Import the shipped check modules so their ``@register_rule`` decorators run.

    Late rather than at module scope because every check module imports this one, so a top-level
    import would be circular.

    ``importlib.import_module`` rather than a plain ``import`` statement, because an import kept
    purely for its registration side effect looks unused: ``ruff check --fix`` deleted exactly that
    line as F401, leaving this function a no-op and the registry permanently empty. A verifier that
    finds nothing is indistinguishable from a clean program, so that failure would have been silent.
    ``test_load_builtin_checks_actually_imports_the_package`` guards it.
    """
    importlib.import_module("foursight.verify.checks")


def verify(program: Program, *, rules: Iterable[type[Rule]] | None = None) -> list[Diagnostic]:
    """Run every rule over the program and return diagnostics in presentation order.

    A rule that raises is not allowed to take the whole report down with it — the point of the
    verifier is to list every problem it *can* find, so a broken rule becomes one diagnostic about
    itself and the rest still run.
    """
    selected = tuple(rules) if rules is not None else registered_rules()
    found: list[Diagnostic] = []
    for rule_class in selected:
        rule = rule_class()
        try:
            found.extend(rule.check(program))
        # Bare `Exception` is intentional: a broken rule must not hide the other findings.
        except Exception as exc:
            found.append(
                Diagnostic(
                    rule_id="internal.rule-failed",
                    severity=Severity.ERROR,
                    line=1,
                    message=f"rule {rule.rule_id!r} failed: {type(exc).__name__}: {exc}",
                )
            )
    return sort_diagnostics(found)


@contextmanager
def isolated_registry() -> Iterator[None]:
    """Run with an empty registry, restoring the real one afterwards. Test-only.

    A plain `clear()` would be a trap: the check modules are already imported, so their decorators
    never run again and every later test would see an empty registry. Snapshot-and-restore cannot
    leak that way.
    """
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)
