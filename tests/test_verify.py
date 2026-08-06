"""Verifier infrastructure tests (T1.6): Diagnostic, severity, formatting, registry, driver.

The individual checks arrive in T1.7–T1.9 and have their own tests against the fixture corpus.
"""

import dataclasses
import sys
from pathlib import Path

import pytest

from foursight.machine.profile import MachineProfile
from foursight.parser.model import ParseError
from foursight.parser.resolver import parse
from foursight.verify import checks as checks_package
from foursight.verify.report import (
    Diagnostic,
    Severity,
    format_angle,
    format_feed,
    format_length,
    sort_diagnostics,
)
from foursight.verify.rules import (
    Program,
    Rule,
    isolated_registry,
    load_builtin_checks,
    register_rule,
    registered_rules,
    verify,
)


def make_program(text: str = "G21 G90 G1 X1 F100\n", profile: MachineProfile | None = None):
    result = parse(text)
    return Program(
        commands=result.commands,
        profile=profile or MachineProfile(),
        parse_errors=result.errors,
    )


def diag(rule_id: str = "test.rule", line: int = 1, severity: Severity = Severity.WARNING):
    return Diagnostic(rule_id=rule_id, severity=severity, line=line, message="m")


# --------------------------------------------------------------------------- severity


def test_severity_has_exactly_the_three_documented_tiers() -> None:
    assert [member.value for member in Severity] == ["error", "unsupported", "warning"]


def test_severity_is_a_string_enum_for_cli_output() -> None:
    assert Severity.UNSUPPORTED == "unsupported"
    assert f"{Severity.ERROR}" == "error"


def test_is_error_distinguishes_error_from_unsupported() -> None:
    """`unsupported` is not an error: the program is fine, we just cannot interpret that span."""
    assert diag(severity=Severity.ERROR).is_error is True
    assert diag(severity=Severity.UNSUPPORTED).is_error is False
    assert diag(severity=Severity.WARNING).is_error is False


# --------------------------------------------------------------------------- Diagnostic


def test_diagnostic_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        diag().line = 2  # type: ignore[misc]


def test_diagnostic_is_hashable_so_fixture_tests_can_diff_sets() -> None:
    """The baseline-plus-mutation strategy diffs sets of diagnostics, which needs hashability."""
    baseline = {diag("a"), diag("b")}
    mutated = {diag("a"), diag("b"), diag("c")}
    assert mutated - baseline == {diag("c")}


def test_fix_ids_default_to_empty_and_are_a_tuple() -> None:
    """Plural per PLAN.md; a tuple rather than a list so the dataclass can stay frozen."""
    assert diag().fix_ids == ()
    assert isinstance(Diagnostic("r", Severity.WARNING, 1, "m", ("f1", "f2")).fix_ids, tuple)


def test_diagnostic_carries_an_optional_offset() -> None:
    assert diag().offset is None
    assert Diagnostic("r", Severity.WARNING, 1, "m", (), 42).offset == 42


def test_equal_diagnostics_compare_equal() -> None:
    assert diag("a", 3) == diag("a", 3)
    assert diag("a", 3) != diag("a", 4)


# --------------------------------------------------------------------------- ordering


def test_sorted_by_line_then_severity_then_rule_id() -> None:
    unsorted = [
        diag("z.rule", 5, Severity.WARNING),
        diag("a.rule", 1, Severity.WARNING),
        diag("b.rule", 1, Severity.ERROR),
        diag("c.rule", 1, Severity.UNSUPPORTED),
    ]
    assert [(d.line, str(d.severity), d.rule_id) for d in sort_diagnostics(unsorted)] == [
        (1, "error", "b.rule"),
        (1, "unsupported", "c.rule"),
        (1, "warning", "a.rule"),
        (5, "warning", "z.rule"),
    ]


def test_sorting_is_stable_for_identical_keys() -> None:
    same = [diag("a", 1), diag("a", 1)]
    assert sort_diagnostics(same) == same


