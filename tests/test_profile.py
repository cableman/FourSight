"""Machine profile tests (T1.5).

The DoD list — the shipped profile loads, head-mount-without-pivot is refused, unset offsets stay
distinct from zero, inch conversion, unknown keys — plus the mm/degrees split, which is where a
silent corruption would hide.
"""

import dataclasses
from pathlib import Path

import pytest

from foursight.machine.profile import (
    INCH_TO_MM,
    MachineProfile,
    ProfileError,
    WorkOffset,
    default_profile_path,
    load_profile,
    load_profile_text,
)

DEFAULT_PROFILE = default_profile_path()

MINIMAL = """
[machine]
name = "test"
units = "mm"
"""


# --------------------------------------------------------------------------- the shipped profile


def test_default_profile_loads() -> None:
    profile = load_profile(DEFAULT_PROFILE)
    assert profile.name == "Generic 4-axis mill"
    assert profile.declared_units == "mm"
    assert profile.path == DEFAULT_PROFILE


def test_default_profile_has_no_unknown_keys() -> None:
    """A typo in the shipped profile would silently disable a check."""
    assert load_profile(DEFAULT_PROFILE).unknown_keys == ()


def test_default_profile_sections() -> None:
    profile = load_profile(DEFAULT_PROFILE)
    assert profile.limits.max_feed == 3000.0
    assert profile.limits.max_spindle_rpm == 24000.0
    assert profile.limits.rotary_wrap_warn == 360.0
    assert profile.tolerance.arc_chord == 0.01
    assert profile.tolerance.arc_radius_mismatch == 0.005
    assert profile.tolerance.rotary_chord == 0.01
    assert set(profile.axes) == {"X", "Y", "Z", "A"}
    assert profile.kinematics.rotary_mount == "table"
    assert profile.kinematics.rotary_axis == "x"
    assert profile.kinematics.centerline_offset == (0.0, 0.0, 50.0)
    assert profile.safety.min_clearance_z == 5.0
    assert profile.safety.retract_before_toolchange is True


def test_default_profile_rotary_axis_is_marked_rotary() -> None:
    axis = load_profile(DEFAULT_PROFILE).axes["A"]
    assert axis.is_rotary is True
    assert axis.wrap is True
    assert axis.max_rapid == 3600.0  # deg/min, not scaled


# --------------------------------------------------------------------------- unset vs zero


def test_unset_offset_is_none_not_zero() -> None:
    """This distinction is what downgrades travel-limit violations to warnings."""
    profile = load_profile_text(MINIMAL + "\n[offsets]\ng54 = [1.0, 2.0, 3.0, 4.0]\n")
    assert profile.offset("54") == WorkOffset(1.0, 2.0, 3.0, 4.0)
    assert profile.offset("55") is None
    assert profile.offset("59") is None


def test_offset_set_to_zero_is_not_the_same_as_unset() -> None:
    profile = load_profile_text(MINIMAL + "\n[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n")
    assert profile.offset("54") == WorkOffset(0.0, 0.0, 0.0, 0.0)
    assert profile.offset("54") is not None, "set-to-zero must not read as unset"
    assert profile.offset("55") is None


def test_no_offsets_section_means_all_unset() -> None:
    profile = load_profile_text(MINIMAL)
    assert profile.offsets == {}
    assert all(profile.offset(code) is None for code in ("54", "55", "56", "57", "58", "59"))


def test_offset_lookup_keys_match_modal_state_codes() -> None:
    """`ModalState.offset` holds '54', so the profile is keyed the same way — no conversion."""
    profile = load_profile_text(MINIMAL + "\n[offsets]\ng57 = [1.0, 0.0, 0.0]\n")
    assert set(profile.offsets) == {"57"}
    assert profile.offset(None) is None


def test_three_component_offset_defaults_a_to_zero() -> None:
    profile = load_profile_text(MINIMAL + "\n[offsets]\ng54 = [1.0, 2.0, 3.0]\n")
    assert profile.offset("54") == WorkOffset(1.0, 2.0, 3.0, 0.0)


