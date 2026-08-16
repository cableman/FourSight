"""``SolidField`` → GL-ready triangles. **No Qt, deliberately**, exactly as ``batching.py`` is not.

The part that can be *wrong* — which vertices exist, which way the normals point, whether the solid is
closed — is unit-testable here without a display or a GL context, leaving `viewport3d` thin enough to
check by eye. Same split, same reason.

Three things this module exists to get right:

- **A heightfield is a sheet, and a sheet does not read as a part.** Seen from below or edge-on, a bare
  carved surface is a skin floating in space with nothing behind it, and the eye reads that as a
  rendering failure rather than as a machined face. So the mesh is *closed*: a box grows four walls down
  to the blank's underside plus a floor, and a cylinder grows an end cap at each end. That is the whole
  difference between "a surface" and "the end result".
- **Normals are computed, not inferred from winding**, and every one is explicitly oriented *outward*.
  `MeshData.vertexNormals()` would recompute them from the faces in Python at 500k triangles; central
  differences over the position grid give the same answer for a fraction of the cost. A normal pointing
  into the solid is lit from inside and looks like a hole.
- **The lighting is baked into vertex colours here, not left to pyqtgraph's `shaded` shader.** That
  shader lights from `normalize(vec3(1, -1, -1))` in **eye** space, so a face turned toward the camera
  gets `dot < 0`, is clamped to zero, and renders at ambient only. The machined surface is precisely the
  face pointing at the viewer, so it came out as the darkest thing on screen — the one part of the
  picture the whole feature exists to show. `LIGHT_DIRECTION` is in **world** space instead, from above
  and slightly toward the default camera, so the cut face is the brightest and the lit side stays put
  while you orbit, the way a real object does.
- **The wrap has no seam.** A cylinder's angular axis closes on itself, so the last column of quads joins
  column zero. Leaving it open puts a hairline crack down the length of the part at exactly the place a
  user will assume is a modelling artefact of their program.
"""

from dataclasses import dataclass

import numpy as np

from foursight.gui.legend import SOLID_COLOR
from foursight.sim.solid import SolidField

#: GL wants float32; the field is float64 for the same reason `SegmentStore` is. Converted once, here.
GL_DTYPE = np.float32
#: Face indices. uint32 rather than the default int64 halves the index buffer, and a heightfield grid
#: cannot approach 2**32 vertices.
INDEX_DTYPE = np.uint32

#: Where the light is, in **world** coordinates — see the module docstring for why not the shader's.
#: Mostly from above, so the machined face reads brightest, and leaning toward the default camera
#: (azimuth -60, elevation 30) so the two visible walls of a pocket are lit differently and the relief
#: shows. A purely vertical light would leave a pocket floor and the untouched top face the same shade.
LIGHT_DIRECTION = (0.40, -0.62, 0.67)
#: What a face turned *fully* away from the light gets. Not zero: an unlit face has to stay legible as a
#: surface rather than collapsing into the background, and a pocket you can only find by its silhouette
#: is not what this view is for.
AMBIENT = 0.22
DIFFUSE = 0.78


@dataclass(frozen=True, slots=True)
class SolidMesh:
    """One closed, pre-lit solid, ready for ``GLMeshItem(shader=None)``."""

    vertices: np.ndarray  # (V, 3) float32
    faces: np.ndarray  # (F, 3) uint32
    normals: np.ndarray  # (V, 3) float32
    colors: np.ndarray  # (V, 4) float32 — the base colour with the diffuse term already applied

    @property
    def triangles(self) -> int:
        return int(self.faces.shape[0])

    @property
    def nbytes(self) -> int:
        """What this mesh costs in GL-bound memory, alongside the toolpath's own budget."""
        return int(
            self.vertices.nbytes + self.faces.nbytes + self.normals.nbytes + self.colors.nbytes
        )