# --------------------------------------------------------------------------- unit formatting


def test_lengths_render_in_mm_for_a_metric_program() -> None:
    assert format_length(400.0, "mm") == "400 mm"
    assert format_length(0.005, "mm") == "0.005 mm"


def test_lengths_render_in_inches_for_an_inch_program() -> None:
    """ "X exceeds 400 mm" against an inch program is not actionable (PLAN.md § Verifier Rules)."""
    assert format_length(25.4, "inch") == "1 in"
    assert format_length(400.0, "inch") == "15.7480 in".replace("15.7480", "15.748")


def test_inch_gets_more_decimals_than_mm() -> None:
    """One inch is 25.4 mm, so equal decimal counts would lose precision where it matters."""
    assert format_length(1.0, "mm") == "1 mm"
    assert format_length(1.0, "inch") == "0.0394 in"


def test_feed_rates_render_per_minute_in_declared_units() -> None:
    assert format_feed(3000.0, "mm") == "3000 mm/min"
    assert format_feed(254.0, "inch") == "10 in/min"


def test_angles_have_no_units_parameter_at_all() -> None:
    """Rotary values are degrees in every unit mode; a parameter here could only be misused."""
    assert format_angle(90.0) == "90 deg"
    assert format_angle(-360.0) == "-360 deg"
    assert format_angle(0.0) == "0 deg"


def test_trailing_zeros_are_trimmed_but_zero_survives() -> None:
    assert format_length(0.0, "mm") == "0 mm"
    assert format_length(1.500, "mm") == "1.5 mm"


def test_tiny_value_does_not_render_as_a_bare_minus() -> None:
    """`-0.0001` at 3 decimals rounds to '-0.000'; trimming that naively yields '-'."""
    assert format_length(-0.0001, "mm") == "0 mm"


# --------------------------------------------------------------------------- registry


def test_register_and_list() -> None:
    with isolated_registry():

        @register_rule
        class Second(Rule):
            rule_id = "b.second"

        @register_rule
        class First(Rule):
            rule_id = "a.first"

        assert [rule.rule_id for rule in registered_rules()] == ["a.first", "b.second"]


def test_registration_is_ordered_by_id_not_definition_order() -> None:
    """A reproducible run order keeps diagnostics stable between invocations."""
    with isolated_registry():

        @register_rule
        class Z(Rule):
            rule_id = "zzz"

        @register_rule
        class A(Rule):
            rule_id = "aaa"

        assert [rule.rule_id for rule in registered_rules()][:2] == ["aaa", "zzz"]


def test_duplicate_rule_id_is_refused() -> None:
    """A silent collision would make one of the two rules invisible."""
    with isolated_registry():

        @register_rule
        class One(Rule):
            rule_id = "same.id"

        with pytest.raises(ValueError, match="duplicate rule_id"):

            @register_rule
            class Two(Rule):
                rule_id = "same.id"


def test_missing_rule_id_is_refused() -> None:
    with isolated_registry(), pytest.raises(ValueError, match="rule_id"):

        @register_rule
        class Nameless(Rule):
            pass


def test_registering_the_same_class_twice_is_harmless() -> None:
    with isolated_registry():

        class Once(Rule):
            rule_id = "idempotent"

        register_rule(Once)
        register_rule(Once)
        assert len([r for r in registered_rules() if r.rule_id == "idempotent"]) == 1


def test_isolated_registry_restores_the_real_rules() -> None:
    """Clearing without restoring would leave every later test with an empty registry."""
    before = registered_rules()
    with isolated_registry():

        @register_rule
        class Temp(Rule):
            rule_id = "temp.only"

        assert [r.rule_id for r in registered_rules()] == ["temp.only"]
    assert registered_rules() == before


def test_base_rule_check_must_be_overridden() -> None:
    with pytest.raises(NotImplementedError):
        Rule().check(make_program())


