"""``SegmentStore``: columnar segment storage — parallel numpy arrays.

Never per-object segment dataclasses. 500k of those cost over 200 MB before the GL buffers and then
have to be repacked into contiguous arrays anyway; the columnar form is ~38 MB and
``lin.reshape(-1, 3)`` is already the zero-copy layout ``GLLinePlotItem(mode='lines')`` wants. A
``Segment`` view class may exist for test readability, but must never be the storage.

Invariants this module exists to hold:

- **``rot`` is its own column**, kept out of the position vector, so no norm is ever taken across
  millimetres and degrees. There is deliberately no accessor returning a 4-vector: the way to
  combine them is ``sim/timing.py``'s ``max(linear_time, rotary_time)``, not a hypotenuse.
- **``lin`` is always machine coordinates.** Display transforms write ``lin_part`` and never touch
  ``lin``; mutating it in place would silently destroy the ability to verify travel limits.
- **``kind`` is motion type only** — RAPID or FEED. After interpolation everything is a line
  segment, and an arc is always a cutting move, so geometry has no business in this column. The
  originating motion code is recoverable via ``line[i]``.
- **Every segment traces back to a source line** via ``line[i]``. Editor sync, diagnostics and fixes
  all depend on it, so ``finalize`` refuses a store containing a zero.

Segment count is not known in advance — it is driven by arc and rotary tessellation, so one
``G1 X100 A360`` block can become a thousand segments. ``SegmentBuilder`` therefore grows in chunks
and concatenates once, rather than reallocating per segment.
"""

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

# Chunk size for the builder: 65,536 segments is ~3 MB of `lin`, big enough that the per-chunk
# bookkeeping disappears and small enough that a tiny program does not allocate megabytes.
DEFAULT_CHUNK = 65_536


class Kind(IntEnum):
    """Motion type, stored as ``uint8``.

    ``IntEnum`` so ``store.kind == Kind.RAPID`` works directly against the numpy column.
    """

    RAPID = 0
    FEED = 1


@dataclass(slots=True)
class SegmentStore:
    """Interpolated toolpath as parallel columns. ``N`` is the segment count."""

    lin: np.ndarray  # (N, 2, 3) float64 — XYZ mm, machine coords, [seg, start|end, axis]
    rot: np.ndarray  # (N, 2)    float64 — A degrees, kept OUT of the position vector
    kind: np.ndarray  # (N,)      uint8   — Kind.RAPID | Kind.FEED
    line: np.ndarray  # (N,)      int32   — 1-based source line, for editor sync
    duration: np.ndarray  # (N,)  float64 — seconds, for the timeline
    lin_part: np.ndarray | None = None  # (N, 2, 3) display coords when rotary_mount = "table"

    def __len__(self) -> int:
        return int(self.lin.shape[0])

    @property
    def vertices(self) -> np.ndarray:
        """``(2N, 3)`` view for ``GLLinePlotItem(mode='lines')`` — **zero copy**.

        A C-contiguous ``(N, 2, 3)`` array reshapes to ``(2N, 3)`` without copying, which is the
        whole reason the store is shaped this way. Converting to float32 for upload is the
        renderer's job (T2.6); doing it here would double the resident cost.
        """
        return self.lin.reshape(-1, 3)

    @property
    def part_vertices(self) -> np.ndarray | None:
        """The same view over display coordinates, or None when no transform has been applied."""
        return None if self.lin_part is None else self.lin_part.reshape(-1, 3)

    def set_part_coordinates(self, lin_part: np.ndarray) -> None:
        """Attach display coordinates. Validates shape; never touches ``lin``.

        The rotary transform depends on A at every interpolation step, so it is nonlinear along the
        path and cannot be expressed as a view matrix — it has to be baked into vertex positions,
        which is why this is a second array rather than a camera transform.
        """
        if lin_part.shape != self.lin.shape:
            raise ValueError(f"lin_part shape {lin_part.shape} does not match lin {self.lin.shape}")
        if lin_part.dtype != np.float64:
            raise ValueError(f"lin_part must be float64, got {lin_part.dtype}")
        if lin_part is self.lin:
            raise ValueError("lin_part must be a separate array: lin stays in machine coordinates")
        self.lin_part = lin_part

    def nbytes(self) -> int:
        """Resident bytes of the columns, for the memory budget."""
        total = sum(
            array.nbytes for array in (self.lin, self.rot, self.kind, self.line, self.duration)
        )
        return total + (0 if self.lin_part is None else self.lin_part.nbytes)

    def mask(self, kind: Kind) -> np.ndarray:
        """Boolean mask selecting one motion type, for grouping into GL batches."""
        return self.kind == kind

    @classmethod
    def empty(cls) -> "SegmentStore":
        """A store with no segments, so callers need not special-case an empty program."""
        return cls(
            lin=np.empty((0, 2, 3), dtype=np.float64),
            rot=np.empty((0, 2), dtype=np.float64),
            kind=np.empty(0, dtype=np.uint8),
            line=np.empty(0, dtype=np.int32),
            duration=np.empty(0, dtype=np.float64),
        )

    def validate(self) -> None:
        """Assert every structural invariant. Cheap enough to call from tests and golden checks."""
        count = len(self)
        expected = {
            "lin": ((count, 2, 3), np.float64),
            "rot": ((count, 2), np.float64),
            "kind": ((count,), np.uint8),
            "line": ((count,), np.int32),
            "duration": ((count,), np.float64),
        }
        for name, (shape, dtype) in expected.items():
            array = getattr(self, name)
            if array.shape != shape:
                raise ValueError(f"{name} shape {array.shape}, expected {shape}")
            if array.dtype != dtype:
                raise ValueError(f"{name} dtype {array.dtype}, expected {dtype}")
        if not self.lin.flags["C_CONTIGUOUS"]:
            raise ValueError("lin must be C-contiguous or the GL upload cannot be zero-copy")
        if count and int(self.line.min()) < 1:
            raise ValueError("every segment must trace back to a 1-based source line")
        if self.lin_part is not None and self.lin_part.shape != self.lin.shape:
            raise ValueError("lin_part shape does not match lin")