def test_malformed_offset_is_refused() -> None:
    with pytest.raises(ProfileError, match="3 or 4"):
        load_profile_text(MINIMAL + "\n[offsets]\ng54 = [1.0, 2.0]\n")


# --------------------------------------------------------------------------- head mount


def test_head_mount_without_pivot_to_tip_is_refused() -> None:
    """The tip translates as the head swings, so its path is unknowable without this distance."""
    with pytest.raises(ProfileError, match="pivot_to_tip"):
        load_profile_text(MINIMAL + "\n[kinematics]\nrotary_mount = 'head'\n")


def test_head_mount_with_pivot_to_tip_loads() -> None:
    profile = load_profile_text(
        MINIMAL + "\n[kinematics]\nrotary_mount = 'head'\npivot_to_tip = 120.0\n"
    )
    assert profile.kinematics.rotary_mount == "head"
    assert profile.kinematics.pivot_to_tip == 120.0


def test_table_mount_does_not_need_pivot_to_tip() -> None:
    profile = load_profile_text(MINIMAL + "\n[kinematics]\nrotary_mount = 'table'\n")
    assert profile.kinematics.pivot_to_tip is None


def test_unknown_rotary_mount_is_refused() -> None:
    with pytest.raises(ProfileError, match="rotary_mount"):
        load_profile_text(MINIMAL + "\n[kinematics]\nrotary_mount = 'gantry'\n")


def test_unknown_rotary_axis_is_refused() -> None:
    with pytest.raises(ProfileError, match="rotary_axis"):
        load_profile_text(MINIMAL + "\n[kinematics]\nrotary_axis = 'w'\n")


# --------------------------------------------------------------------------- inch conversion


INCH = """
[machine]
units = "inch"

[limits]
max_feed = 100.0
max_spindle_rpm = 24000.0

[tolerance]
arc_chord = 0.001
rotary_chord = 0.001
arc_radius_mismatch = 0.0002

[axes.x]
min = 0.0
max = 10.0
max_rapid = 200.0

[axes.a]
type = "rotary"
min = -360.0
max = 360.0
max_rapid = 3600.0

[offsets]
g54 = [1.0, 2.0, 3.0, 90.0]

[kinematics]
centerline_offset = [0.0, 0.0, 2.0]

[safety]
min_clearance_z = 0.2
"""


def test_inch_profile_converts_lengths() -> None:
    profile = load_profile_text(INCH)
    assert profile.declared_units == "inch"
    assert profile.limits.max_feed == pytest.approx(100.0 * INCH_TO_MM)
    assert profile.axes["X"].max == pytest.approx(10.0 * INCH_TO_MM)
    assert profile.axes["X"].max_rapid == pytest.approx(200.0 * INCH_TO_MM)
    assert profile.safety.min_clearance_z == pytest.approx(0.2 * INCH_TO_MM)
    assert profile.kinematics.centerline_offset[2] == pytest.approx(2.0 * INCH_TO_MM)


def test_inch_profile_converts_tolerances() -> None:
    """rotary_chord is a chord height in mm despite its name, so it scales too."""
    profile = load_profile_text(INCH)
    assert profile.tolerance.arc_chord == pytest.approx(0.001 * INCH_TO_MM)
    assert profile.tolerance.rotary_chord == pytest.approx(0.001 * INCH_TO_MM)
    assert profile.tolerance.arc_radius_mismatch == pytest.approx(0.0002 * INCH_TO_MM)


def test_inch_profile_never_scales_degrees() -> None:
    """The mm/degrees split again: scaling A by 25.4 would corrupt every rotary limit."""
    profile = load_profile_text(INCH)
    axis = profile.axes["A"]
    assert (axis.min, axis.max, axis.max_rapid) == (-360.0, 360.0, 3600.0)


