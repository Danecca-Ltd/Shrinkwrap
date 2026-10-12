"""Shrinkwrap - Fusion add-in.

Solid > Modify > Shrinkwrap has two modes, picked automatically:

Assembly mode (more than one visible solid body) turns the active part or
assembly into one solid "shrinkwrap" body in a new internal component:

1. Every visible solid body (assembly bodies included) is copied into root
   space as a temporary B-Rep and united into one solid. Faces where parts
   touch, and parts buried inside other parts, disappear in the union.
2. Sealed internal cavities (void shells of the union) are filled by deleting
   their faces. Through-holes, pockets and gaps that reach the outside stay open.
   With Fill = Envelope, everything enclosed along X, Y and Z is also filled,
   even if the inside leaks to the outside (see "Envelope fill" below).
3. The result goes into a new component "<name> shrinkwrap", with a copy of
   the default material, "<name> material", whose density is tuned so the
   shrinkwrap weighs exactly what the original bodies weigh, or a mass the
   user types into the dialog.
4. Optionally, a construction point "Center of mass" marks the original
   centre of mass.
The source design is never modified.

Single-body mode (exactly one visible solid body, e.g. a shrinkwrap with its
source parts hidden, or a one-body part) fills that body's cavities in place
and re-tunes its material density so its mass stays the same (or matches a
manual mass).

Either way the whole run is one undo step.
"""

import adsk.core
import adsk.fusion
import contextlib
import importlib
import json
import math
import os
import re
import sys
import threading
import time
import traceback

# The updater lives next to this file. Its name is specific to this add-in
# because all Fusion add-ins share one Python, so sys.modules is shared too.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import shrinkwrap_updater as updater  # noqa: E402
importlib.reload(updater)  # pick up a new version on add-in Stop/Run

SHRINKWRAP_VERSION = '1.3.0'

CMD_ID = 'DaneccaShrinkwrapCmd'
CMD_NAME = 'Shrinkwrap'
CMD_TOOLTIP = ('Create a solid body from the external surfaces of the whole design, '
               'in a new component, with a material that matches the original mass. '
               'On a single-body design, fills that body\'s cavities in place.')
WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'SolidModifyPanel'

# Marks components created by Shrinkwrap so a re-run ignores them.
ATTR_GROUP = 'Shrinkwrap'
ATTR_OUTPUT = 'output'

COM_POINT_NAME = 'Center of mass'
DENSITY_PROPERTY_ID = 'structural_Density'

# Bounding boxes closer than this (cm) are considered identical.
BOX_TOLERANCE = 1e-4
# Relative mass error we accept after density tuning.
MASS_TOLERANCE = 1e-6
# Less added volume than this (cm^3) counts as "nothing was filled".
MIN_ADDED_VOLUME = 1e-6

ACCURACY = adsk.fusion.CalculationAccuracy.VeryHighCalculationAccuracy

# Envelope fill (all lengths in cm)
# Detail level -> grid cells along the model's longest side. Ray count (and
# time) grows with the square of this.
DETAIL_COARSE = 'Coarse'
DETAIL_NORMAL = 'Normal'
DETAIL_FINE = 'Fine'
DETAIL_CELLS = {DETAIL_COARSE: 50, DETAIL_NORMAL: 100, DETAIL_FINE: 200}
DEFAULT_WALL = 0.3              # default wall thickness: 3 mm
ENVELOPE_MIN_CELL = 0.05        # never finer than 0.5 mm
ENVELOPE_MIN_CORNERS = 2        # a cell needs this many corner rays to hit
ENVELOPE_PAD = 1.0              # rays start this far outside the bounding box
ENVELOPE_SNAP = 0.001           # fill boundaries snap (inwards) to a 10 µm grid
ENVELOPE_ROUND = 6              # strips float noise so equal spans compare equal
ENVELOPE_MIN_THICKNESS = 1e-4   # ignore cells thinner than this
PROGRESS_EVERY = 20             # rays between progress updates / cancel checks
LOG_RAYS_EVERY = 500            # rays between envelope progress lines in the log

# Run logs: one file per run, newest LOG_KEEP kept.
LOG_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'Shrinkwrap logs')
LOG_KEEP = 30
# Bodies whose union froze Fusion, per design, skipped in later runs. Written
# by the freeze recovery below; delete an entry (or the file) to retry it.
PROBLEM_FILE = os.path.join(LOG_DIR, 'problem bodies.json')
# Names the body being united; left behind only if Fusion dies mid-union.
PENDING_FILE = os.path.join(LOG_DIR, 'union in progress.json')

# Dialog inputs
INPUT_COM = 'add_com'
INPUT_FILL = 'fill_mode'
INPUT_DETAIL = 'detail'
INPUT_WALL = 'wall_thickness'
INPUT_MASS_SOURCE = 'mass_source'
INPUT_MASS = 'manual_mass'
INPUT_INFO = 'info'
FILL_SEALED = 'Sealed cavities only'
FILL_ENVELOPE = 'Envelope (fill all internals)'
MASS_FROM_MODEL = 'Inherit from model'
MASS_MANUAL = 'Set manually'

# Dialog choices remembered between runs in this Fusion session.
_last_options = {'add_com': True, 'envelope': False, 'detail': DETAIL_COARSE,
                 'wall': DEFAULT_WALL, 'manual': False, 'mass': None}

handlers = []  # Keeps event handlers alive (see the development guide, section 5)


def _app():
    return adsk.core.Application.get()


def _log(msg):
    """Log to the Text Commands window and, during a run, to the run's log file."""
    try:
        _app().log(f'[Shrinkwrap] {msg}')
    except Exception:
        pass
    if _run_log:
        _run_log.write(msg)


class _RunLog:
    """One log file per run, flushed to disk after every line.

    If Fusion freezes or crashes mid-run, the file still ends with the last
    step that started, which is the one it got stuck on.
    """

    def __init__(self, title):
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        self.path = os.path.join(LOG_DIR, f'{title.lower().replace(" ", "-")}_{stamp}.log')
        self._file = open(self.path, 'a', encoding='utf-8')
        self._start = time.perf_counter()
        self._prune()

    def write(self, msg):
        try:
            elapsed = time.perf_counter() - self._start
            self._file.write(f'{time.strftime("%H:%M:%S")} +{elapsed:8.1f}s  {msg}\n')
            self._file.flush()
            os.fsync(self._file.fileno())
        except Exception:
            pass

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass

    @staticmethod
    def _prune():
        """Keep only the newest LOG_KEEP log files."""
        try:
            logs = sorted((f for f in os.listdir(LOG_DIR) if f.endswith('.log')),
                          key=lambda f: os.path.getmtime(os.path.join(LOG_DIR, f)))
            for name in logs[:-LOG_KEEP]:
                os.remove(os.path.join(LOG_DIR, name))
        except Exception:
            pass


_run_log = None  # the _RunLog of the run in progress, if any


@contextlib.contextmanager
def _logged_run(title):
    """Open a run log for the duration of a command, and log any crash in it."""
    global _run_log
    try:
        _run_log = _RunLog(title)
    except Exception:
        _run_log = None
    try:
        app = _app()
        doc = app.activeDocument
        _log(f'{title} v{SHRINKWRAP_VERSION} | Fusion {app.version} | '
             f'document "{doc.name if doc else "?"}"')
        yield _run_log
        _log('Finished.')
    except Exception:
        _log('Stopped with an error:\n' + traceback.format_exc())
        raise
    finally:
        if _run_log:
            _run_log.close()
        _run_log = None


def _log_path_line():
    return f'Log: {_run_log.path}' if _run_log else ''


def _describe_body(body):
    """Size facts about a body for the log (cheap properties only)."""
    try:
        box = body.boundingBox
        size = box.maxPoint.vectorTo(box.minPoint).length * 10
        return f'{body.faces.count} faces, {body.edges.count} edges, {size:.0f} mm across'
    except Exception as e:
        return f'(could not describe: {e})'


class ShrinkwrapError(Exception):
    """A failure we report to the user without a traceback."""


class _Cancelled(Exception):
    """The user pressed Cancel in a progress dialog."""