# --------------------------------------------------------------------------- registration wiring


def test_load_builtin_checks_actually_imports_the_package() -> None:
    """Guards a silent failure mode: `ruff --fix` once deleted this import as unused.

    An import kept only for its `@register_rule` side effect looks unused to a linter. With it gone,
    the registry stays empty and the verifier reports nothing — indistinguishable from a clean
    program.
    """
    sys.modules.pop("foursight.verify.checks", None)
    load_builtin_checks()
    assert "foursight.verify.checks" in sys.modules


def test_every_check_module_is_imported_by_the_package() -> None:
    """A check module missing from `checks/__init__.py` registers nothing and never runs."""
    directory = Path(checks_package.__file__).parent
    modules = {path.stem for path in directory.glob("*.py")} - {"__init__"}
    source = Path(checks_package.__file__).read_text(encoding="utf-8")
    missing = {name for name in modules if name not in source}
    assert not missing, f"check modules not imported in checks/__init__.py: {sorted(missing)}"


# --------------------------------------------------------------------------- driver


def test_verify_collects_from_every_rule_and_sorts() -> None:
    class Late(Rule):
        rule_id = "late"

        def check(self, program):
            return [diag("late", 9)]

    class Early(Rule):
        rule_id = "early"

        def check(self, program):
            return [diag("early", 2)]

    found = verify(make_program(), rules=[Late, Early])
    assert [d.line for d in found] == [2, 9]


def test_verify_with_no_rules_returns_nothing() -> None:
    assert verify(make_program(), rules=[]) == []


def test_a_broken_rule_does_not_suppress_the_others() -> None:
    """The verifier's job is to list every problem it can find, so one bad rule cannot end the run."""

    class Broken(Rule):
        rule_id = "broken"

        def check(self, program):
            raise RuntimeError("boom")

    class Working(Rule):
        rule_id = "working"

        def check(self, program):
            return [diag("working", 3)]

    found = verify(make_program(), rules=[Broken, Working])
    ids = [d.rule_id for d in found]
    assert "working" in ids
    assert "internal.rule-failed" in ids
    failure = next(d for d in found if d.rule_id == "internal.rule-failed")
    assert failure.severity is Severity.ERROR
    assert "boom" in failure.message


def test_a_generator_rule_that_raises_midway_is_still_contained() -> None:
    """`check` may be a generator, so the exception surfaces during iteration, not at call time."""

    class HalfBroken(Rule):
        rule_id = "half"

        def check(self, program):
            yield diag("half", 1)
            raise ValueError("late failure")

    found = verify(make_program(), rules=[HalfBroken])
    # The contract is that the diagnostics yielded *before* the failure survive. Order is by
    # severity within a line, so the ERROR about the broken rule sorts ahead of its own warning.
    assert {d.rule_id for d in found} == {"half", "internal.rule-failed"}
    assert [str(d.severity) for d in found] == ["error", "warning"]


# --------------------------------------------------------------------------- Program


def test_program_carries_parse_errors_unconverted() -> None:
    """Choosing a severity for a parse error is a check's judgment (T1.7), not the driver's."""
    program = make_program("G1 X#\n")
    assert program.parse_errors
    assert all(isinstance(error, ParseError) for error in program.parse_errors)


def test_units_at_reports_the_units_in_force_at_that_command() -> None:
    """A program may switch G20/G21 mid-file, so there is no single program-wide unit."""
    program = make_program("G20 X1\nG21 X1\n")
    assert program.units_at(0) == "inch"
    assert program.units_at(1) == "mm"


def test_units_at_is_safe_on_an_empty_program() -> None:
    assert make_program("(only a comment)\n").units_at(0) == "mm"


def test_units_at_clamps_out_of_range_indices() -> None:
    program = make_program("G20 X1\n")
    assert program.units_at(99) == "inch"
    assert program.units_at(-5) == "inch"


def test_program_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_program().profile = MachineProfile()  # type: ignore[misc]