def test_inch_profile_scales_offset_lengths_but_not_its_angle() -> None:
    """`g54 = [x, y, z, a]` is three lengths and one angle in a single list."""
    offset = load_profile_text(INCH).offset("54")
    assert offset.x == pytest.approx(1.0 * INCH_TO_MM)
    assert offset.z == pytest.approx(3.0 * INCH_TO_MM)
    assert offset.a == 90.0, "A is degrees and must not be scaled"


def test_inch_profile_does_not_scale_spindle_rpm() -> None:
    assert load_profile_text(INCH).limits.max_spindle_rpm == 24000.0


def test_unknown_units_is_refused() -> None:
    with pytest.raises(ProfileError, match="units"):
        load_profile_text("[machine]\nunits = 'furlongs'\n")


# --------------------------------------------------------------------------- absence vs default


def test_absent_limits_are_none_not_invented() -> None:
    """An invented max_feed would produce confident diagnostics about an unknown machine."""
    profile = load_profile_text(MINIMAL)
    assert profile.limits.max_feed is None
    assert profile.limits.max_spindle_rpm is None
    assert profile.limits.max_plunge_feed is None
    assert profile.safety.min_clearance_z is None
    assert profile.stock is None
    assert profile.axes == {}


def test_tolerances_do_have_defaults() -> None:
    """Tessellation cannot proceed without a number, so these are the one exception."""
    tolerance = load_profile_text(MINIMAL).tolerance
    assert tolerance.arc_chord == 0.01
    assert tolerance.rotary_chord == 0.01
    assert tolerance.arc_radius_mismatch == 0.005


def test_zero_is_a_real_value_not_a_missing_one() -> None:
    """`value or default` would silently turn `rotary_wrap_warn = 0` into 360."""
    profile = load_profile_text(MINIMAL + "\n[limits]\nrotary_wrap_warn = 0.0\nmax_feed = 0.0\n")
    assert profile.limits.rotary_wrap_warn == 0.0
    assert profile.limits.max_feed == 0.0


def test_defaults_apply_with_no_sections_at_all() -> None:
    profile = load_profile_text("")
    assert profile.name == "unnamed"
    assert profile.declared_units == "mm"
    assert profile.kinematics.rotary_mount == "table"
    assert profile.safety.require_spindle_before_cut is True


# --------------------------------------------------------------------------- unknown keys


def test_unknown_key_is_reported_not_ignored() -> None:
    """`max_fed = 3000` is a typo that would otherwise silently disable the feed check."""
    profile = load_profile_text(MINIMAL + "\n[limits]\nmax_fed = 3000.0\n")
    assert "limits.max_fed" in profile.unknown_keys
    assert profile.limits.max_feed is None


def test_unknown_section_is_reported() -> None:
    profile = load_profile_text(MINIMAL + "\n[toolchanger]\ncapacity = 20\n")
    assert "toolchanger" in profile.unknown_keys


def test_unknown_axis_key_is_reported() -> None:
    profile = load_profile_text(MINIMAL + "\n[axes.x]\nmax = 1.0\nbacklash = 0.01\n")
    assert "axes.x.backlash" in profile.unknown_keys
    assert profile.axes["X"].max == 1.0


def test_unknown_offset_key_is_reported() -> None:
    profile = load_profile_text(MINIMAL + "\n[offsets]\ng99 = [1.0, 2.0, 3.0]\n")
    assert "offsets.g99" in profile.unknown_keys
    assert profile.offsets == {}


def test_unknown_keys_do_not_prevent_loading() -> None:
    """A profile written for a newer version must still load."""
    profile = load_profile_text(MINIMAL + "\n[limits]\nmax_feed = 10.0\nfuture_thing = 1\n")
    assert profile.limits.max_feed == 10.0
    assert profile.unknown_keys == ("limits.future_thing",)


# --------------------------------------------------------------------------- malformed input


def test_invalid_toml_is_refused() -> None:
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_profile_text("[machine\nname = 'x'")