class _Timings:
    """Wall-clock time per step, in the order the steps first ran."""

    def __init__(self):
        self._seconds = {}

    @contextlib.contextmanager
    def phase(self, label):
        start = time.perf_counter()
        try:
            yield
        finally:
            self._seconds[label] = self._seconds.get(label, 0.0) + time.perf_counter() - start

    def total(self):
        return sum(self._seconds.values())

    def summary(self):
        return ' · '.join(f'{label} {_format_seconds(s)}' for label, s in self._seconds.items())


def _format_seconds(seconds):
    if seconds < 10:
        return f'{seconds:.1f} s'
    if seconds < 60:
        return f'{seconds:.0f} s'
    return f'{int(seconds // 60)} min {seconds % 60:.0f} s'


def _strip_version(name):
    """Drop Fusion's ' v12' version suffix."""
    return re.sub(r'\s+v\d+$', '', name or '').strip()


# ----------------------------------------------------------------------------
# Collecting source bodies
# ----------------------------------------------------------------------------

def _is_output_component(comp):
    try:
        return bool(comp.attributes.itemByName(ATTR_GROUP, ATTR_OUTPUT))
    except Exception:
        return False


def _is_output_occurrence(occ):
    """True if this occurrence, or any parent of it, is a previous Shrinkwrap output."""
    cur = occ
    while cur:
        if _is_output_component(cur.component):
            return True
        cur = cur.assemblyContext
    return False


def _collect_bodies(design):
    """Visible solid bodies of the design, as proxies in root context.

    Previous Shrinkwrap outputs are left out.
    Returns (bodies, hidden_count, non_solid_count).
    """
    root = design.rootComponent
    bodies = []
    counts = {'hidden': 0, 'non_solid': 0}

    def consider(body):
        if not body.isSolid:
            counts['non_solid'] += 1
        elif not body.isVisible:
            counts['hidden'] += 1
        else:
            bodies.append(body)

    for body in root.bRepBodies:
        consider(body)
    for occ in root.allOccurrences:
        if _is_output_occurrence(occ):
            continue
        for body in occ.bRepBodies:
            consider(body)

    return bodies, counts['hidden'], counts['non_solid']


def _visible_output_bodies(design):
    bodies = []
    for occ in design.rootComponent.allOccurrences:
        if _is_output_component(occ.component):
            bodies.extend(b for b in occ.bRepBodies if b.isSolid and b.isVisible)
    return bodies


def _find_single_body(design):
    """The body to refill in single-body mode, or None for assembly mode.

    That's the only visible solid body in the design, or, when every source
    body is hidden, the only visible Shrinkwrap output body.
    """
    bodies, _hidden, _non_solid = _collect_bodies(design)
    if len(bodies) == 1:
        return bodies[0]
    if bodies:
        return None
    outputs = _visible_output_bodies(design)
    return outputs[0] if len(outputs) == 1 else None


def _mass_properties(bodies):
    """Total mass (kg) and mass-weighted centre of mass (Point3D, cm, root space)."""
    total = 0.0
    sx = sy = sz = 0.0
    for body in bodies:
        props = body.getPhysicalProperties(ACCURACY)
        m = props.mass
        c = props.centerOfMass
        total += m
        sx += m * c.x
        sy += m * c.y
        sz += m * c.z
    if total <= 0:
        return 0.0, None
    return total, adsk.core.Point3D.create(sx / total, sy / total, sz / total)


def _body_label(body):
    if body.assemblyContext:
        return f'{body.assemblyContext.fullPathName} / {body.name}'
    return body.name


# ----------------------------------------------------------------------------
# Building the shrinkwrap geometry
# ----------------------------------------------------------------------------

def _same_box(a, b):
    return (a.minPoint.distanceTo(b.minPoint) < BOX_TOLERANCE and
            a.maxPoint.distanceTo(b.maxPoint) < BOX_TOLERANCE)


def _path_transform(occ):
    """Occurrence-to-root transform built by walking the assembly path."""
    m = adsk.core.Matrix3D.create()
    cur = occ
    while cur:
        native = cur.nativeObject or cur
        m.transformBy(native.transform2)
        cur = cur.assemblyContext
    return m


def _root_space_copy(tbm, body):
    """Temporary copy of a (possibly proxy) body, positioned in root space.

    Whether TemporaryBRepManager.copy() of a proxy already applies the
    occurrence transform isn't documented, so check against the proxy's
    bounding box (which is in root space) and transform only if needed.
    """
    tmp = tbm.copy(body)
    if tmp is None:
        return None
    occ = body.assemblyContext
    target_box = body.boundingBox
    if occ is None or _same_box(tmp.boundingBox, target_box):
        return tmp

    for transform in (occ.transform2, _path_transform(occ)):
        candidate = tbm.copy(body)
        if candidate and tbm.transform(candidate, transform) and \
                _same_box(candidate.boundingBox, target_box):
            return candidate

    _log(f'Could not place "{body.name}" in {occ.fullPathName}; using untransformed copy')
    return tmp


def _boolean(tbm, target, tool, boolean_type):
    """TemporaryBRepManager.booleanOperation that reports failure instead of raising.

    Fusion raises (e.g. ASM_BAD_CONTAINMENT on nearly coincident faces) as well
    as returning False. Either way the target may be damaged, so callers keep
    a copy to fall back to.
    """
    try:
        return tbm.booleanOperation(target, tool, boolean_type)
    except Exception as e:
        _log(f'Boolean {boolean_type} failed: {e}'.splitlines()[0])
        return False


# ----------------------------------------------------------------------------
# Freeze recovery
#
# Fusion's geometry engine can hang for good on some unions (e.g. a nut whose
# bore sits exactly on a thread cylinder), and Python can't interrupt it. So
# before each union the body is named in PENDING_FILE, which is removed again
# afterwards. If Fusion dies mid-union the file survives; the next run moves
# that body into PROBLEM_FILE, and bodies listed there for the design are
# skipped from then on (their mass still counts).
# ----------------------------------------------------------------------------

def _load_problems():
    try:
        with open(PROBLEM_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log(f'Could not read {PROBLEM_FILE}: {e}')
        return {}


def _save_problems(data):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = PROBLEM_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PROBLEM_FILE)


def _recover_from_freeze():
    """If the last run died mid-union, record that body as a problem.

    Returns (design_key, body_label) of the recovered body, or None.
    """
    try:
        with open(PENDING_FILE, encoding='utf-8') as f:
            pending = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        pending = {}
    _clear_pending()
    design_key, label = pending.get('design'), pending.get('body')
    if not (design_key and label):
        return None
    data = _load_problems()
    labels = data.setdefault(design_key, [])
    if label not in labels:
        labels.append(label)
        _save_problems(data)
    _log(f'Previous run froze while uniting "{label}" in "{design_key}"; '
         'it will be skipped from now on.')
    return design_key, label