def shade(normals: np.ndarray, base=SOLID_COLOR) -> np.ndarray:
    """``(V, 4)`` vertex colours for ``normals`` under `LIGHT_DIRECTION`.

    Separate and public because it is the whole of the appearance, and getting it wrong is invisible to
    every other test here — the mesh can be perfectly formed and still render as a flat silhouette.

    **Wrap-around rather than clamped Lambert.** Textbook `max(0, n·L)` sends every face turned away from
    the light to the same value, so a pocket's two shaded walls come out identical and the recess reads
    only as an outline. Mapping ``n·L`` from ``[-1, 1]`` onto ``[AMBIENT, 1]`` instead gives every
    orientation its own shade, which is what makes machined relief legible. It is not physical, and it is
    not trying to be — this is a drawing of a part, not a render of one.
    """
    light = np.asarray(LIGHT_DIRECTION, dtype=np.float64)
    light = light / np.linalg.norm(light)
    intensity = AMBIENT + DIFFUSE * (0.5 * (normals @ light) + 0.5)
    colors = np.empty((normals.shape[0], 4), dtype=np.float64)
    colors[:, :3] = np.asarray(base[:3], dtype=np.float64) * intensity[:, None]
    colors[:, 3] = base[3]
    return colors.astype(GL_DTYPE, copy=False)


def build_mesh(field: SolidField) -> SolidMesh | None:
    """Triangulate ``field`` into a closed solid, or None when there is nothing to draw."""
    nu, nv = field.height.shape
    if nu < 2 or nv < 2:
        return None

    points = field.points()
    surface_normals = _surface_normals(points, field)

    vertices = [points.reshape(-1, 3)]
    normals = [surface_normals.reshape(-1, 3)]
    faces = [_grid_faces(nu, nv, wrap=field.wraps_u)]
    offset = nu * nv

    for part in _closing_parts(points, field):
        part_vertices, part_normals, part_faces = part
        vertices.append(part_vertices)
        normals.append(part_normals)
        faces.append(part_faces + offset)
        offset += part_vertices.shape[0]

    all_normals = np.concatenate(normals)
    return SolidMesh(
        vertices=np.concatenate(vertices).astype(GL_DTYPE, copy=False),
        faces=np.concatenate(faces).astype(INDEX_DTYPE, copy=False),
        normals=all_normals.astype(GL_DTYPE, copy=False),
        colors=shade(all_normals),
    )


# ------------------------------------------------------------------------------------ the surface


def _grid_faces(nu: int, nv: int, *, wrap: bool) -> np.ndarray:
    """Two triangles per quad over an ``(nu, nv)`` vertex grid.

    ``wrap`` closes the ``u`` axis onto itself, which is what removes the seam down a cylinder.
    """
    rows = nu if wrap else nu - 1
    i0 = np.arange(rows, dtype=np.int64)[:, None]
    i1 = (i0 + 1) % nu if wrap else i0 + 1
    j0 = np.arange(nv - 1, dtype=np.int64)[None, :]

    a = (i0 * nv + j0).ravel()
    b = (i1 * nv + j0).ravel()
    c = (i1 * nv + j0 + 1).ravel()
    d = (i0 * nv + j0 + 1).ravel()
    return np.concatenate([np.stack([a, b, c], axis=1), np.stack([a, c, d], axis=1)])


def _surface_normals(points: np.ndarray, field: SolidField) -> np.ndarray:
    """Outward unit normals at every grid vertex, from central differences of the positions.

    Taking the differences of the **positions** rather than of the heights is what lets one code path
    serve both frames: a cylinder's surface curves in a way its radius column alone does not describe,
    and a normal derived from the radius gradient would be wrong everywhere except at the top.
    """
    du = _difference(points, axis=0, wrap=field.wraps_u)
    dv = _difference(points, axis=1, wrap=False)
    normals = np.cross(du, dv)
    _normalize(normals)

    # Orient outward. A normal pointing into the solid is lit from inside and reads as a hole, and the
    # cross product's sign depends on the grid's handedness, which differs between the two frames.
    outward = _outward_reference(points, field)
    flip = np.sum(normals * outward, axis=-1) < 0.0
    normals[flip] *= -1.0
    return normals


