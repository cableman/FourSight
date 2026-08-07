"""The fix engine and the one-fix contract.

PLAN.md § Fix Engine: **apply exactly one fix → re-parse the whole buffer → re-verify → rebuild
segments.** No batch application, no diff rebasing.

That is not conservatism, it is arithmetic. A fix that inserts or removes a line shifts every line number
after it, so every `Diagnostic.line` and every `SegmentStore.line` entry is stale the instant it applies.
A second fix aimed at "line 42" would land somewhere else — and often somewhere that still looks
plausible, which is the worst case. `apply_fix` therefore returns new *text* and nothing else; the caller
re-runs the pipeline, and `differ.apply_unified_diff` refuses on a context mismatch as a backstop.

**Refusal is a first-class result, not an error.** Several fixes must decline: arc-centre recomputation
beyond 10× tolerance, IJK→R on a full circle. A refusal carries a reason and is shown to the user, because
"we could fix this but the intent is ambiguous" is genuinely useful information — and a fix that guessed
instead would invent geometry, which is the one thing this tool exists not to do.

**Fixes never write the file.** They return text for the editor buffer; the user saves explicitly.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from foursight.fix.differ import unified_diff
from foursight.machine.profile import MachineProfile


@dataclass(frozen=True, slots=True)
class FixContext:
    """What a fix is allowed to look at.

    The profile is here because tolerances belong to the machine, not the fix: arc-centre recomputation
    shares `tolerance.arc_radius_mismatch` with the check that reported the problem (T1.9), so the two can
    never disagree about what counts as a mismatch.

    ``parameter`` carries a value the user supplied when prompted — the feed rate for
    `fix.inject-feed-rate`, for instance. PLAN.md requires those to be *prompted*: inventing a feed rate
    would put a number in the program that nobody chose.
    """

    text: str
    profile: MachineProfile
    line: int | None = None  # the diagnostic's line, when the fix is aimed at one
    parameter: float | str | None = None
    block_delete: bool = False


@dataclass(frozen=True, slots=True)
class FixResult:
    """What a fix did, or why it declined.

    ``text`` is None on refusal. A caller must check `applied` rather than truthiness of the diff, because
    a fix that legitimately changes nothing and a fix that refused are different outcomes.
    """

    fix_id: str
    text: str | None
    diff: str
    refusal: str | None = None
    note: str | None = None

    @property
    def applied(self) -> bool:
        return self.text is not None and self.diff != ""

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    @property
    def changed_nothing(self) -> bool:
        """Applied cleanly but produced no change — worth distinguishing from both of the above."""
        return self.text is not None and self.diff == ""


@dataclass(frozen=True, slots=True)
class Fix:
    """One registered transform.

    ``needs_parameter`` drives the prompt: the GUI has to know *before* running the fix that it must ask
    for a value, and a fix that returned "I needed a number" only after being invoked would make that
    impossible to present sensibly.
    """

    fix_id: str
    title: str
    description: str
    transform: Callable[[FixContext], FixResult]
    needs_parameter: bool = False
    parameter_prompt: str = ""
    destructive: bool = False  # off by default in any UI; see fixes.strip_line_numbers


_REGISTRY: dict[str, Fix] = {}


def register_fix(fix: Fix) -> Fix:
    if fix.fix_id in _REGISTRY:
        raise ValueError(f"duplicate fix id {fix.fix_id!r}")
    _REGISTRY[fix.fix_id] = fix
    return fix


def registered_fixes() -> dict[str, Fix]:
    """All fixes, keyed by id. Importing `fix.fixes` is what populates this."""
    return dict(_REGISTRY)


def get_fix(fix_id: str) -> Fix | None:
    return _REGISTRY.get(fix_id)


def load_builtin_fixes() -> dict[str, Fix]:
    """Import the fix modules so the registry is populated.

    Uses `importlib.import_module` rather than a plain import for the same reason
    `verify.rules.load_builtin_checks` does: `ruff --fix` deletes an unused import as F401, and a silently
    empty fix registry is indistinguishable from a program with nothing to fix.
    """
    import importlib

    importlib.import_module("foursight.fix.fixes")
    return registered_fixes()


def apply_fix(fix_id: str, context: FixContext) -> FixResult:
    """Run one fix. The caller re-parses, re-verifies and rebuilds afterwards — see the module docstring.

    An unknown id is a `FixResult` refusal rather than an exception: fix ids arrive from `Diagnostic.fix_ids`
    and from UI actions, so a stale one is a plausible runtime condition rather than a programming error,
    and the user should see a sentence instead of a traceback.
    """
    load_builtin_fixes()
    fix = get_fix(fix_id)
    if fix is None:
        return FixResult(fix_id=fix_id, text=None, diff="", refusal=f"no such fix: {fix_id}")
    if fix.needs_parameter and context.parameter is None:
        return FixResult(
            fix_id=fix_id,
            text=None,
            diff="",
            refusal=f"{fix.title} needs a value: {fix.parameter_prompt}",
        )
    try:
        return fix.transform(context)
    except Exception as error:  # noqa: BLE001 - a fix raising is our bug; the buffer must survive it
        # Converted rather than propagated for the same reason `verify` converts a raising rule: the user's
        # buffer must not be lost because our transform tripped, and a traceback tells them nothing they
        # can act on.
        return FixResult(
            fix_id=fix_id,
            text=None,
            diff="",
            refusal=f"the fix could not be applied: {type(error).__name__}: {error}",
        )


def succeeded(
    fix_id: str, context: FixContext, new_text: str, note: str | None = None
) -> FixResult:
    """Helper for a fix that produced text: builds the diff so no fix has to remember to."""
    return FixResult(
        fix_id=fix_id,
        text=new_text,
        diff=unified_diff(context.text, new_text),
        note=note,
    )


def refuse(fix_id: str, reason: str) -> FixResult:
    """Helper for a fix that declines. The reason is shown to the user verbatim."""
    return FixResult(fix_id=fix_id, text=None, diff="", refusal=reason)


@dataclass(slots=True)
class FixHistory:
    """What has been applied to the current buffer, for an undo stack and for the manual script.

    Kept as *text snapshots* rather than diffs. Reversing a diff is exactly the rebasing the one-fix
    contract forbids, and a snapshot cannot be applied to the wrong place.
    """

    snapshots: list[tuple[str, str]] = field(default_factory=list)  # (fix_id, text before it)

    def record(self, fix_id: str, text_before: str) -> None:
        self.snapshots.append((fix_id, text_before))

    def undo(self) -> tuple[str, str] | None:
        """The most recent (fix_id, text before it), or None."""
        return self.snapshots.pop() if self.snapshots else None

    @property
    def depth(self) -> int:
        return len(self.snapshots)

    def clear(self) -> None:
        self.snapshots.clear()