def _mark_pending(design_key, label):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(PENDING_FILE, 'w', encoding='utf-8') as f:
            json.dump({'design': design_key, 'body': label,
                       'started': time.strftime('%Y-%m-%d %H:%M:%S')}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _clear_pending():
    try:
        os.remove(PENDING_FILE)
    except Exception:
        pass


def _union_bodies(tbm, bodies, progress, design_key, skip):
    """Unite all bodies into one temporary body, leaving out labels in skip.

    Returns (result_body, failed_names, skipped_names). result_body is None if
    cancelled.
    """
    result = None
    failed = []
    skipped = []
    n = len(bodies)
    try:
        for i, body in enumerate(bodies):
            progress.progressValue = i
            adsk.doEvents()
            if progress.wasCancelled:
                _log('Union cancelled by user.')
                return None, failed, skipped

            # Logged before the slow calls, so a freeze leaves this as the last line.
            label = _body_label(body)
            if label in skip:
                skipped.append(label)
                _log(f'Union {i + 1}/{n}: "{label}" SKIPPED (froze Fusion in an earlier run)')
                continue
            _log(f'Union {i + 1}/{n}: "{label}" ({_describe_body(body)})')
            _mark_pending(design_key, label)
            start = time.perf_counter()
            tmp = _root_space_copy(tbm, body)
            if tmp is None:
                failed.append(label)
                _log(f'  copy FAILED')
                continue
            if result is None:
                result = tmp
                _log(f'  first body ({time.perf_counter() - start:.2f} s)')
                continue
            backup = tbm.copy(result)
            if _boolean(tbm, result, tmp, adsk.fusion.BooleanTypes.UnionBooleanType):
                _log(f'  united ({time.perf_counter() - start:.2f} s); '
                     f'result now {result.faces.count} faces')
            else:
                result = backup
                failed.append(label)
                _log(f'  union FAILED ({time.perf_counter() - start:.2f} s); body left out')
    finally:
        _clear_pending()

    progress.progressValue = n
    return result, failed, skipped


def _union_all(tbm, bodies, what):
    """Unite many small bodies pairwise, like a tournament.

    Much cheaper than adding them one by one to an ever-growing body. A pair
    that won't unite keeps the first body and drops the second.
    """
    bodies = [b for b in bodies if b]
    _log(f'{what}: uniting {len(bodies)} boxes')
    while len(bodies) > 1:
        merged = []
        for a, b in zip(bodies[0::2], bodies[1::2]):
            backup = tbm.copy(a)
            if not _boolean(tbm, a, b, adsk.fusion.BooleanTypes.UnionBooleanType):
                a = backup
                _log(f'{what}: a union failed; one piece was dropped')
            merged.append(a)
        if len(bodies) % 2:
            merged.append(bodies[-1])
        bodies = merged
    return bodies[0] if bodies else None


def _fill_cavities(tbm, body):
    """Fill sealed internal cavities by deleting the faces of void shells.

    Returns (body, cavities_filled, warning). On failure the original,
    unfilled body is returned with a warning.
    """
    void_faces = []
    cavities = 0
    for lump in body.lumps:
        for shell in lump.shells:
            if shell.isVoid:
                cavities += 1
                void_faces.extend(shell.faces)

    _log(f'Sealed cavities: {cavities} found ({len(void_faces)} faces)')
    if not void_faces:
        return body, 0, None

    backup = tbm.copy(body)
    volume_before = body.volume
    try:
        ok = tbm.deleteFaces(void_faces, True)
    except Exception:
        ok = False
    if ok and body.isSolid and body.volume > volume_before:
        return body, cavities, None

    _log(f'Cavity fill failed (ok={ok}); keeping {cavities} cavities')
    return backup, 0, f'{cavities} internal cavities could not be filled and were left hollow.'


# ----------------------------------------------------------------------------
# Envelope fill
#
# Sealed-cavity filling fails as soon as the inside connects to the outside,
# even through a hairline clearance. Envelope mode doesn't care about leaks.
# Along each axis it casts rays through the model on a uniform grid. Each ray
# needs only its first and last hit: the fill runs from one wall thickness
# inside the first hit to one wall thickness inside the last (see
# _RayCaster.span), so the outer skin, and any hole or pocket shallower than
# the wall thickness, is never filled. A grid cell is filled over the range its
# corner rays share; corners whose rays miss are ignored, so cells at the
# walls are filled too. The three axis fills are intersected, so a region is
# filled only if it's at least a wall thickness inside the skin along X, Y and
# Z. That trims each axis's fill exactly to the walls and keeps exterior
# pockets and the spaces between protruding parts open.
# ----------------------------------------------------------------------------

class _RayCaster:
    """Casts rays and turns the hits into fill ranges.

    accept(face) decides which hit faces count (e.g. ignore previous outputs,
    or only the one body being refilled).
    """

    def __init__(self, root, accept, wall, progress):
        self.root = root
        self.accept = accept
        self.wall = wall
        self.progress = progress
        self.count = 0

    def span(self, origin, direction, axis):
        """(lo, hi) range to fill along axis on this ray, or None if it hits nothing.

        lo/hi are the first/last hit moved one wall thickness inwards. lo >= hi
        (nothing to fill) is still returned, so a cell with this corner stays
        empty rather than ignoring it.
        """
        hit_points = adsk.core.ObjectCollection.create()
        faces = self.root.findBRepUsingRay(
            origin, direction, adsk.fusion.BRepEntityTypes.BRepFaceEntityType,
            -1.0, True, hit_points)
        self.count += 1
        if self.count % LOG_RAYS_EVERY == 0:
            _log(f'  {self.count} rays cast')
        if self.progress:
            self.progress.tick(self.count)
        hits = [_coord(hit_points.item(i), axis)
                for i in range(faces.count) if self.accept(faces.item(i))]
        if not hits:
            return None
        return _snap(min(hits) + self.wall, max(hits) - self.wall)


def _accept_non_output():
    """Hit filter for assembly mode: everything except previous outputs."""
    cache = {}

    def accept(face):
        occ = face.assemblyContext
        if occ is None:
            return True
        key = occ.fullPathName
        if key not in cache:
            cache[key] = not _is_output_occurrence(occ)
        return cache[key]
    return accept


def _accept_only(body):
    """Hit filter for single-body mode: only faces of this body."""
    def key(b):
        occ = b.assemblyContext
        return (occ.fullPathName if occ else '', b.name)
    wanted = key(body)
    return lambda face: key(face.body) == wanted


class _RayProgress:
    """Progress dialog for ray casting, with a cancel check."""

    def __init__(self, total):
        self.dialog = _app().userInterface.createProgressDialog()
        self.dialog.isCancelButtonShown = True
        self.dialog.show('Shrinkwrap', 'Envelope: casting ray %v of %m', 0, max(1, total))

    def tick(self, count):
        if count % PROGRESS_EVERY:
            return
        self.dialog.progressValue = count
        adsk.doEvents()
        if self.dialog.wasCancelled:
            raise _Cancelled()

    def hide(self):
        self.dialog.hide()


def _snap(lo, hi):
    """Shrink a span onto the ENVELOPE_SNAP grid.

    Spans from different rays on the same wall then come out identical (so
    their boxes merge, and faces line up exactly) instead of nanometres apart,
    which Fusion's booleans can't cope with.
    """
    lo = math.ceil(round(lo / ENVELOPE_SNAP, 3)) * ENVELOPE_SNAP
    hi = math.floor(round(hi / ENVELOPE_SNAP, 3)) * ENVELOPE_SNAP
    return (round(lo, ENVELOPE_ROUND), round(hi, ENVELOPE_ROUND))


def _coord(point, axis):
    return (point.x, point.y, point.z)[axis]


def _make_point(coords):
    return adsk.core.Point3D.create(coords[0], coords[1], coords[2])


def _unit(axis):
    c = [0.0, 0.0, 0.0]
    c[axis] = 1.0
    return adsk.core.Vector3D.create(c[0], c[1], c[2])


def _merge_cells(cells, nu, nv):
    """Merge equal-span grid cells into rectangles: [(i0, i1, j0, j1, span)]."""
    rects = []
    open_runs = {}
    for j in range(nv):
        runs = []
        i = 0
        while i < nu:
            span = cells[i][j]
            if span is None:
                i += 1
                continue
            i0 = i
            while i + 1 < nu and cells[i + 1][j] == span:
                i += 1
            runs.append((i0, i, span))
            i += 1
        still_open = {}
        for i0, i1, span in runs:
            key = (i0, i1, span)
            rect = open_runs.pop(key, None) or [i0, i1, j, j, span]
            rect[3] = j
            still_open[key] = rect
        rects.extend(open_runs.values())
        open_runs = still_open
    rects.extend(open_runs.values())
    return rects


def _grid_cells(nodes, nu, nv):
    """Cell spans from node spans: the range all hitting corners share.

    Corners whose ray missed are ignored (at least ENVELOPE_MIN_CORNERS must
    hit), so cells at the edge of the silhouette are filled too; the other two
    axes trim them back to the walls.
    """
    cells = [[None] * nv for _ in range(nu)]
    for i in range(nu):
        for j in range(nv):
            corners = [c for c in (nodes[i][j], nodes[i + 1][j],
                                   nodes[i][j + 1], nodes[i + 1][j + 1]) if c is not None]
            if len(corners) < ENVELOPE_MIN_CORNERS:
                continue
            lo = max(c[0] for c in corners)
            hi = min(c[1] for c in corners)
            if hi - lo > ENVELOPE_MIN_THICKNESS:
                cells[i][j] = (lo, hi)
    return cells


def _axis_fill(tbm, caster, mn, mx, axis, cell, timings):
    """Fill body for one axis, or None if nothing to fill."""
    u, v = [a for a in range(3) if a != axis]
    nu = max(1, int(round((mx[u] - mn[u]) / cell)))
    nv = max(1, int(round((mx[v] - mn[v]) / cell)))
    du = (mx[u] - mn[u]) / nu
    dv = (mx[v] - mn[v]) / nv
    direction = _unit(axis)
    _log(f'Envelope axis {"XYZ"[axis]}: {(nu + 1) * (nv + 1)} rays on a {nu} x {nv} grid')

    with timings.phase('Envelope rays'):
        nodes = [[None] * (nv + 1) for _ in range(nu + 1)]
        for i in range(nu + 1):
            for j in range(nv + 1):
                p = [0.0, 0.0, 0.0]
                p[axis] = mn[axis] - ENVELOPE_PAD
                p[u] = mn[u] + i * du
                p[v] = mn[v] + j * dv
                nodes[i][j] = caster.span(_make_point(p), direction, axis)

    with timings.phase('Envelope boxes'):
        boxes = []
        for i0, i1, j0, j1, (lo, hi) in _merge_cells(_grid_cells(nodes, nu, nv), nu, nv):
            center = [0.0, 0.0, 0.0]
            center[axis] = (lo + hi) / 2
            center[u] = mn[u] + (i0 + i1 + 1) * du / 2
            center[v] = mn[v] + (j0 + j1 + 1) * dv / 2
            obb = adsk.core.OrientedBoundingBox3D.create(
                _make_point(center), _unit(u), _unit(v),
                (i1 - i0 + 1) * du, (j1 - j0 + 1) * dv, hi - lo)
            boxes.append(tbm.createBox(obb))
        return _union_all(tbm, boxes, f'Envelope axis {"XYZ"[axis]}')


def _envelope_fill(tbm, root, merged, accept, detail, wall, timings):
    """Fill everything at least `wall` (cm) inside the skin along X, Y and Z.

    Rays hit the faces that accept() lets through. Returns (body, info,
    warning), or None if cancelled.
    """
    box = merged.boundingBox
    mn = [box.minPoint.x, box.minPoint.y, box.minPoint.z]
    mx = [box.maxPoint.x, box.maxPoint.y, box.maxPoint.z]
    cell = max(max(b - a for a, b in zip(mn, mx)) / DETAIL_CELLS[detail], ENVELOPE_MIN_CELL)

    def nodes(u, v):
        return (max(1, int(round((mx[u] - mn[u]) / cell))) + 1) * \
               (max(1, int(round((mx[v] - mn[v]) / cell))) + 1)
    total = nodes(1, 2) + nodes(0, 2) + nodes(0, 1)

    progress = _RayProgress(total)
    caster = _RayCaster(root, accept, wall, progress)
    try:
        fills = [_axis_fill(tbm, caster, mn, mx, axis, cell, timings) for axis in range(3)]
    except _Cancelled:
        return None
    finally:
        progress.hide()

    info = (f'{detail}, {caster.count} rays, {cell * 10:.2g} mm grid, '
            f'{wall * 10:.3g} mm walls')
    if any(f is None for f in fills):
        return merged, info, 'Envelope found nothing to fill (is the wall thickness too large?).'

    _log('Envelope: intersecting the X, Y and Z fills')
    with timings.phase('Envelope boxes'):
        fill = fills[0]
        for other in fills[1:]:
            if not _boolean(tbm, fill, other, adsk.fusion.BooleanTypes.IntersectionBooleanType):
                return merged, info, 'Envelope fill failed (could not intersect the axis fills).'
    if fill.volume <= 0:
        return merged, info, 'Envelope found nothing to fill (is the wall thickness too large?).'

    _log(f'Envelope: merging the fill ({fill.faces.count} faces) into the body')
    with timings.phase('Envelope merge'):
        backup = tbm.copy(merged)
        merged_ok = _boolean(tbm, merged, fill, adsk.fusion.BooleanTypes.UnionBooleanType) \
            and merged.isSolid
    if not merged_ok:
        return backup, info, 'Envelope fill failed (could not merge the fill into the body).'
    _log(f'Envelope: {info}, added {merged.volume - backup.volume:.6g} cm^3')
    return merged, info, None


# ----------------------------------------------------------------------------
# Writing results into the design
# ----------------------------------------------------------------------------

def _base_name(design):
    """Design name without Fusion's ' v12' version suffix."""
    name = ''
    doc = _app().activeDocument
    try:
        if doc and doc.dataFile:
            name = doc.dataFile.name
    except Exception:
        pass
    if not name:
        name = design.rootComponent.name or (doc.name if doc else '')
    return _strip_version(name) or 'Design'


def _add_com_point_direct(comp, point, base_feature=None):
    cp_input = comp.constructionPoints.createInput()
    if base_feature:
        cp_input.targetBaseOrFormFeature = base_feature
    if not cp_input.setByPoint(point):
        return None
    return comp.constructionPoints.add(cp_input)


def _add_com_point_via_sketch(comp, point):
    """Parametric fallback: construction point on a hidden sketch point."""
    sketch = comp.sketches.add(comp.xYConstructionPlane)
    sketch.name = COM_POINT_NAME
    sketch_point = sketch.sketchPoints.add(sketch.modelToSketchSpace(point))
    cp_input = comp.constructionPoints.createInput()
    cp_input.setByPoint(sketch_point)
    cp = comp.constructionPoints.add(cp_input)
    sketch.isLightBulbOn = False
    return cp


def _is_parametric(design):
    return design.designType == adsk.fusion.DesignTypes.ParametricDesignType


def _create_output(design, body, base_name, com_point):
    """New internal component holding the shrinkwrap body and CoM point."""
    root = design.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = f'{base_name} shrinkwrap'
    comp.attributes.add(ATTR_GROUP, ATTR_OUTPUT, SHRINKWRAP_VERSION)

    com = None
    parametric = _is_parametric(design)
    if parametric:
        base_feature = comp.features.baseFeatures.add()
        base_feature.startEdit()
        try:
            if not comp.bRepBodies.add(body, base_feature):
                raise ShrinkwrapError('Fusion refused to add the shrinkwrap body to the design.')
            if com_point:
                try:
                    com = _add_com_point_direct(comp, com_point, base_feature)
                except Exception:
                    com = None
        finally:
            base_feature.finishEdit()
        base_feature.name = comp.name
    else:
        if not comp.bRepBodies.add(body):
            raise ShrinkwrapError('Fusion refused to add the shrinkwrap body to the design.')

    if com_point and com is None:
        if parametric:
            com = _add_com_point_via_sketch(comp, com_point)
        else:
            com = _add_com_point_direct(comp, com_point)
    if com:
        com.name = COM_POINT_NAME

    for i, b in enumerate(comp.bRepBodies):
        b.name = comp.name if i == 0 else f'{comp.name} {i + 1}'

    return occ, comp, com is not None


def _replace_body(design, body, new_root_space):
    """Give body the geometry of new_root_space (a root-space temporary body).

    A Shrinkwrap output in a parametric design has its base feature updated in
    place. Any other parametric body is replaced by a new base feature plus a
    Remove feature for the old body. In a direct design the body is swapped.
    Returns the resulting (native) body.
    """
    tbm = adsk.fusion.TemporaryBRepManager.get()
    native = body.nativeObject or body
    comp = native.parentComponent
    name = native.name

    new = tbm.copy(new_root_space)
    if body.assemblyContext:
        to_component = _path_transform(body.assemblyContext)
        to_component.invert()
        tbm.transform(new, to_component)

    if not _is_parametric(design):
        added = comp.bRepBodies.add(new)
        if not added:
            raise ShrinkwrapError('Fusion refused to add the filled body to the design.')
        native.deleteMe()
        added.name = name
        return added

    base_features = comp.features.baseFeatures
    if _is_output_component(comp) and base_features.count == 1:
        base_feature = base_features.item(0)
        base_feature.startEdit()
        try:
            sources = base_feature.bodies  # source bodies while editing
            updated = sources.count == 1 and base_feature.updateBody(sources.item(0), new)
        except Exception:
            updated = False
        finally:
            base_feature.finishEdit()
        if updated:
            return comp.bRepBodies.itemByName(name) or comp.bRepBodies.item(0)
        _log('Base feature update failed; replacing the body instead')

    base_feature = base_features.add()
    base_feature.startEdit()
    try:
        added = comp.bRepBodies.add(new, base_feature)
    finally:
        base_feature.finishEdit()
    if not added:
        raise ShrinkwrapError('Fusion refused to add the filled body to the design.')
    base_feature.name = f'{name} filled'
    comp.features.removeFeatures.add(native)
    result = base_feature.bodies.item(0)
    result.name = name
    return result


def _find_density_property(material):
    props = material.materialProperties
    for i in range(props.count):
        prop = props.item(i)
        if prop.id == DENSITY_PROPERTY_ID or prop.name.lower() == 'density':
            return adsk.core.FloatProperty.cast(prop)
    return None


def _default_material():
    app = _app()
    mat = None
    try:
        mat = app.preferences.materialPreferences.defaultMaterial
    except Exception:
        mat = None
    if mat:
        return mat
    lib = app.materialLibraries.itemByName('Fusion Material Library')
    return lib.materials.itemByName('Steel') if lib else None


def _material_copy(design, name, source):
    """The design material called name, creating it as a copy of source."""
    material = design.materials.itemByName(name)
    if material:
        return material
    source = source or _default_material()
    if not source:
        raise ShrinkwrapError('Could not find a material to copy.')
    material = design.materials.addByCopy(source, name)
    if not material:
        raise ShrinkwrapError(f'Could not create material "{name}".')
    return material


def _tune_density(material, assign, measure, target_mass):
    """Scale the material's density until measure() returns target_mass.

    Density is scaled by target/actual mass, so the property's internal units
    never matter. assign(material) applies the material; returns the mass.
    """
    density = _find_density_property(material)
    if not density:
        raise ShrinkwrapError(f'Material "{material.name}" has no density property.')

    mass = 0.0
    for _attempt in range(5):
        assign(material)
        mass = measure()
        if mass <= 0:
            raise ShrinkwrapError('The body has no mass; it may not be a valid solid.')
        if abs(mass - target_mass) / target_mass < MASS_TOLERANCE:
            break
        density.value = density.value * target_mass / mass
    return mass


def _own_material(design, bodies, current):
    """A material these bodies can have to themselves, so its density can change.

    Changing the density of a shared material (e.g. library Steel) would change
    every other body using it. So the bodies get a copy of their current
    material named "<owner> material", where owner is their component (or the
    design, for root bodies, or the first body's name if they're spread over
    several components). A material that already follows that pattern, or that
    Shrinkwrap made for its output, is reused.
    """
    natives = [b.nativeObject or b for b in bodies]
    comps = {n.parentComponent.entityToken: n.parentComponent for n in natives}
    if len(comps) == 1:
        comp = next(iter(comps.values()))
        if comp == design.rootComponent:
            owner = _base_name(design)
        else:
            owner = _strip_version(comp.name)
            if _is_output_component(comp) and current and current.name.endswith(' material'):
                return current
    else:
        owner = natives[0].name
    return _material_copy(design, f'{owner} material', current)


def _set_mass(material, bodies, target_mass):
    """Assign material to the bodies and tune its density to target_mass total."""
    natives = [b.nativeObject or b for b in bodies]

    def assign(m):
        for b in natives:
            b.material = m

    def measure():
        return sum(b.getPhysicalProperties(ACCURACY).mass for b in natives)

    return _tune_density(material, assign, measure, target_mass)


def _density_g_cm3(mass, bodies):
    volume = sum(b.volume for b in bodies)  # cm^3
    return mass * 1000.0 / volume if volume > 0 else 0.0


# ----------------------------------------------------------------------------
# The two modes
# ----------------------------------------------------------------------------

def _run_shrinkwrap(design, add_com, envelope, detail, wall, manual_mass):
    """Assembly mode. Returns a summary message, or None if cancelled.

    add_com: create the "Center of mass" point.
    envelope: also fill everything enclosed along X, Y and Z (leak tolerant).
    detail: envelope detail level, a key of DETAIL_CELLS.
    wall: envelope wall thickness in cm.
    manual_mass: target mass in kg, or None to inherit the model's mass.
    """
    ui = _app().userInterface
    timings = _Timings()
    _log(f'Assembly mode | fill: {"envelope" if envelope else "sealed only"} | '
         f'detail: {detail} | wall: {wall * 10:.3g} mm | center of mass: {add_com} | '
         f'mass: {"manual %.6g kg" % manual_mass if manual_mass is not None else "inherit"}')
    with timings.phase('Mass'):
        bodies, hidden, non_solid = _collect_bodies(design)
        if not bodies:
            raise ShrinkwrapError('No visible solid bodies found in this design.')
        _log(f'{len(bodies)} visible solid bodies ({hidden} hidden, {non_solid} non-solid ignored)')
        model_mass, com_point = _mass_properties(bodies)
        _log(f'Model mass {model_mass:.6g} kg')

    if not add_com:
        com_point = None
    if manual_mass is not None:
        target_mass = manual_mass
    else:
        target_mass = model_mass
        if target_mass <= 0:
            raise ShrinkwrapError('The visible bodies have zero total mass.')

    design_key = _base_name(design)
    recovered = _recover_from_freeze()
    skip = set(_load_problems().get(design_key, []))
    if skip:
        _log(f'{len(skip)} problem bodies will be skipped for "{design_key}"')

    tbm = adsk.fusion.TemporaryBRepManager.get()
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.show('Shrinkwrap', 'Uniting body %v of %m', 0, len(bodies))
    try:
        with timings.phase('Union'):
            merged, failed, skipped = _union_bodies(tbm, bodies, progress, design_key, skip)
    finally:
        progress.hide()
    if progress.wasCancelled:
        return None
    if merged is None or not merged.isSolid:
        raise ShrinkwrapError('Could not unite the bodies into a solid.')

    with timings.phase('Sealed cavities'):
        merged, cavities, cavity_warning = _fill_cavities(tbm, merged)

    envelope_info = envelope_warning = None
    if envelope:
        result = _envelope_fill(tbm, design.rootComponent, merged, _accept_non_output(),
                                detail, wall, timings)
        if result is None:
            return None
        merged, envelope_info, envelope_warning = result

    base_name = _base_name(design)
    _log(f'Creating component "{base_name} shrinkwrap" ({merged.faces.count} faces)')
    with timings.phase('Create component'):
        occ, comp, has_com = _create_output(design, merged, base_name, com_point)

    def assign(material):
        comp.material = material
        for b in comp.bRepBodies:
            b.material = material

    _log('Setting material and mass')
    with timings.phase('Material'):
        material = _material_copy(design, f'{base_name} material', None)
        mass = _tune_density(material, assign,
                             lambda: comp.getPhysicalProperties(ACCURACY).mass, target_mass)

    lines = [
        f'Created component "{comp.name}".',
        '',
        f'Bodies united: {len(bodies) - len(failed) - len(skipped)} of {len(bodies)}',
        f'Sealed cavities filled: {cavities}',
    ]
    if envelope_info:
        lines.append(f'Envelope fill: {envelope_info}')
    lines += [
        f'Original mass: {model_mass:.6g} kg',
        f'Shrinkwrap mass: {mass:.6g} kg'
        + (' (set manually)' if manual_mass is not None else ''),
        f'Material "{material.name}": {_density_g_cm3(mass, comp.bRepBodies):.6g} g/cm³',
    ]
    if com_point:
        lines.append(f'Center of mass (mm): {com_point.x * 10:.3f}, '
                     f'{com_point.y * 10:.3f}, {com_point.z * 10:.3f}')
    notes = []
    if hidden:
        notes.append(f'{hidden} hidden bodies were ignored (geometry and mass).')
    if non_solid:
        notes.append(f'{non_solid} surface/open bodies were ignored.')
    if failed:
        shown = '\n  '.join(failed[:10])
        more = f'\n  ...and {len(failed) - 10} more' if len(failed) > 10 else ''
        notes.append(f'{len(failed)} bodies could not be united and are missing '
                     f'from the shape (their mass is still counted):\n  {shown}{more}')
    if recovered:
        notes.append(f'The previous Shrinkwrap run froze Fusion while uniting '
                     f'"{recovered[1]}" ({recovered[0]}). That body is now on the skip list.')
    if skipped:
        notes.append(f'{len(skipped)} bodies were skipped because uniting them froze Fusion '
                     f'in an earlier run; they are missing from the shape but their mass '
                     f'is still counted:\n  ' + '\n  '.join(skipped) +
                     f'\n  To retry them, remove them from "{PROBLEM_FILE}".')
    if cavity_warning:
        notes.append(cavity_warning)
    if envelope_warning:
        notes.append(envelope_warning)
    if not envelope and cavities == 0 and not cavity_warning:
        notes.append('No sealed cavity was found: the inside connects to the outside '
                     'somewhere, possibly through a tiny clearance. Use Fill: '
                     f'"{FILL_ENVELOPE}" to fill it anyway.')
    if com_point and not has_com:
        notes.append('The "Center of mass" point could not be created.')
    return _finish_summary(lines, notes, timings)


def _run_refill(design, body, envelope, detail, wall, manual_mass):
    """Single-body mode: fill the body's cavities in place, keeping its mass.

    Returns a summary message, or None if cancelled.
    """
    timings = _Timings()
    tbm = adsk.fusion.TemporaryBRepManager.get()
    label = _body_label(body)
    native = body.nativeObject or body
    root = design.rootComponent
    _log(f'Single-body mode on "{label}" ({_describe_body(body)}) | '
         f'fill: {"envelope" if envelope else "sealed only"} | detail: {detail} | '
         f'wall: {wall * 10:.3g} mm | '
         f'mass: {"manual %.6g kg" % manual_mass if manual_mass is not None else "keep"}')

    with timings.phase('Mass'):
        mass_before = body.getPhysicalProperties(ACCURACY).mass
        old_material = body.material
    target_mass = manual_mass if manual_mass is not None else mass_before
    if target_mass <= 0:
        raise ShrinkwrapError(f'"{label}" has zero mass.')

    with timings.phase('Copy'):
        filled = _root_space_copy(tbm, body)
        if filled is None:
            raise ShrinkwrapError(f'Could not read the geometry of "{label}".')
        volume_before = filled.volume

    with timings.phase('Sealed cavities'):
        filled, cavities, cavity_warning = _fill_cavities(tbm, filled)

    envelope_info = envelope_warning = None
    if envelope:
        result = _envelope_fill(tbm, root, filled, _accept_only(body), detail, wall, timings)
        if result is None:
            return None
        filled, envelope_info, envelope_warning = result

    added = filled.volume - volume_before
    lines = [f'Filled cavities in "{label}" in place.', '']
    notes = [w for w in (cavity_warning, envelope_warning) if w]

    _log(f'Volume added: {added:.6g} cm^3')
    if added > MIN_ADDED_VOLUME:
        _log(f'Updating the body in the design ({filled.faces.count} faces)')
        with timings.phase('Update body'):
            new_body = _replace_body(design, body, filled)
    else:
        new_body = native
        lines[0] = f'No cavities to fill in "{label}"; its geometry is unchanged.'
        if not envelope:
            notes.append('No sealed cavity was found. Use Fill: '
                         f'"{FILL_ENVELOPE}" to fill cavities that leak to the outside.')

    _log('Setting material and mass')
    with timings.phase('Material'):
        material = _own_material(design, [new_body], old_material)
        mass = _set_mass(material, [new_body], target_mass)

    lines += [
        f'Sealed cavities filled: {cavities}',
    ]
    if envelope_info:
        lines.append(f'Envelope fill: {envelope_info}')
    lines += [
        f'Volume added: {added:.6g} cm³',
        f'Mass before: {mass_before:.6g} kg',
        f'Mass now: {mass:.6g} kg' + (' (set manually)' if manual_mass is not None else ''),
        f'Material "{material.name}": {_density_g_cm3(mass, [new_body]):.6g} g/cm³',
    ]
    return _finish_summary(lines, notes, timings)


def _finish_summary(lines, notes, timings):
    if notes:
        lines += ['', 'Notes:'] + [f'- {n}' for n in notes]
    lines += ['', f'Time: {_format_seconds(timings.total())} ({timings.summary()})']
    if _run_log:
        lines.append(_log_path_line())
    _log('Summary: ' + ' | '.join(l for l in lines if l))
    return '\n'.join(lines)


# ----------------------------------------------------------------------------
# The command
# ----------------------------------------------------------------------------

def _active_design():
    return adsk.fusion.Design.cast(_app().activeProduct)


def _quick_mass(design, single):
    """Fast (+/-1%) mass for the dialog, or None."""
    try:
        if single:
            return single.physicalProperties.mass
        bodies, _hidden, _non_solid = _collect_bodies(design)
        return sum(b.physicalProperties.mass for b in bodies)
    except Exception:
        return None


def _manual_selected(inputs):
    group = adsk.core.RadioButtonGroupCommandInput.cast(inputs.itemById(INPUT_MASS_SOURCE))
    return bool(group and group.selectedItem and group.selectedItem.name == MASS_MANUAL)


def _envelope_selected(inputs):
    fill = adsk.core.DropDownCommandInput.cast(inputs.itemById(INPUT_FILL))
    return bool(fill and fill.selectedItem and fill.selectedItem.name == FILL_ENVELOPE)


def _read_options(inputs):
    """Dialog values as (add_com, envelope, detail, wall_cm, manual_mass_kg_or_None)."""
    add_com = adsk.core.BoolValueCommandInput.cast(inputs.itemById(INPUT_COM)).value
    envelope = _envelope_selected(inputs)
    detail_input = adsk.core.DropDownCommandInput.cast(inputs.itemById(INPUT_DETAIL))
    detail = detail_input.selectedItem.name if detail_input.selectedItem else DETAIL_COARSE
    wall = adsk.core.ValueCommandInput.cast(inputs.itemById(INPUT_WALL)).value
    manual_mass = None
    if _manual_selected(inputs):
        manual_mass = adsk.core.ValueCommandInput.cast(inputs.itemById(INPUT_MASS)).value
    return add_com, envelope, detail, wall, manual_mass


class ShrinkwrapExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        ui = _app().userInterface
        log_line = ''
        try:
            add_com, envelope, detail, wall, manual_mass = \
                _read_options(args.command.commandInputs)
            _last_options['envelope'] = envelope
            _last_options['detail'] = detail
            _last_options['wall'] = wall
            _last_options['manual'] = manual_mass is not None
            if manual_mass is not None:
                _last_options['mass'] = manual_mass

            with _logged_run('Shrinkwrap'):
                log_line = _log_path_line()
                design = _active_design()
                if not design:
                    raise ShrinkwrapError('Open a design (model or assembly) first.')
                single = _find_single_body(design)
                if single:
                    summary = _run_refill(design, single, envelope, detail, wall, manual_mass)
                else:
                    _last_options['add_com'] = add_com
                    summary = _run_shrinkwrap(design, add_com, envelope, detail, wall,
                                              manual_mass)
                if summary is None:
                    _log('Cancelled by user; changes rolled back.')

            if summary is None:
                args.executeFailed = True  # cancelled: roll back
                args.executeFailedMessage = 'Shrinkwrap cancelled.'
                return
            ui.messageBox(summary, 'Shrinkwrap')
        except ShrinkwrapError as e:
            args.executeFailed = True
            ui.messageBox(f'{e}\n\n{log_line}'.strip(), 'Shrinkwrap')
        except Exception:
            args.executeFailed = True
            ui.messageBox(f'Shrinkwrap failed:\n{traceback.format_exc()}\n{log_line}'.strip(),
                          'Shrinkwrap')


class ShrinkwrapInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            inputs = args.inputs
            if args.input.id == INPUT_MASS_SOURCE:
                inputs.itemById(INPUT_MASS).isEnabled = _manual_selected(inputs)
            elif args.input.id == INPUT_FILL:
                envelope = _envelope_selected(inputs)
                inputs.itemById(INPUT_DETAIL).isEnabled = envelope
                inputs.itemById(INPUT_WALL).isEnabled = envelope
        except Exception:
            _log(traceback.format_exc())


class ShrinkwrapValidateHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            inputs = args.inputs
            valid = True
            if _manual_selected(inputs):
                mass_input = adsk.core.ValueCommandInput.cast(inputs.itemById(INPUT_MASS))
                valid = mass_input.isValidExpression and mass_input.value > 0
            if _envelope_selected(inputs):
                wall_input = adsk.core.ValueCommandInput.cast(inputs.itemById(INPUT_WALL))
                valid = valid and wall_input.isValidExpression and wall_input.value > 0
            args.areInputsValid = valid
        except Exception:
            args.areInputsValid = False


class ShrinkwrapCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            cmd = args.command
            inputs = cmd.commandInputs
            design = _active_design()
            single = _find_single_body(design) if design else None
            cmd.okButtonText = 'Fill cavities' if single else 'Shrinkwrap'

            fill = inputs.addDropDownCommandInput(
                INPUT_FILL, 'Fill', adsk.core.DropDownStyles.TextListDropDownStyle)
            fill.listItems.add(FILL_SEALED, not _last_options['envelope'])
            fill.listItems.add(FILL_ENVELOPE, _last_options['envelope'])
            fill.tooltip = 'What to fill inside the shrinkwrap.'
            fill.tooltipDescription = (
                f'{FILL_SEALED}: fills only cavities that are completely closed off. '
                'Any gap or clearance to the outside leaves the inside hollow.<br><br>'
                f'{FILL_ENVELOPE}: fills everything more than one wall thickness inside '
                'the outer skin, even with small gaps. Holes and pockets shallower than '
                'the wall thickness stay open. Slower.')

            wall = inputs.addValueInput(INPUT_WALL, 'Wall thickness', 'mm',
                                        adsk.core.ValueInput.createByReal(_last_options['wall']))
            wall.isEnabled = _last_options['envelope']
            wall.tooltip = 'How deep below the outer skin the fill starts.'
            wall.tooltipDescription = (
                'Set it to the wall thickness of the enclosure, or a little less. '
                'Too large leaves a hollow gap inside the walls; smaller is safe '
                '(the fill just overlaps the walls). Holes and pockets deeper than '
                'this get a floor at this depth.')

            detail = inputs.addDropDownCommandInput(
                INPUT_DETAIL, 'Detail', adsk.core.DropDownStyles.TextListDropDownStyle)
            for level in DETAIL_CELLS:
                detail.listItems.add(level, level == _last_options['detail'])
            detail.isEnabled = _last_options['envelope']
            detail.tooltip = 'Envelope ray grid density.'
            detail.tooltipDescription = '<br>'.join(
                f'{level}: {cells} cells along the longest side.'
                for level, cells in DETAIL_CELLS.items()) + (
                '<br><br>Each step up is about 4x the rays and time. The fill reaches '
                'the walls at any setting; finer detail only follows smaller features '
                '(e.g. gaps between protruding parts) more closely.')

            com = inputs.addBoolValueInput(INPUT_COM, 'Copy center of mass', True, '',
                                           _last_options['add_com'] and not single)
            com.isEnabled = not single

            manual = _last_options['manual']
            source = inputs.addRadioButtonGroupCommandInput(INPUT_MASS_SOURCE, 'Mass')
            source.listItems.add(MASS_FROM_MODEL, not manual)
            source.listItems.add(MASS_MANUAL, manual)

            model_mass = _quick_mass(design, single) if design else None
            initial = _last_options['mass'] or model_mass or 1.0
            mass_input = inputs.addValueInput(INPUT_MASS, 'Mass', 'kg',
                                              adsk.core.ValueInput.createByReal(initial))
            mass_input.isEnabled = manual

            if single:
                info = (f'Single body "{_body_label(single)}": its cavities will be filled '
                        'in place. "Inherit from model" keeps its current mass')
                info += f' (approx. {model_mass:.4g} kg).' if model_mass is not None else '.'
            elif model_mass is not None:
                info = (f'Model mass: approx. {model_mass:.4g} kg (visible solid bodies). '
                        'Material density is worked out from the chosen mass.')
            else:
                info = 'Material density is worked out from the chosen mass.'
            info += ('<br><b>If Fusion freezes, restart it and run Shrinkwrap again</b> '
                     '(save first). The part that caused the freeze is skipped next time.')
            inputs.addTextBoxCommandInput(INPUT_INFO, '', info, 4, True)

            for event, handler in ((cmd.execute, ShrinkwrapExecuteHandler()),
                                   (cmd.inputChanged, ShrinkwrapInputChangedHandler()),
                                   (cmd.validateInputs, ShrinkwrapValidateHandler())):
                event.add(handler)
                handlers.append(handler)
        except Exception:
            _app().userInterface.messageBox(traceback.format_exc(), 'Shrinkwrap')


# ----------------------------------------------------------------------------
# Set Mass command: give selected bodies an exact total mass by tuning the
# density of a material of their own (same method as Shrinkwrap).
# ----------------------------------------------------------------------------

SETMASS_CMD_ID = 'DaneccaSetMassCmd'
SETMASS_CMD_NAME = 'Set Mass'
SETMASS_TOOLTIP = ('Give the selected bodies an exact total mass by adjusting the density '
                   'of their material (a copy, so other bodies are not affected).')
SETMASS_BODIES = 'setmass_bodies'
SETMASS_MASS = 'setmass_mass'
SETMASS_INFO = 'setmass_info'


def _selected_bodies(inputs):
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(SETMASS_BODIES))
    return [adsk.fusion.BRepBody.cast(sel.selection(i).entity) for i in range(sel.selectionCount)]


