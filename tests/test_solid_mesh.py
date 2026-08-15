"""Triangulating a carved field. No Qt and no GL context, exactly as `test_batching.py` needs neither.

What can go wrong here is geometric rather than numeric: a face indexing a vertex that does not exist, a
seam left open down a cylinder, a normal pointing into the solid so the shader lights it from inside. All
three render as something plausible-looking, so all three are asserted directly.
"""

import numpy as np
import pytest

from foursight.gui.legend import SOLID_COLOR
from foursight.gui.solid_mesh import build_mesh
from foursight.machine.kinematics import apply_display_transform
from foursight.machine.profile import (
    Kinematics,
    MachineProfile,
    StockBox,
    StockCylinder,
    Tool,
)
from foursight.sim.segments import Kind, SegmentBuilder
from foursight.sim.solid import carve

BLANK = StockBox(min=(0.0, 0.0, -20.0), max=(100.0, 80.0, 0.0))
CYLINDER = StockCylinder(diameter=52.0, length=150.0, axis_min=0.0)


def box_field(target_cells: int = 48):
    profile = MachineProfile(stock=BLANK, tool=Tool(diameter=6.0))
    builder = SegmentBuilder()
    builder.add_segment(np.array([20.0, 40.0, -2.0]), np.array([80.0, 40.0, -2.0]), Kind.FEED, 1)
    return carve(builder.finalize(), profile, target_cells=target_cells)


def cylinder_field(target_cells: int = 48):
    profile = MachineProfile(
        stock=CYLINDER,
        tool=Tool(diameter=6.0),
        kinematics=Kinematics(
            rotary_mount="table", rotary_axis="y", centerline_offset=(0.0, 0.0, -26.0)
        ),
    )
    builder = SegmentBuilder()
    point = np.array([0.0, 75.0, -2.0])
    for step in range(72):
        builder.add_segment(
            point, point.copy(), Kind.FEED, 1, rot_start=step * 5.0, rot_end=(step + 1) * 5.0
        )
    store = builder.finalize()
    apply_display_transform(store, profile.kinematics)
    return carve(store, profile, target_cells=target_cells)


# ------------------------------------------------------------------------------- basic wellformedness


@pytest.mark.parametrize("field_of", [box_field, cylinder_field])
def test_every_face_indexes_a_vertex_that_exists(field_of):
    """An out-of-range index is undefined behaviour in GL, not an error — it draws garbage."""
    mesh = build_mesh(field_of())
    assert mesh.faces.min() >= 0
    assert mesh.faces.max() < mesh.vertices.shape[0]


@pytest.mark.parametrize("field_of", [box_field, cylinder_field])
def test_vertices_and_normals_agree_in_length(field_of):
    mesh = build_mesh(field_of())
    assert mesh.normals.shape == mesh.vertices.shape


@pytest.mark.parametrize("field_of", [box_field, cylinder_field])
def test_geometry_is_float32_and_finite(field_of):
    """A NaN normal renders as an unlit black band, and float64 doubles the upload for nothing."""
    mesh = build_mesh(field_of())
    assert mesh.vertices.dtype == np.float32
    assert mesh.normals.dtype == np.float32
    assert np.all(np.isfinite(mesh.vertices))
    assert np.all(np.isfinite(mesh.normals))


@pytest.mark.parametrize("field_of", [box_field, cylinder_field])
def test_no_face_is_degenerate(field_of):
    """A triangle with a repeated vertex has no normal and contributes nothing but index bandwidth."""
    mesh = build_mesh(field_of())
    a, b, c = mesh.faces[:, 0], mesh.faces[:, 1], mesh.faces[:, 2]
    assert np.all((a != b) & (b != c) & (a != c))


def test_a_degenerate_field_produces_no_mesh_rather_than_an_empty_one():
    field = box_field()
    single_row = type(field)(
        kind="box",
        height=field.height[:1, :1],
        origin=field.origin,
        cell=field.cell,
        floor=field.floor,
        ceiling=field.ceiling,
    )
    assert build_mesh(single_row) is None


# ------------------------------------------------------------------------------------ orientation


def test_box_surface_normals_point_upward():
    """A Z map is machined from above. A downward normal is lit from inside and reads as a hole."""
    field = box_field()
    mesh = build_mesh(field)
    nu, nv = field.height.shape
    surface = mesh.normals[: nu * nv]
    assert np.all(surface[:, 2] > 0.0)


def test_cylinder_surface_normals_point_away_from_the_rotary_axis():
    field = cylinder_field()
    mesh = build_mesh(field)
    nu, nv = field.height.shape
    surface_points = mesh.vertices[: nu * nv]
    surface_normals = mesh.normals[: nu * nv]

    # rotary_axis = "y", centreline at (0, 0, -26): the radial pair is Z and X.
    outward = np.stack([surface_points[:, 0] - 0.0, surface_points[:, 2] - (-26.0)], axis=1)
    projected = surface_normals[:, 0] * outward[:, 0] + surface_normals[:, 2] * outward[:, 1]
    assert np.all(projected > 0.0)