class SegmentBuilder:
    """Accumulates segments in chunks, then concatenates once.

    Growth is chunked rather than a doubling realloc so the final arrays are *exactly* sized: a
    doubling scheme would leave up to 2× the needed capacity resident, which the 250 MB budget
    cannot spare. The single concatenate at ``finalize`` is the price.

    The vectorized ``add_polyline`` is the path interpolation should use. Appending one segment at a
    time through Python for 500k segments would cost more than the interpolation itself.
    """

    __slots__ = ("_chunk_size", "_chunks", "_count", "_current", "_used")

    def __init__(self, chunk_size: int = DEFAULT_CHUNK) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self._chunk_size = chunk_size
        self._chunks: list[dict[str, np.ndarray]] = []
        self._current: dict[str, np.ndarray] | None = None
        self._used = 0
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def add_polyline(
        self,
        points: np.ndarray,
        kind: Kind,
        line: int,
        rotations: np.ndarray | None = None,
        durations: np.ndarray | None = None,
    ) -> int:
        """Append ``len(points) - 1`` segments joining consecutive points.

        ``points`` is ``(M, 3)`` XYZ in mm, machine coordinates. ``rotations`` is ``(M,)`` A in
        degrees — a *separate* argument, not a fourth column of ``points``, so the two can never be
        accidentally combined. Returns the number of segments added.
        """
        points = np.asarray(points, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError(f"points must be (M, 3), got {points.shape}")
        added = points.shape[0] - 1
        if added <= 0:
            return 0
        if line < 1:
            raise ValueError("line must be a 1-based source line number")

        rot_values = self._rotations(rotations, points.shape[0])
        if durations is not None:
            durations = np.asarray(durations, dtype=np.float64)
            if durations.shape != (added,):
                raise ValueError(
                    f"durations must be one per segment, ({added},), got {durations.shape}"
                )
        written = 0
        while written < added:
            room = self._ensure_room()
            take = min(room, added - written)
            start = written
            stop = written + take
            # _ensure_room has just guaranteed a current chunk with space in it.
            block = self._current or self._allocate()
            at = self._used
            block["lin"][at : at + take, 0, :] = points[start:stop]
            block["lin"][at : at + take, 1, :] = points[start + 1 : stop + 1]
            block["rot"][at : at + take, 0] = rot_values[start:stop]
            block["rot"][at : at + take, 1] = rot_values[start + 1 : stop + 1]
            block["kind"][at : at + take] = int(kind)
            block["line"][at : at + take] = line
            if durations is None:
                block["duration"][at : at + take] = 0.0
            else:
                block["duration"][at : at + take] = durations[start:stop]
            self._used += take
            self._count += take
            written += take
        return added

    def add_segment(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        kind: Kind,
        line: int,
        rot_start: float = 0.0,
        rot_end: float = 0.0,
        duration: float = 0.0,
    ) -> None:
        """Append one segment. Convenience for tests and single-move blocks."""
        points = np.array([start, end], dtype=np.float64)
        rotations = np.array([rot_start, rot_end], dtype=np.float64)
        durations = np.array([duration], dtype=np.float64)
        self.add_polyline(points, kind, line, rotations=rotations, durations=durations)

    def finalize(self) -> SegmentStore:
        """Concatenate the chunks into exactly-sized columns and check the invariants."""
        if self._count == 0:
            return SegmentStore.empty()
        blocks = [*self._chunks, self._current] if self._current is not None else self._chunks
        limits = [self._chunk_size] * (len(blocks) - 1) + [self._used]
        store = SegmentStore(
            lin=np.concatenate([b["lin"][:n] for b, n in zip(blocks, limits, strict=True)]),
            rot=np.concatenate([b["rot"][:n] for b, n in zip(blocks, limits, strict=True)]),
            kind=np.concatenate([b["kind"][:n] for b, n in zip(blocks, limits, strict=True)]),
            line=np.concatenate([b["line"][:n] for b, n in zip(blocks, limits, strict=True)]),
            duration=np.concatenate(
                [b["duration"][:n] for b, n in zip(blocks, limits, strict=True)]
            ),
        )
        store.validate()
        return store

    def _rotations(self, rotations: np.ndarray | None, expected: int) -> np.ndarray:
        if rotations is None:
            return np.zeros(expected, dtype=np.float64)
        values = np.asarray(rotations, dtype=np.float64)
        if values.shape != (expected,):
            raise ValueError(f"rotations must be ({expected},), got {values.shape}")
        return values

    def _ensure_room(self) -> int:
        """Room left in the current chunk, allocating a fresh one when full."""
        if self._current is None or self._used >= self._chunk_size:
            if self._current is not None:
                self._chunks.append(self._current)
            self._current = self._allocate()
            self._used = 0
        return self._chunk_size - self._used

    def _allocate(self) -> dict[str, np.ndarray]:
        size = self._chunk_size
        return {
            "lin": np.empty((size, 2, 3), dtype=np.float64),
            "rot": np.empty((size, 2), dtype=np.float64),
            "kind": np.empty(size, dtype=np.uint8),
            "line": np.empty(size, dtype=np.int32),
            "duration": np.empty(size, dtype=np.float64),
        }