def _current_mass(bodies):
    return sum(b.getPhysicalProperties(ACCURACY).mass for b in bodies)


def _setmass_info(bodies):
    if not bodies:
        return 'Select one or more solid bodies.'
    materials = sorted({b.material.name for b in bodies if b.material})
    return (f'Current mass: {_current_mass(bodies):.6g} kg '
            f'({len(bodies)} {"body" if len(bodies) == 1 else "bodies"}; '
            f'material: {", ".join(materials) or "none"}).')


class SetMassExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        ui = _app().userInterface
        try:
            inputs = args.command.commandInputs
            bodies = _selected_bodies(inputs)
            target = adsk.core.ValueCommandInput.cast(inputs.itemById(SETMASS_MASS)).value
            design = _active_design()
            if not design or not bodies:
                raise ShrinkwrapError('Select one or more solid bodies.')

            with _logged_run(SETMASS_CMD_NAME):
                _log(f'Target {target:.6g} kg on: ' +
                     ', '.join(f'"{_body_label(b)}"' for b in bodies))
                before = _current_mass(bodies)
                current = bodies[0].material
                material = _own_material(design, bodies, current)
                mass = _set_mass(material, bodies, target)
                natives = [b.nativeObject or b for b in bodies]
                summary = (f'Mass set on {len(bodies)} '
                           f'{"body" if len(bodies) == 1 else "bodies"}.\n\n'
                           f'Mass before: {before:.6g} kg\n'
                           f'Mass now: {mass:.6g} kg\n'
                           f'Material "{material.name}": '
                           f'{_density_g_cm3(mass, natives):.6g} g/cm³')
                _log(summary.replace('\n', ' | '))
            ui.messageBox(summary, SETMASS_CMD_NAME)
        except ShrinkwrapError as e:
            args.executeFailed = True
            ui.messageBox(str(e), SETMASS_CMD_NAME)
        except Exception:
            args.executeFailed = True
            ui.messageBox('Set Mass failed:\n{}'.format(traceback.format_exc()), SETMASS_CMD_NAME)


class SetMassInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            if args.input.id != SETMASS_BODIES:
                return
            inputs = args.inputs
            bodies = _selected_bodies(inputs)
            info = adsk.core.TextBoxCommandInput.cast(inputs.itemById(SETMASS_INFO))
            info.formattedText = _setmass_info(bodies)
        except Exception:
            _log(traceback.format_exc())


class SetMassValidateHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            inputs = args.inputs
            mass_input = adsk.core.ValueCommandInput.cast(inputs.itemById(SETMASS_MASS))
            args.areInputsValid = bool(_selected_bodies(inputs)) and \
                mass_input.isValidExpression and mass_input.value > 0
        except Exception:
            args.areInputsValid = False


class SetMassCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            cmd = args.command
            cmd.okButtonText = 'Set mass'
            inputs = cmd.commandInputs

            sel = inputs.addSelectionInput(SETMASS_BODIES, 'Bodies', 'Select solid bodies')
            sel.addSelectionFilter('SolidBodies')
            sel.setSelectionLimits(1, 0)

            # Preselect the obvious body: the only visible one (e.g. a shrinkwrap).
            design = _active_design()
            single = _find_single_body(design) if design else None
            if single:
                sel.addSelection(single)
            bodies = [single] if single else []

            initial = _current_mass(bodies) if bodies else (_last_options['mass'] or 1.0)
            inputs.addValueInput(SETMASS_MASS, 'Mass', 'kg',
                                 adsk.core.ValueInput.createByReal(initial))
            inputs.addTextBoxCommandInput(SETMASS_INFO, '', _setmass_info(bodies), 2, True)

            for event, handler in ((cmd.execute, SetMassExecuteHandler()),
                                   (cmd.inputChanged, SetMassInputChangedHandler()),
                                   (cmd.validateInputs, SetMassValidateHandler())):
                event.add(handler)
                handlers.append(handler)
        except Exception:
            _app().userInterface.messageBox(traceback.format_exc(), SETMASS_CMD_NAME)