def test_every_normal_is_a_unit_vector():
    mesh = build_mesh(box_field())
    lengths = np.linalg.norm(mesh.normals, axis=1)
    assert np.allclose(lengths, 1.0, atol=1e-5)


# --------------------------------------------------------------------------------- closing the solid


def test_the_box_mesh_reaches_the_underside_of_the_blank():
    """Without walls and a floor the carve is a sheet, and a sheet does not read as a part."""
    field = box_field()
    mesh = build_mesh(field)
    assert mesh.vertices[:, 2].min() == pytest.approx(field.floor, abs=1e-5)


def test_the_cylinder_mesh_is_capped_at_both_ends():
    field = cylinder_field()
    mesh = build_mesh(field)
    # A cap's centre vertex sits exactly on the rotary axis; nothing on the carved surface does.
    on_axis = np.isclose(mesh.vertices[:, 0], 0.0, atol=1e-4) & np.isclose(
        mesh.vertices[:, 2], -26.0, atol=1e-4
    )
    assert np.count_nonzero(on_axis) == 2


def test_the_cylinder_surface_has_no_seam():
    """The angular axis closes on itself; an open seam is a hairline crack down the whole part."""
    field = cylinder_field()
    nu, nv = field.height.shape
    mesh = build_mesh(field)

    surface_faces = mesh.faces[mesh.faces.max(axis=1) < nu * nv]
    rows = surface_faces // nv
    # A quad joining the last column of the grid to the first is the seam, and it must exist.
    joins_seam = (rows.max(axis=1) == nu - 1) & (rows.min(axis=1) == 0)
    assert np.count_nonzero(joins_seam) > 0


def test_a_box_surface_has_no_wrap_faces():
    """The X axis of a box does not close on itself; joining its ends would fold the part in half."""
    field = box_field()
    nu, nv = field.height.shape
    mesh = build_mesh(field)

    surface_faces = mesh.faces[mesh.faces.max(axis=1) < nu * nv]
    rows = surface_faces // nv
    assert np.all(rows.max(axis=1) - rows.min(axis=1) <= 1)


# ------------------------------------------------------------------------------------ size budget


def test_the_mesh_reports_what_it_costs_in_gl_memory():
    mesh = build_mesh(box_field())
    assert mesh.nbytes == sum(
        array.nbytes for array in (mesh.vertices, mesh.faces, mesh.normals, mesh.colors)
    )
    assert mesh.triangles == mesh.faces.shape[0]


# ---------------------------------------------------------------------------------- the lighting


def test_the_machined_face_is_the_brightest_thing_on_the_solid():
    """The bug this exists for: pyqtgraph's `shaded` shader lights from **eye** space, so the face
    turned toward the camera lands at ambient — and that face is the machined surface, the one thing
    the whole view exists to show. It rendered as the darkest part of the picture.

    Perfectly formed geometry shades this way too, so no other test here can see it.
    """
    from foursight.gui.solid_mesh import shade

    up = shade(np.array([[0.0, 0.0, 1.0]]))
    down = shade(np.array([[0.0, 0.0, -1.0]]))
    assert up[0, :3].max() > down[0, :3].max()


def test_a_face_turned_fully_away_lands_on_ambient_and_no_lower():
    """`AMBIENT` is the floor of the wrap-around term, so an unlit face still reads as a surface."""
    from foursight.gui.solid_mesh import AMBIENT, LIGHT_DIRECTION, shade

    light = np.asarray(LIGHT_DIRECTION)
    away = shade(-light[None, :] / np.linalg.norm(light))
    assert away[0, 0] == pytest.approx(SOLID_COLOR[0] * AMBIENT, abs=1e-6)
    assert np.all(away[0, :3] > 0.0)


def test_shading_never_leaves_the_zero_to_one_range():
    """A channel over 1.0 clips to white and flattens the relief it was meant to show."""
    field = box_field()
    mesh = build_mesh(field)
    assert mesh.colors.min() >= 0.0
    assert mesh.colors.max() <= 1.0


def test_the_solid_is_opaque():
    """Alpha below one would blend against the toolpath and make both harder to read."""
    mesh = build_mesh(box_field())
    assert np.all(mesh.colors[:, 3] == 1.0)


def test_pocket_walls_facing_different_ways_are_shaded_differently():
    """A purely vertical light would leave a pocket floor and the untouched top face identical, and
    the recess would then be visible only by its silhouette."""
    from foursight.gui.solid_mesh import shade

    walls = shade(np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]]))
    assert len(set(walls[:, 0].tolist())) == 3