def test_invalid_toml_from_a_file_is_refused_too(tmp_path) -> None:
    """The path variant needs this more than the text variant, and for a while only the text one had it.

    `load_profile_text` wrapped `TOMLDecodeError` into `ProfileError`; `load_profile` did not. Since
    the path variant is what `--profile` reaches, a hand-edited profile with a typo surfaced as a raw
    tomllib traceback out of `foursight check` — which documents exit code 2 for an unusable profile.
    """
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not = valid toml [[[\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_profile(bad)


def test_the_toml_error_names_the_file(tmp_path) -> None:
    """With several profiles on disk, "invalid TOML" alone does not say which one to fix."""
    bad = tmp_path / "which-one.toml"
    bad.write_text("nope [[[\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="which-one.toml"):
        load_profile(bad)


def test_non_numeric_limit_is_refused() -> None:
    with pytest.raises(ProfileError, match="number"):
        load_profile_text(MINIMAL + "\n[limits]\nmax_feed = 'fast'\n")


def test_boolean_is_not_accepted_as_a_number() -> None:
    """`True` is an int in Python; accepting it would make max_feed = 1.0 mm/min."""
    with pytest.raises(ProfileError, match="number"):
        load_profile_text(MINIMAL + "\n[limits]\nmax_feed = true\n")


def test_axis_min_above_max_is_refused() -> None:
    with pytest.raises(ProfileError, match="exceeds max"):
        load_profile_text(MINIMAL + "\n[axes.x]\nmin = 10.0\nmax = 1.0\n")


def test_short_rotate_needs_a_wrapping_axis() -> None:
    """Short-rotating means stopping a full turn from the commanded angle.

    That is only the *same place* if the axis wraps, so the combination is refused rather than
    half-honoured — `process.rotary-rapid-short-rotates` would otherwise report gouges at angles a
    limited axis cannot reach.
    """
    with pytest.raises(ProfileError, match="requires wrap = true"):
        load_profile_text(MINIMAL + '\n[axes.a]\ntype = "rotary"\nshort_rotate = true\n')


def test_short_rotate_on_a_linear_axis_is_refused() -> None:
    """There is no equivalent position 360 mm away, so the setting cannot mean anything."""
    with pytest.raises(ProfileError, match="only to a rotary axis"):
        load_profile_text(MINIMAL + "\n[axes.x]\nshort_rotate = true\nwrap = true\n")


def test_short_rotate_loads_on_a_wrapping_rotary_axis() -> None:
    profile = load_profile_text(
        MINIMAL + '\n[axes.a]\ntype = "rotary"\nwrap = true\nshort_rotate = true\n'
    )
    assert profile.axes["A"].short_rotate is True
    assert profile.unknown_keys == (), (
        "short_rotate is a known key, not a typo the loader tolerates"
    )


def test_short_rotate_defaults_to_off() -> None:
    """A control that honours absolute angles is the assumption; the hazard is opted into."""
    profile = load_profile_text(MINIMAL + '\n[axes.a]\ntype = "rotary"\nwrap = true\n')
    assert profile.axes["A"].short_rotate is False


def test_bad_centerline_offset_is_refused() -> None:
    with pytest.raises(ProfileError, match="centerline_offset"):
        load_profile_text(MINIMAL + "\n[kinematics]\ncenterline_offset = [1.0, 2.0]\n")


def test_section_that_is_not_a_table_is_refused() -> None:
    """`limits` must come before any table header to be a top-level key rather than machine.limits."""
    with pytest.raises(ProfileError, match="must be a table"):
        load_profile_text("limits = 5\n\n[machine]\nname = 'x'\n")


def test_axis_that_is_not_a_table_is_refused() -> None:
    with pytest.raises(ProfileError, match="must be a table"):
        load_profile_text(MINIMAL + "\n[axes]\nx = 5\n")


def test_missing_profile_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_profile(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- shape


def test_profile_is_frozen() -> None:
    profile = load_profile_text(MINIMAL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.name = "other"  # type: ignore[misc]


def test_profile_defaults_are_not_shared_between_instances() -> None:
    """A mutable default would let one profile's axes leak into another."""
    first = MachineProfile()
    second = MachineProfile()
    first.axes["X"] = load_profile(DEFAULT_PROFILE).axes["X"]
    assert second.axes == {}


# --------------------------------------------------------------------------- [stock] (M7)

STOCK = """
[machine]
units = "%s"
[stock]
min = [0.0, 0.0, -20.0]
max = [100.0, 80.0, 0.0]
"""


def test_box_stock_bounds_are_loaded() -> None:
    stock = load_profile_text(STOCK % "mm").stock
    assert stock is not None
    assert stock.min == (0.0, 0.0, -20.0)
    assert stock.max == (100.0, 80.0, 0.0)


def test_stock_bounds_are_lengths_and_scale_on_an_inch_profile() -> None:
    stock = load_profile_text(STOCK % "inch").stock
    assert stock is not None
    assert stock.max == (100.0 * INCH_TO_MM, 80.0 * INCH_TO_MM, 0.0)
    assert stock.min == (0.0, 0.0, -20.0 * INCH_TO_MM)


@pytest.mark.parametrize("present", ["min", "max"])
def test_a_half_specified_stock_box_is_refused(present: str) -> None:
    """Neither bound is completed from the other.

    ±infinity would declare the whole machine envelope to be stock and zero would declare a box
    nothing can intersect. Either way the user believes they configured a check that is in fact
    reporting on a different solid.
    """
    text = f'[machine]\nunits = "mm"\n[stock]\n{present} = [0.0, 0.0, 0.0]\n'
    with pytest.raises(ProfileError, match="needs min, max"):
        load_profile_text(text)


def test_an_empty_stock_section_is_refused_rather_than_silently_doing_nothing() -> None:
    """Written deliberately, and configuring nothing — the same trap as a half-specified box."""
    with pytest.raises(ProfileError, match=r"\[stock\]"):
        load_profile_text('[machine]\nunits = "mm"\n[stock]\n')


def test_no_stock_section_at_all_is_simply_absent() -> None:
    assert load_profile_text(MINIMAL).stock is None


def test_stock_min_above_max_is_refused() -> None:
    text = '[machine]\nunits = "mm"\n[stock]\nmin = [0.0, 90.0, -20.0]\nmax = [100.0, 80.0, 0.0]\n'
    with pytest.raises(ProfileError, match="min y 90.0 exceeds max 80.0"):
        load_profile_text(text)


@pytest.mark.parametrize("value", ["[0.0, 0.0]", "[0.0, 0.0, 0.0, 0.0]", "5.0", '"x"'])
def test_a_stock_bound_that_is_not_three_numbers_is_refused(value: str) -> None:
    text = f'[machine]\nunits = "mm"\n[stock]\nmin = {value}\nmax = [1.0, 1.0, 1.0]\n'
    with pytest.raises(ProfileError, match="list of 3 numbers"):
        load_profile_text(text)


def test_an_unknown_stock_key_is_reported() -> None:
    text = '[machine]\nunits = "mm"\n[stock]\nmin = [0,0,0]\nmax = [1,1,1]\nmaterial = "6082"\n'
    assert "stock.material" in load_profile_text(text).unknown_keys


def test_a_degenerate_stock_box_is_allowed() -> None:
    """A zero-thickness plate is a real thing to clamp down, and equal bounds are not a mistake."""
    text = '[machine]\nunits = "mm"\n[stock]\nmin = [0.0, 0.0, 0.0]\nmax = [100.0, 80.0, 0.0]\n'
    stock = load_profile_text(text).stock
    assert stock is not None and stock.min[2] == stock.max[2] == 0.0


def test_max_plunge_feed_is_a_rate_and_scales_on_an_inch_profile() -> None:
    text = '[machine]\nunits = "inch"\n[limits]\nmax_plunge_feed = 12.0\n'
    assert load_profile_text(text).limits.max_plunge_feed == 12.0 * INCH_TO_MM


# --------------------------------------------------------------------- cylindrical stock (M9)

CYLINDER = """
[machine]
units = "%s"
[stock]
shape = "cylinder"
diameter = 50.0
length = 200.0
axis_min = 0.0
"""


def test_a_cylinder_is_loaded() -> None:
    from foursight.machine.profile import StockCylinder

    stock = load_profile_text(CYLINDER % "mm").stock
    assert isinstance(stock, StockCylinder)
    assert (stock.diameter, stock.length, stock.axis_min) == (50.0, 200.0, 0.0)
    assert stock.radius == 25.0
    assert stock.axis_max == 200.0


def test_every_cylinder_key_is_a_length_and_scales_on_an_inch_profile() -> None:
    """Diameter, length and the axial position are all lengths — none is an angle or a count."""
    stock = load_profile_text(CYLINDER % "inch").stock
    assert stock.diameter == 50.0 * INCH_TO_MM
    assert stock.length == 200.0 * INCH_TO_MM
    assert stock.radius == 25.0 * INCH_TO_MM


def test_a_cylinder_is_inferred_from_its_keys_without_a_shape() -> None:
    """A `[stock]` written before cylinders existed still means a box; one with a diameter does not."""
    from foursight.machine.profile import StockBox, StockCylinder

    text = '[machine]\nunits = "mm"\n[stock]\ndiameter = 50.0\nlength = 200.0\naxis_min = 0.0\n'
    assert isinstance(load_profile_text(text).stock, StockCylinder)
    assert isinstance(load_profile_text(STOCK % "mm").stock, StockBox)


@pytest.mark.parametrize("absent", ["diameter", "length", "axis_min"])
def test_a_half_specified_cylinder_is_refused(absent: str) -> None:
    text = "\n".join(
        line for line in (CYLINDER % "mm").splitlines() if not line.startswith(f"{absent} ")
    )
    with pytest.raises(ProfileError, match=f"{absent}.*absent|needs diameter"):
        load_profile_text(text)


def test_a_box_key_under_a_cylinder_is_refused_rather_than_ignored() -> None:
    """A key from the other shape looks configured and does nothing, which is the worst outcome."""
    text = (CYLINDER % "mm") + "min = [0.0, 0.0, 0.0]\n"
    with pytest.raises(ProfileError, match="which cylinder stock does not use"):
        load_profile_text(text)


def test_a_cylinder_key_under_a_box_is_refused() -> None:
    text = (STOCK % "mm") + 'shape = "box"\ndiameter = 50.0\n'
    with pytest.raises(ProfileError, match="which box stock does not use"):
        load_profile_text(text)


def test_a_declared_shape_is_checked_against_the_keys_not_trusted() -> None:
    """The two disagreeing is how a cylinder gets read as a box."""
    text = (STOCK % "mm") + 'shape = "cylinder"\n'
    with pytest.raises(ProfileError):
        load_profile_text(text)


def test_an_unknown_shape_is_refused() -> None:
    text = '[machine]\nunits = "mm"\n[stock]\nshape = "sphere"\n'
    with pytest.raises(ProfileError, match="shape must be one of"):
        load_profile_text(text)


@pytest.mark.parametrize("key", ["diameter", "length"])
@pytest.mark.parametrize("value", ["0.0", "-10.0"])
def test_a_cylinder_with_no_volume_is_refused(key: str, value: str) -> None:
    """It describes no solid, and a check against nothing passes every program while looking set."""
    text = (
        (CYLINDER % "mm")
        .replace(f"{key} = 50.0", f"{key} = {value}")
        .replace(f"{key} = 200.0", f"{key} = {value}")
    )
    with pytest.raises(ProfileError, match="must be greater than zero"):
        load_profile_text(text)


def test_a_negative_axis_min_is_fine() -> None:
    """Unlike the diameter, the axial position is a coordinate and may sit either side of zero."""
    text = (CYLINDER % "mm").replace("axis_min = 0.0", "axis_min = -100.0")
    stock = load_profile_text(text).stock
    assert stock.axis_min == -100.0 and stock.axis_max == 100.0