# ----------------------------------------------------------------------------
# Updates (development guide, section 19). The network work is in
# shrinkwrap_updater; this part is the Fusion side: a "Shrinkwrap Updates"
# button, and a startup check that runs on a background thread so it never
# slows Fusion's start-up, then hands any result to the main thread through a
# custom event (the API may only be used on the main thread).
# ----------------------------------------------------------------------------

UPDATE_CMD_ID = 'DaneccaShrinkwrapUpdateCmd'
UPDATE_CMD_NAME = 'Shrinkwrap Updates'
UPDATE_TOOLTIP = 'Check GitHub for a newer version of the Shrinkwrap add-in and install it.'
UPDATE_EVENT_ID = 'DaneccaShrinkwrapUpdateFound'
UPDATE_STARTUP_DELAY = 30       # seconds after load, so Fusion has finished starting
# Remembers which new version the user was already told about at startup, so
# the startup prompt appears once per release (the button always works).
UPDATE_STATE_FILE = os.path.join(LOG_DIR, 'update check.json')


def _load_update_state():
    try:
        with open(UPDATE_STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_update_state(data):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(UPDATE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _offer_update(latest, tag, intro):
    """Ask, then download and install release `tag`. Runs on the main thread."""
    ui = _app().userInterface
    answer = ui.messageBox(
        f'{intro}\n\nInstalled: v{SHRINKWRAP_VERSION}\nLatest: v{latest}\n\n'
        'Download and install it now? Restart Fusion afterwards to start using it.',
        'Shrinkwrap update', adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        adsk.core.MessageBoxIconTypes.QuestionIconType)
    if answer != adsk.core.DialogResults.DialogYes:
        _log(f'Update to v{latest} declined')
        return
    try:
        with _logged_run('Shrinkwrap update'):
            _log(f'Installing v{latest} over v{SHRINKWRAP_VERSION}')
            updater.download_and_install(tag, updater.cached_github_token(), log=_log)
        ui.messageBox(f'Shrinkwrap v{latest} is installed.\n\nRestart Fusion to start using it.',
                      'Shrinkwrap update')
    except updater.UpdateError as e:
        ui.messageBox(f'The update could not be installed:\n\n{e}\n\n'
                      'Your current version is unchanged.', 'Shrinkwrap update')
    except Exception:
        ui.messageBox('The update could not be installed:\n\n' + traceback.format_exc() +
                      '\nYour current version is unchanged.', 'Shrinkwrap update')


class UpdateExecuteHandler(adsk.core.CommandEventHandler):
    """The "Shrinkwrap Updates" button: check now, offer to install."""

    def __init__(self):
        super().__init__()

    def notify(self, args):
        ui = _app().userInterface
        try:
            latest, tag = updater.latest_release(updater.cached_github_token())
            _log(f'Update check: installed v{SHRINKWRAP_VERSION}, latest v{latest}')
            if not updater.is_newer(latest, SHRINKWRAP_VERSION):
                ui.messageBox(f'Shrinkwrap is up to date (v{SHRINKWRAP_VERSION}).',
                              'Shrinkwrap update')
                return
            _offer_update(latest, tag, 'A newer version of Shrinkwrap is available.')
        except updater.UpdateError as e:
            ui.messageBox(f'Could not check for updates:\n\n{e}', 'Shrinkwrap update')
        except Exception:
            ui.messageBox('Could not check for updates:\n\n' + traceback.format_exc(),
                          'Shrinkwrap update')


class UpdateCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            cmd = args.command
            cmd.isAutoExecute = True  # no dialog
            on_execute = UpdateExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)
        except Exception:
            _app().userInterface.messageBox(traceback.format_exc(), UPDATE_CMD_NAME)


class UpdateFoundHandler(adsk.core.CustomEventHandler):
    """Main-thread end of the startup check: prompt once per new version."""

    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            info = json.loads(args.additionalInfo)
            latest, tag = info['latest'], info['tag']
            state = _load_update_state()
            if state.get('notified') == latest:
                _log(f'v{latest} is available (already offered at an earlier start)')
                return
            state['notified'] = latest
            _save_update_state(state)
            _offer_update(latest, tag, 'A new version of the Shrinkwrap add-in is available.')
        except Exception:
            _log('Startup update prompt failed:\n' + traceback.format_exc())


def _start_update_check():
    """Check GitHub on a background thread; never blocks or bothers on failure."""
    app = _app()

    def worker():
        try:
            time.sleep(UPDATE_STARTUP_DELAY)
            latest, tag = updater.latest_release(updater.cached_github_token())
            if updater.is_newer(latest, SHRINKWRAP_VERSION):
                app.fireCustomEvent(UPDATE_EVENT_ID, json.dumps({'latest': latest, 'tag': tag}))
        except Exception:
            pass  # offline, rate-limited, ...: stay quiet; the button still works

    threading.Thread(target=worker, name='ShrinkwrapUpdateCheck', daemon=True).start()


# ----------------------------------------------------------------------------
# Add-in entry points
# ----------------------------------------------------------------------------

COMMANDS = (
    (CMD_ID, CMD_NAME, CMD_TOOLTIP, './resources/ShrinkwrapButton', ShrinkwrapCreatedHandler),
    (SETMASS_CMD_ID, SETMASS_CMD_NAME, SETMASS_TOOLTIP, './resources/SetMassButton',
     SetMassCreatedHandler),
    (UPDATE_CMD_ID, UPDATE_CMD_NAME, UPDATE_TOOLTIP, './resources/UpdateButton',
     UpdateCreatedHandler),
)


def run(context):
    try:
        ui = _app().userInterface
        workspace = ui.workspaces.itemById(WORKSPACE_ID)
        panel = workspace.toolbarPanels.itemById(PANEL_ID) if workspace else None
        if not panel:
            _log(f'Panel {PANEL_ID} not found; buttons not added')

        for cmd_id, name, tooltip, icon, created_class in COMMANDS:
            cmd_def = ui.commandDefinitions.itemById(cmd_id)
            if not cmd_def:
                cmd_def = ui.commandDefinitions.addButtonDefinition(cmd_id, name, tooltip, icon)
            on_created = created_class()
            cmd_def.commandCreated.add(on_created)
            handlers.append(on_created)

            if panel:
                ctrl = panel.controls.itemById(cmd_id)
                if not ctrl:
                    ctrl = panel.controls.addCommand(cmd_def)
                ctrl.isVisible = True
        _log(f'v{SHRINKWRAP_VERSION} loaded')

        try:
            app = _app()
            try:
                app.unregisterCustomEvent(UPDATE_EVENT_ID)  # left over from an unclean stop
            except Exception:
                pass
            update_event = app.registerCustomEvent(UPDATE_EVENT_ID)
            on_update_found = UpdateFoundHandler()
            update_event.add(on_update_found)
            handlers.append(on_update_found)
            _start_update_check()
        except Exception:
            _log('Startup update check not started:\n' + traceback.format_exc())

    except Exception:
        _app().userInterface.messageBox(
            'Shrinkwrap failed to start:\n{}'.format(traceback.format_exc()), 'Shrinkwrap')


def stop(context):
    try:
        _app().unregisterCustomEvent(UPDATE_EVENT_ID)
    except Exception:
        pass
    try:
        ui = _app().userInterface
        workspace = ui.workspaces.itemById(WORKSPACE_ID)
        panel = workspace.toolbarPanels.itemById(PANEL_ID) if workspace else None
        for cmd_id, *_rest in COMMANDS:
            try:
                if panel:
                    ctrl = panel.controls.itemById(cmd_id)
                    if ctrl:
                        ctrl.deleteMe()
                cmd_def = ui.commandDefinitions.itemById(cmd_id)
                if cmd_def:
                    cmd_def.deleteMe()
            except Exception:
                pass
    except Exception:
        pass
    handlers.clear()