def _difference(points: np.ndarray, *, axis: int, wrap: bool) -> np.ndarray:
    """Central differences along ``axis``.

    ``np.gradient`` is already central in the interior and one-sided at the ends, which is what an open
    edge needs — a zero tangent there would give a zero normal, and a zero normal normalizes to nothing
    and renders as an unlit black band along the rim.
    """
    if wrap:
        return (np.roll(points, -1, axis=axis) - np.roll(points, 1, axis=axis)) * 0.5
    return np.gradient(points, axis=axis)


def _outward_reference(points: np.ndarray, field: SolidField) -> np.ndarray:
    """A direction known to point out of the solid at each vertex."""
    if field.kind == "box":
        reference = np.zeros_like(points)
        reference[..., 2] = 1.0  # a Z map is machined from above, so "out" is up
        return reference
    reference = np.zeros_like(points)
    first, second = field.radial
    reference[..., first] = points[..., first] - field.centerline[first]
    reference[..., second] = points[..., second] - field.centerline[second]
    return reference


def _normalize(vectors: np.ndarray) -> None:
    """Unit-length in place, leaving degenerate vectors as zero rather than NaN."""
    lengths = np.linalg.norm(vectors, axis=-1, keepdims=True)
    np.divide(vectors, lengths, out=vectors, where=lengths > 0.0)


# ------------------------------------------------------------------- closing the solid underneath


def _closing_parts(points: np.ndarray, field: SolidField):
    """The walls and caps that turn the carved sheet into a solid."""
    if field.kind == "box":
        yield from _box_skirt(points, field)
    else:
        yield from _cylinder_caps(points, field)


def _box_skirt(points: np.ndarray, field: SolidField):
    """Four walls dropping to the blank's underside, plus the floor they stand on."""
    nu, nv = field.height.shape
    floor = field.floor

    edges = (
        (points[0, :, :], np.array([-1.0, 0.0, 0.0])),
        (points[-1, :, :], np.array([1.0, 0.0, 0.0])),
        (points[:, 0, :], np.array([0.0, -1.0, 0.0])),
        (points[:, -1, :], np.array([0.0, 1.0, 0.0])),
    )
    for rim, outward in edges:
        yield _wall(rim, floor, outward)

    corners = np.array(
        [
            [points[0, 0, 0], points[0, 0, 1], floor],
            [points[-1, 0, 0], points[-1, 0, 1], floor],
            [points[-1, -1, 0], points[-1, -1, 1], floor],
            [points[0, -1, 0], points[0, -1, 1], floor],
        ]
    )
    normals = np.tile(np.array([0.0, 0.0, -1.0]), (4, 1))
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    yield corners, normals, faces


def _wall(rim: np.ndarray, floor: float, outward: np.ndarray):
    """One quad strip from a boundary row of the surface down to ``floor``."""
    count = rim.shape[0]
    bottom = rim.copy()
    bottom[:, 2] = floor
    vertices = np.concatenate([rim, bottom])
    normals = np.tile(outward, (vertices.shape[0], 1))

    top = np.arange(count - 1, dtype=np.int64)
    faces = np.concatenate(
        [
            np.stack([top, top + 1, top + count + 1], axis=1),
            np.stack([top, top + count + 1, top + count], axis=1),
        ]
    )
    return vertices, normals, faces


def _cylinder_caps(points: np.ndarray, field: SolidField):
    """A triangle fan closing each end of the blank onto the rotary axis."""
    axial = field.axial
    for index, direction in ((0, -1.0), (-1, 1.0)):
        rim = points[:, index, :]
        centre = np.zeros(3)
        centre[axial] = rim[0, axial]
        for column in field.radial:
            centre[column] = field.centerline[column]

        vertices = np.concatenate([centre[None, :], rim])
        normals = np.zeros_like(vertices)
        normals[:, axial] = direction

        # The rim wraps, so the fan closes back onto its first vertex.
        spoke = np.arange(rim.shape[0], dtype=np.int64)
        faces = np.stack([np.zeros_like(spoke), spoke + 1, (spoke + 1) % rim.shape[0] + 1], axis=1)
        yield vertices, normals, faces
