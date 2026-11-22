# SPDX-License-Identifier: GPL-3.0-or-later
#
# Origo  -  by wilmastudio.co
#
#   Panel 1 (Object Mode) : fast origin placement (World/Local + X/Y/Z picker + quick buttons)
#   Panel 2 (Edit/Object) : 3D cursor -> selection, origin -> selection
#   Panel 3 (Object Mode) : Drop to Surface (hotkey: V)
#
# Written for Blender 5.2.  Everything here is original code.

import random
from math import cos, pi, sin, sqrt

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# --------------------------------------------------------------------------
#  Branding (change here if you want another tab / hotkey)
# --------------------------------------------------------------------------
TAB = "Origo"
DROP_KEY = "V"

ORIGIN_TYPES = {"MESH", "CURVE", "SURFACE", "FONT"}
COLLIDER_TYPES = {"MESH", "CURVE", "SURFACE", "FONT"}

# axis -> (axis icon, ((mode, label, icon), ...))   mode: MIN / CENTER / MAX
AXIS_UI = {
    "X": ("EVENT_X", (("MIN", "Left", "TRIA_LEFT"),
                      ("CENTER", "Center", "DOT"),
                      ("MAX", "Right", "TRIA_RIGHT"))),
    "Y": ("EVENT_Y", (("MIN", "Front", "TRIA_DOWN"),
                      ("CENTER", "Center", "DOT"),
                      ("MAX", "Back", "TRIA_UP"))),
    "Z": ("EVENT_Z", (("MIN", "Bottom", "TRIA_DOWN"),
                      ("CENTER", "Middle", "DOT"),
                      ("MAX", "Top", "TRIA_UP"))),
}

_MODE_ITEMS = [
    ("MIN", "Min", "Lowest value on this axis"),
    ("CENTER", "Center", "Middle of the bounds on this axis"),
    ("MAX", "Max", "Highest value on this axis"),
]


# --------------------------------------------------------------------------
#  Geometry helpers
# --------------------------------------------------------------------------
def _to_world(co, mw):
    """Nx3 local coordinates -> Nx3 world coordinates (float64)."""
    m = np.array(mw, dtype=np.float64)
    return co.astype(np.float64) @ m[:3, :3].T + m[:3, 3]


def _eval_geometry(ob, depsgraph, need_tris=True):
    """Evaluated (modifiers applied) local-space geometry of an object.

    Returns (co Nx3 float32, tris Mx3 int32 or None) or None when the object
    has no mesh-like geometry (empties, lights, cameras...).
    """
    ev = ob.evaluated_get(depsgraph)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return None
    if me is None:
        return None
    try:
        nv = len(me.vertices)
        if nv == 0:
            return None
        co = np.empty(nv * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        tris = None
        if need_tris:
            me.calc_loop_triangles()
            nt = len(me.loop_triangles)
            if nt:
                tris = np.empty(nt * 3, dtype=np.int32)
                me.loop_triangles.foreach_get("vertices", tris)
                tris = tris.reshape(-1, 3)
        return co, tris
    finally:
        ev.to_mesh_clear()


def _pick_vec(lo, hi, modes, base):
    """Per axis: MIN / CENTER / MAX of the bounds, or None = keep base[i]."""
    out = []
    for i in range(3):
        m = modes[i]
        if m is None:
            out.append(float(base[i]))
        elif m == "MIN":
            out.append(float(lo[i]))
        elif m == "MAX":
            out.append(float(hi[i]))
        else:
            out.append(float((lo[i] + hi[i]) * 0.5))
    return Vector(out)


def _targets(context):
    return [o for o in context.selected_objects if o.type in ORIGIN_TYPES]


# --------------------------------------------------------------------------
#  Origin placement
# --------------------------------------------------------------------------
def compute_origin_points(context, objs, space, modes):
    """Return {object: world-space point where its new origin should go}.

    modes = (x, y, z), each MIN / CENTER / MAX, or None to leave that axis of the
    current origin untouched.
    """
    dg = context.evaluated_depsgraph_get()
    pts = {}
    for ob in objs:
        g = _eval_geometry(ob, dg, need_tris=False)
        if g is None:
            continue
        co = g[0]
        if space == "LOCAL":
            pl = _pick_vec(co.min(0), co.max(0), modes, (0.0, 0.0, 0.0))
            pts[ob] = ob.matrix_world @ pl
        else:
            w = _to_world(co, ob.matrix_world)
            pts[ob] = _pick_vec(w.min(0), w.max(0), modes, ob.matrix_world.translation)
    return pts


def apply_origin_points(context, pts):
    """Move each object's origin to its world point (geometry stays put)."""
    scene = context.scene
    vl = context.view_layer
    prev_sel = [o for o in vl.objects if o.select_get()]
    prev_active = vl.objects.active
    prev_cursor = scene.cursor.location.copy()
    done, failed = 0, 0
    try:
        for o in prev_sel:
            o.select_set(False)
        for ob, pt in pts.items():
            ob.select_set(True)
            vl.objects.active = ob
            scene.cursor.location = pt
            try:
                bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
                done += 1
            except RuntimeError:
                failed += 1
            ob.select_set(False)
    finally:
        for o in prev_sel:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        vl.objects.active = prev_active
        scene.cursor.location = prev_cursor
    return done, failed


def run_origin(op, context, modes):
    p = context.scene.origo
    objs = _targets(context)
    if not objs:
        op.report({"WARNING"}, "Select a mesh / curve / text object first")
        return {"CANCELLED"}
    pts = compute_origin_points(context, objs, p.space, modes)
    if not pts:
        op.report({"WARNING"}, "Selected objects have no geometry")
        return {"CANCELLED"}
    done, failed = apply_origin_points(context, pts)
    if failed:
        op.report({"WARNING"}, f"Origin moved on {done}, failed on {failed} (linked data?)")
    return {"FINISHED"} if done else {"CANCELLED"}


# --------------------------------------------------------------------------
#  Live randomize (seeded, non-destructive transform jitter)
# --------------------------------------------------------------------------
_random_base = {}  # object name -> (base matrix, last applied matrix)


def _mat_close(a, b, tol=1e-9):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(4) for j in range(4))


def _randomize_apply(context):
    """Re-place the selected objects with a seeded random transform, live.

    A per-object base transform is remembered the first time an object is
    touched; every later seed / toggle change re-randomizes FROM that base,
    so the effect never accumulates and stays reproducible per seed.
    """
    if context.mode != "OBJECT":
        return
    sel = list(context.selected_objects)
    if not sel:
        return
    s = context.scene.origo_drop
    do_loc, do_rot, do_scale = s.rnd_loc, s.rnd_rot, s.rnd_scale
    if not (do_loc or do_rot or do_scale):
        return

    # footprint radius from the combined world bounding box of the selection
    pts = []
    for o in sel:
        mw = o.matrix_world
        pts.extend(mw @ Vector(c) for c in o.bound_box)
    radius = 0.0
    if pts:
        dx = max(p.x for p in pts) - min(p.x for p in pts)
        dy = max(p.y for p in pts) - min(p.y for p in pts)
        radius = 0.5 * max(dx, dy)

    for o in sel:
        cur = o.matrix_world.copy()
        rec = _random_base.get(o.name)
        if rec is None or not _mat_close(cur, rec[1]):
            # never touched yet, or moved/undone by hand -> take a new base
            rec = (cur.copy(), cur.copy())
        base = rec[0]
        rng = random.Random(f"{s.seed}|{o.name}")

        m = base.copy()
        if do_rot:
            a = rng.uniform(0.0, 2.0 * pi)
            piv = base.translation
            m = (Matrix.Translation(piv) @ Matrix.Rotation(a, 4, "Z")
                 @ Matrix.Translation(-piv)) @ m
        if do_loc and radius > 0.0:
            ang = rng.uniform(0.0, 2.0 * pi)
            r = radius * sqrt(rng.random())
            m = Matrix.Translation((cos(ang) * r, sin(ang) * r, 0.0)) @ m
        if do_scale:
            f = rng.uniform(0.7, 1.3)
            m = m @ Matrix.Scale(f, 4)
        o.matrix_world = m
        _random_base[o.name] = (base, m.copy())


def _on_random_update(self, context):
    _randomize_apply(context)


# --------------------------------------------------------------------------
#  Properties
# --------------------------------------------------------------------------
class ORIGO_Props(PropertyGroup):
    space: EnumProperty(
        name="Orientation",
        description="How the new origin position is calculated",
        items=[
            ("WORLD", "World", "Use the world axes (the real top / bottom of the object in the scene)",
             "ORIENTATION_GLOBAL", 0),
            ("LOCAL", "Local", "Use the object's own axes (follows its rotation)",
             "ORIENTATION_LOCAL", 1),
        ],
        default="WORLD",
    )
    cursor_mode: EnumProperty(
        name="Cursor goes to",
        items=[
            ("MEDIAN", "Median", "Average position of the selected elements", "PIVOT_MEDIAN", 0),
            ("BOUNDS", "Bounds", "Center of the selection's bounding box", "PIVOT_BOUNDBOX", 1),
            ("ACTIVE", "Active", "The active element", "PIVOT_ACTIVE", 2),
        ],
        default="MEDIAN",
    )
    # last applied position per axis (drives the lit button in the panel)
    axis_x: EnumProperty(
        name="X State",
        items=[("NONE", "", ""), ("MIN", "", ""), ("CENTER", "", ""), ("MAX", "", "")],
        default="NONE",
    )
    axis_y: EnumProperty(
        name="Y State",
        items=[("NONE", "", ""), ("MIN", "", ""), ("CENTER", "", ""), ("MAX", "", "")],
        default="NONE",
    )
    axis_z: EnumProperty(
        name="Z State",
        items=[("NONE", "", ""), ("MIN", "", ""), ("CENTER", "", ""), ("MAX", "", "")],
        default="NONE",
    )


class ORIGO_DropProps(PropertyGroup):
    target: EnumProperty(
        name="Drop to",
        items=[
            ("SURFACE", "Surface", "Land on the visible objects below (collision)", "SNAP_FACE", 0),
            ("GROUND", "Ground", "Land on a flat ground plane (world Z)", "ANCHOR_BOTTOM", 1),
        ],
        default="SURFACE",
    )
    surface_object: PointerProperty(
        name="Only on",
        description="Land only on this object. Empty = every visible object",
        type=bpy.types.Object,
        poll=lambda self, ob: ob.type in COLLIDER_TYPES,
    )
    ground_z: FloatProperty(name="Ground Z", unit="LENGTH", default=0.0)
    method: EnumProperty(
        name="Contact",
        description="Which part of the object touches the surface",
        items=[
            ("MESH", "Mesh Contact", "Accurate: the real shape touches the surface (best quality)"),
            ("BOUNDS", "Bounding Box", "Fast: the bottom of the bounding box touches the surface"),
            ("ORIGIN", "Origin", "The object's origin lands on the surface"),
        ],
        default="MESH",
    )
    collide_selection: BoolProperty(
        name="Collide with Selection",
        description="Selected objects land on each other (lowest first)",
        default=False,
    )
    affect_children: BoolProperty(
        name="Affect Selected Children",
        description="Also drop children that are selected together with their parent. "
                    "When off, only the top parents drop and children simply follow",
        default=False,
    )
    align_normal: BoolProperty(
        name="Align to Surface Normal",
        description="Tilt the object to match the slope of the surface (keeps its heading)",
        default=False,
    )
    allow_up: BoolProperty(
        name="Lift Objects Below Surface",
        description="Objects that are inside/below the surface are moved up onto it",
        default=True,
    )
    offset: FloatProperty(
        name="Offset Z", description="Extra height above the surface",
        unit="LENGTH", default=0.0,
    )
    random_rotation: FloatProperty(
        name="Random Rotation",
        description="Random spin around the up axis (+/- this angle)",
        subtype="ANGLE", unit="ROTATION", default=0.0, min=0.0, max=2 * pi,
    )
    random_location: FloatProperty(
        name="Random Location",
        description="Random sideways shift (radius) before dropping",
        unit="LENGTH", default=0.0, min=0.0,
    )
    rnd_loc: BoolProperty(
        name="Location",
        description="Randomize the XY position of the selected objects (live preview). "
                    "Press Drop afterwards to re-land them",
        default=True,
        update=_on_random_update,
    )
    rnd_rot: BoolProperty(
        name="Rotation",
        description="Randomize each object's spin around its own vertical axis (live preview)",
        default=True,
        update=_on_random_update,
    )
    rnd_scale: BoolProperty(
        name="Scale",
        description="Randomize the uniform scale of each object between 70% and 130% (live preview)",
        default=False,
        update=_on_random_update,
    )
    seed: IntProperty(
        name="Seed",
        description="Change it live: every value produces a different random placement",
        default=1, min=0, update=_on_random_update,
    )
    samples: IntProperty(
        name="Samples",
        description="Contact points tested per object (higher = more accurate but slower)",
        default=200, min=8, max=4000,
    )


# --------------------------------------------------------------------------
#  Panel 1 operators
# --------------------------------------------------------------------------
class ORIGO_OT_set_axis(Operator):
    bl_idname = "origo.set_axis"
    bl_label = "Origin Position"
    bl_description = "Move the origin along this axis only (the other two axes stay where they are)"
    bl_options = {"REGISTER", "UNDO"}

    axis: EnumProperty(items=[("X", "X", ""), ("Y", "Y", ""), ("Z", "Z", "")])
    mode: EnumProperty(items=_MODE_ITEMS)

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(context.selected_objects)

    def execute(self, context):
        modes = [None, None, None]
        modes["XYZ".index(self.axis)] = self.mode
        res = run_origin(self, context, tuple(modes))
        if "FINISHED" in res:
            setattr(context.scene.origo, "axis_" + self.axis.lower(), self.mode)
        return res


class ORIGO_OT_quick(Operator):
    bl_idname = "origo.quick"
    bl_label = "Quick Origin"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(items=[
        ("CENTER", "Center", "Origin to the center of the bounds"),
        ("GROUND", "Ground", "Origin to the bottom-center of the object"),
        ("TOP", "Top", "Origin to the top-center of the object"),
        ("MEDIAN", "Median", "Origin to the median point of the geometry"),
        ("CURSOR", "3D Cursor", "Origin to the 3D cursor"),
        ("WORLD", "World Origin", "Origin to the world origin (0, 0, 0)"),
    ])

    @classmethod
    def description(cls, context, properties):
        return {
            "CENTER": "Origin to the center of the bounds (follows World / Local above)",
            "GROUND": "Origin to the bottom-center of the object (follows World / Local above)",
            "TOP": "Origin to the top-center of the object (follows World / Local above)",
            "MEDIAN": "Origin to the median point of the geometry",
            "CURSOR": "Origin to the 3D cursor",
            "WORLD": "Origin to the world origin (0, 0, 0)",
        }.get(properties.kind, "")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(context.selected_objects)

    def execute(self, context):
        kind = self.kind
        p = context.scene.origo
        if kind == "CENTER":
            res = run_origin(self, context, ("CENTER", "CENTER", "CENTER"))
            state = ("CENTER", "CENTER", "CENTER")
        elif kind == "GROUND":
            res = run_origin(self, context, ("CENTER", "CENTER", "MIN"))
            state = ("CENTER", "CENTER", "MIN")
        elif kind == "TOP":
            res = run_origin(self, context, ("CENTER", "CENTER", "MAX"))
            state = ("CENTER", "CENTER", "MAX")
        else:
            res = None
            state = ("NONE", "NONE", "NONE")
        if res is not None:
            if "FINISHED" not in res:
                return res
        else:
            try:
                if kind == "MEDIAN":
                    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="MEDIAN")
                elif kind == "CURSOR":
                    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
                else:
                    cur = context.scene.cursor.location.copy()
                    context.scene.cursor.location = (0.0, 0.0, 0.0)
                    try:
                        bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
                    finally:
                        context.scene.cursor.location = cur
            except RuntimeError as e:
                self.report({"WARNING"}, str(e).strip())
                return {"CANCELLED"}
        p.axis_x, p.axis_y, p.axis_z = state
        return {"FINISHED"}


# --------------------------------------------------------------------------
#  Panel 2 operators (Edit Mode)
# --------------------------------------------------------------------------
def _snap_cursor(context, mode):
    ts = context.scene.tool_settings
    if mode == "ACTIVE":
        return bpy.ops.view3d.snap_cursor_to_active()
    prev = ts.transform_pivot_point
    ts.transform_pivot_point = "BOUNDING_BOX_CENTER" if mode == "BOUNDS" else "MEDIAN_POINT"
    try:
        return bpy.ops.view3d.snap_cursor_to_selected()
    finally:
        ts.transform_pivot_point = prev


class ORIGO_OT_cursor_to_selection(Operator):
    bl_idname = "origo.cursor_to_selection"
    bl_label = "3D Cursor to Selection"
    bl_description = "Move the 3D cursor to whatever is selected (vertices / edges / faces in Edit Mode, objects in Object Mode)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" or context.mode.startswith("EDIT")

    def execute(self, context):
        res = _snap_cursor(context, context.scene.origo.cursor_mode)
        if "FINISHED" not in res:
            self.report({"WARNING"}, "Nothing selected")
            return {"CANCELLED"}
        return {"FINISHED"}


class ORIGO_OT_origin_to_selection(Operator):
    bl_idname = "origo.origin_to_selection"
    bl_label = "Origin to Selection"
    bl_description = "Set the object's origin to the selected vertices / edges / faces, then stay in Edit Mode"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH"

    def execute(self, context):
        scene = context.scene
        vl = context.view_layer
        edit_objs = list(context.objects_in_mode_unique_data)
        if not edit_objs:
            return {"CANCELLED"}
        prev_cursor = scene.cursor.location.copy()
        if "FINISHED" not in _snap_cursor(context, scene.origo.cursor_mode):
            self.report({"WARNING"}, "Nothing selected")
            return {"CANCELLED"}
        prev_active = vl.objects.active
        prev_sel = {o: o.select_get() for o in vl.objects}
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
            for o in vl.objects:
                o.select_set(o in edit_objs)
            vl.objects.active = prev_active if prev_active in edit_objs else edit_objs[0]
            bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
            bpy.ops.object.mode_set(mode="EDIT")
        finally:
            for o, s in prev_sel.items():
                if o not in edit_objs:
                    try:
                        o.select_set(s)
                    except RuntimeError:
                        pass
            scene.cursor.location = prev_cursor
        return {"FINISHED"}


# --------------------------------------------------------------------------
#  Drop to Surface - core (independent from bpy objects, easy to test)
# --------------------------------------------------------------------------
class Collider:
    """A mesh in the scene: BVH in local space + its world matrix."""

    def __init__(self, ob, co, tris, mw):
        self.ob = ob
        self.co = co
        self.bvh = BVHTree.FromPolygons(co.tolist(), tris.tolist(), all_triangles=True)
        self.set_matrix(mw)

    def set_matrix(self, mw):
        self.mw = mw.copy()
        self.inv = self.mw.inverted_safe()
        self.inv3 = self.inv.to_3x3()
        self.nmat = self.inv3.transposed()
        self._wco = None
        self._bb = None

    def world_co(self):
        if self._wco is None:
            self._wco = _to_world(self.co, self.mw)
        return self._wco

    def bounds(self):
        if self._bb is None:
            w = self.world_co()
            self._bb = (w.min(0), w.max(0))
        return self._bb

    def ray(self, origin, direction):
        o = self.inv @ origin
        d = self.inv3 @ direction
        if d.length == 0.0:
            return None
        d.normalize()
        loc, nor, _idx, _dist = self.bvh.ray_cast(o, d)
        if loc is None:
            return None
        return self.mw @ loc, (self.nmat @ nor).normalized()


_DOWN = Vector((0.0, 0.0, -1.0))
_UP = Vector((0.0, 0.0, 1.0))


def _cast_down(cols, x, y, z_start):
    """Highest surface under (x, y) starting from z_start going down."""
    best = None
    o = Vector((x, y, z_start))
    for c in cols:
        lo, hi = c.bounds()
        if x < lo[0] or x > hi[0] or y < lo[1] or y > hi[1]:
            continue
        r = c.ray(o, _DOWN)
        if r is not None and (best is None or r[0].z > best[0].z):
            best = r
    return best


def _rot_about(pivot, rot4):
    return Matrix.Translation(pivot) @ rot4 @ Matrix.Translation(-pivot)


def _lowest_samples(w, n):
    """Pick up to n vertices: the lowest ones + an even spread over the rest."""
    cnt = len(w)
    if cnt <= n:
        return w
    k = n // 2
    low = np.argpartition(w[:, 2], k)[:k]
    spread = np.linspace(0, cnt - 1, n - k).astype(np.int64)
    return w[np.unique(np.concatenate([low, spread]))]


def _footprint_normal(cols, lo, hi, top_z):
    """Average surface normal under the object's footprint (5 rays)."""
    cx, cy = (lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5
    ix, iy = (hi[0] - lo[0]) * 0.35, (hi[1] - lo[1]) * 0.35
    pts = [(cx, cy), (cx - ix, cy - iy), (cx + ix, cy - iy), (cx - ix, cy + iy), (cx + ix, cy + iy)]
    acc = Vector((0.0, 0.0, 0.0))
    hits = 0
    for x, y in pts:
        r = _cast_down(cols, x, y, top_z)
        if r is None:
            continue
        n = r[1]
        if n.z < 0:
            n = -n
        acc += n
        hits += 1
    if not hits or acc.length < 1e-6:
        return None
    return acc.normalized()


def solve_drop(co, tris, mw, cols, s, rng, own=None):
    """Compute the new world matrix of one object.

    co/tris  : local geometry of the object (or None for empties/lights)
    cols     : list of Collider (surfaces)
    s        : settings (ORIGO_DropProps or anything with the same attributes)
    own      : Collider of the object itself (needed for 'MESH' contact), optional
    Returns the new matrix, or None when no surface was found.
    """
    mw = mw.copy()
    has_geo = co is not None
    method = s.method if has_geo else "ORIGIN"
    surface = s.target == "SURFACE"

    def world():
        if has_geo:
            return _to_world(co, mw)
        t = mw.translation
        return np.array([[t.x, t.y, t.z]], dtype=np.float64)

    w = world()
    lo, hi = w.min(0), w.max(0)

    # 1) random sideways shift
    if s.random_location > 0.0:
        ang = rng.uniform(0.0, 2.0 * pi)
        r = s.random_location * sqrt(rng.random())
        mw = Matrix.Translation((cos(ang) * r, sin(ang) * r, 0.0)) @ mw
        w = world()
        lo, hi = w.min(0), w.max(0)

    def pivot():
        return Vector(((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, lo[2]))

    up_axis = _UP

    # 2) align to surface (keeps the heading: smallest possible rotation)
    if s.align_normal and surface and cols:
        n = _footprint_normal(cols, lo, hi, hi[2] + 1e-4)
        if n is not None:
            cur_up = (mw.to_3x3() @ _UP).normalized()
            rot = cur_up.rotation_difference(n).to_matrix().to_4x4()
            mw = _rot_about(pivot(), rot) @ mw
            up_axis = n
            w = world()
            lo, hi = w.min(0), w.max(0)

    # 3) random spin around the up axis
    if s.random_rotation > 0.0:
        a = rng.uniform(-1.0, 1.0) * s.random_rotation
        mw = _rot_about(pivot(), Matrix.Rotation(a, 4, up_axis)) @ mw
        w = world()
        lo, hi = w.min(0), w.max(0)

    # 4) contact samples
    if method == "ORIGIN":
        t = mw.translation
        samples = np.array([[t.x, t.y, t.z]], dtype=np.float64)
    elif method == "BOUNDS":
        xs = (lo[0], (lo[0] + hi[0]) * 0.5, hi[0])
        ys = (lo[1], (lo[1] + hi[1]) * 0.5, hi[1])
        samples = np.array([[x, y, lo[2]] for x in xs for y in ys], dtype=np.float64)
    else:
        samples = _lowest_samples(w, s.samples)

    # 5) how far do we have to move down?
    if not surface:
        gap = float(samples[:, 2].min() - s.ground_z)
    else:
        top_z = max(float(hi[2]), float(samples[:, 2].max())) + 1e-4
        cands = []
        for p in samples:
            r = _cast_down(cols, p[0], p[1], top_z)
            if r is not None:
                cands.append(float(p[2] - r[0].z))

        # surface vertices poking up into the object (peaks, edges)
        if method == "MESH" and own is not None and cols:
            own.set_matrix(mw)
            start_z = float(lo[2]) - 1e-4
            for c in cols:
                clo, chi = c.bounds()
                if chi[0] < lo[0] or clo[0] > hi[0] or chi[1] < lo[1] or clo[1] > hi[1]:
                    continue
                cw = c.world_co()
                m = ((cw[:, 0] >= lo[0]) & (cw[:, 0] <= hi[0]) &
                     (cw[:, 1] >= lo[1]) & (cw[:, 1] <= hi[1]) &
                     (cw[:, 2] <= hi[2] + 1e-6))
                v = cw[m]
                if len(v) > s.samples:
                    v = v[np.linspace(0, len(v) - 1, s.samples).astype(np.int64)]
                for q in v:
                    r = own.ray(Vector((q[0], q[1], start_z)), _UP)
                    if r is not None:
                        cands.append(float(r[0].z - q[2]))
        if not cands:
            return None
        gap = min(cands)

    if gap < 0.0 and not s.allow_up:
        gap = 0.0
    return Matrix.Translation((0.0, 0.0, -gap + s.offset)) @ mw


# --------------------------------------------------------------------------
#  Drop to Surface - operator
# --------------------------------------------------------------------------
def _depth(ob):
    d, p = 0, ob.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _has_selected_ancestor(ob, selset):
    p = ob.parent
    while p is not None:
        if p in selset:
            return True
        p = p.parent
    return False


class ORIGO_OT_drop(Operator):
    bl_idname = "origo.drop"
    bl_label = "Drop to Surface"
    bl_description = "Drop the selected objects onto the ground / surface below them"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(context.selected_objects)

    def execute(self, context):
        s = context.scene.origo_drop
        vl = context.view_layer
        dg = context.evaluated_depsgraph_get()
        sel = list(context.selected_objects)
        selset = set(sel)

        if s.affect_children:
            targets = list(sel)
        else:
            targets = [o for o in sel if not _has_selected_ancestor(o, selset)]
        if s.surface_object is not None:
            targets = [o for o in targets if o != s.surface_object]
        if not targets:
            self.report({"WARNING"}, "Nothing to drop")
            return {"CANCELLED"}

        movers = set(targets)
        for o in targets:
            movers.update(o.children_recursive)

        surface = s.target == "SURFACE"
        need_tris = surface and (s.method == "MESH" or s.collide_selection)
        geo = {o: _eval_geometry(o, dg, need_tris=need_tris) for o in targets}

        def low_z(o):
            g = geo[o]
            if g is None:
                return o.matrix_world.translation.z
            return float(_to_world(g[0], o.matrix_world)[:, 2].min())

        targets.sort(key=lambda o: (_depth(o), low_z(o)))

        # --- surfaces -------------------------------------------------------
        cols = []
        if surface:
            f_lo = np.array([1e30, 1e30])
            f_hi = np.array([-1e30, -1e30])
            for o in targets:
                g = geo[o]
                if g is None:
                    t = o.matrix_world.translation
                    pts = np.array([[t.x, t.y, t.z]])
                else:
                    pts = _to_world(g[0], o.matrix_world)
                f_lo = np.minimum(f_lo, pts[:, :2].min(0))
                f_hi = np.maximum(f_hi, pts[:, :2].max(0))
            f_lo -= s.random_location + 1e-3
            f_hi += s.random_location + 1e-3

            for ob in vl.objects:
                if ob in movers or ob.type not in COLLIDER_TYPES or not ob.visible_get():
                    continue
                if s.surface_object is not None and ob != s.surface_object:
                    continue
                g = _eval_geometry(ob, dg, need_tris=True)
                if g is None or g[1] is None:
                    continue
                w = _to_world(g[0], ob.matrix_world)
                if (w[:, 0].max() < f_lo[0] or w[:, 0].min() > f_hi[0] or
                        w[:, 1].max() < f_lo[1] or w[:, 1].min() > f_hi[1]):
                    continue
                cols.append(Collider(ob, g[0], g[1], ob.matrix_world))

            if not cols:
                self.report({"WARNING"}, "No surface found below the selection")
                return {"CANCELLED"}

        # --- drop -----------------------------------------------------------
        rng = random.Random(s.seed)
        moved, skipped = 0, []
        for o in targets:
            g = geo[o]
            co = tris = None
            if g is not None:
                co, tris = g
            mw = o.matrix_world.copy()
            own = None
            if surface and tris is not None and (s.method == "MESH" or s.collide_selection):
                own = Collider(o, co, tris, mw)
            new = solve_drop(co, tris, mw, cols, s, rng, own)
            if new is None:
                skipped.append(o.name)
                continue
            o.matrix_world = new
            moved += 1
            if o.children:
                vl.update()
            if surface and s.collide_selection and own is not None:
                own.set_matrix(new)
                cols.append(own)

        if skipped:
            self.report({"WARNING"}, f"No surface under: {', '.join(skipped[:4])}"
                                     f"{'...' if len(skipped) > 4 else ''}")
        if not moved:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Dropped {moved} object(s)")
        return {"FINISHED"}


# --------------------------------------------------------------------------
#  Randomize reset
# --------------------------------------------------------------------------
class ORIGO_OT_random_reset(Operator):
    bl_idname = "origo.random_reset"
    bl_label = "Reset Randomize"
    bl_description = "Restore every object to the transform it had before the live randomize touched it"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(_random_base)

    def execute(self, context):
        restored = 0
        for o in context.view_layer.objects:
            rec = _random_base.get(o.name)
            if rec is None:
                continue
            try:
                o.matrix_world = rec[0]
                restored += 1
            except RuntimeError:
                pass
        _random_base.clear()
        self.report({"INFO"}, f"Restored {restored} object(s)")
        return {"FINISHED"}


# --------------------------------------------------------------------------
#  UI
# --------------------------------------------------------------------------
STUDIO = "wilmastudio"
SITE_URL = "https://wilmastudio.co"
SITE_TEXT = "wilmastudio.co"


def draw_credit(layout):
    col = layout.column(align=True)
    col.label(text=f"Made by {STUDIO}", icon="HEART")
    op = col.operator("wm.url_open", text=SITE_TEXT, icon="URL")
    op.url = SITE_URL


class _Base:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = TAB


class ORIGO_PT_origin(_Base, Panel):
    bl_idname = "ORIGO_PT_origin"
    bl_label = "Origin"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw_header(self, context):
        self.layout.label(text="", icon="OBJECT_ORIGIN")

    def draw(self, context):
        layout = self.layout
        p = context.scene.origo

        if not context.selected_objects:
            layout.label(text="Select an object", icon="INFO")

        row = layout.row(align=True)
        row.scale_y = 1.5
        row.prop(p, "space", expand=True)

        layout.separator(factor=0.4)

        col = layout.column(align=True)
        for axis in ("X", "Y", "Z"):
            axis_icon, items = AXIS_UI[axis]
            split = col.split(factor=0.16, align=True)
            lab = split.row()
            lab.scale_y = 1.45
            lab.label(text="", icon=axis_icon)
            btns = split.row(align=True)
            btns.scale_y = 1.45
            current = getattr(p, "axis_" + axis.lower())
            for mode, label, icon in items:
                op = btns.operator("origo.set_axis", text=label, icon=icon,
                                   depress=(current == mode))
                op.axis = axis
                op.mode = mode
            if axis != "Z":
                col.separator(factor=0.35)


class ORIGO_PT_origin_quick(_Base, Panel):
    bl_idname = "ORIGO_PT_origin_quick"
    bl_label = "Quick"
    bl_parent_id = "ORIGO_PT_origin"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        grid = self.layout.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        grid.scale_y = 1.35
        for kind, label, icon in (
            ("CENTER", "Center", "ANCHOR_CENTER"),
            ("GROUND", "Ground", "ANCHOR_BOTTOM"),
            ("TOP", "Top", "ANCHOR_TOP"),
            ("MEDIAN", "Median", "PIVOT_MEDIAN"),
            ("CURSOR", "3D Cursor", "PIVOT_CURSOR"),
            ("WORLD", "World 0,0,0", "WORLD_DATA"),
        ):
            grid.operator("origo.quick", text=label, icon=icon).kind = kind


class ORIGO_PT_cursor(_Base, Panel):
    bl_idname = "ORIGO_PT_cursor"
    bl_label = "3D Cursor"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" or context.mode.startswith("EDIT")

    def draw_header(self, context):
        self.layout.label(text="", icon="CURSOR")

    def draw(self, context):
        layout = self.layout
        p = context.scene.origo

        row = layout.row()
        row.scale_y = 2.6
        row.operator("origo.cursor_to_selection", text="Cursor to Selection", icon="CURSOR")

        row = layout.row(align=True)
        row.prop(p, "cursor_mode", expand=True)

        layout.separator(factor=0.4)
        col = layout.column(align=True)
        col.scale_y = 1.25
        if context.mode == "EDIT_MESH":
            col.operator("origo.origin_to_selection", icon="OBJECT_ORIGIN")
        col.operator("view3d.snap_cursor_to_center", text="Cursor to World Center", icon="WORLD_DATA")


class ORIGO_PT_drop(_Base, Panel):
    bl_idname = "ORIGO_PT_drop"
    bl_label = "Drop to Surface"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw_header(self, context):
        self.layout.label(text="", icon="TRIA_DOWN_BAR")

    def draw(self, context):
        layout = self.layout
        s = context.scene.origo_drop

        row = layout.row()
        row.scale_y = 1.8
        row.operator("origo.drop", text=f"Drop   ({DROP_KEY})", icon="TRIA_DOWN_BAR")

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop(s, "target", expand=True)

        col = layout.column(align=True)
        if s.target == "SURFACE":
            col.prop(s, "align_normal")
            col.prop(s, "collide_selection")
        else:
            col.prop(s, "ground_z")


class ORIGO_PT_drop_more(_Base, Panel):
    bl_idname = "ORIGO_PT_drop_more"
    bl_label = "More Options"
    bl_parent_id = "ORIGO_PT_drop"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return (context.mode == "OBJECT"
                and context.scene.origo_drop.target == "SURFACE")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        s = context.scene.origo_drop
        layout.prop(s, "surface_object")
        layout.prop(s, "method")


class ORIGO_PT_drop_random(_Base, Panel):
    bl_idname = "ORIGO_PT_drop_random"
    bl_label = "Randomize"
    bl_parent_id = "ORIGO_PT_drop"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        s = context.scene.origo_drop

        col = layout.column(align=True)
        col.prop(s, "rnd_loc")
        col.prop(s, "rnd_rot")
        col.prop(s, "rnd_scale")

        layout.prop(s, "seed")

        row = layout.row()
        row.operator("origo.random_reset", text="Reset", icon="LOOP_BACK")


class ORIGO_PT_about(_Base, Panel):
    bl_idname = "ORIGO_PT_about"
    bl_label = "About"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        draw_credit(self.layout)


class ORIGO_Prefs(AddonPreferences):
    bl_idname = __package__ or "origo"

    def draw(self, context):
        draw_credit(self.layout)


# --------------------------------------------------------------------------
#  Registration
# --------------------------------------------------------------------------
classes = (
    ORIGO_Props,
    ORIGO_DropProps,
    ORIGO_OT_set_axis,
    ORIGO_OT_quick,
    ORIGO_OT_cursor_to_selection,
    ORIGO_OT_origin_to_selection,
    ORIGO_OT_drop,
    ORIGO_OT_random_reset,
    ORIGO_PT_origin,
    ORIGO_PT_origin_quick,
    ORIGO_PT_cursor,
    ORIGO_PT_drop,
    ORIGO_PT_drop_more,
    ORIGO_PT_drop_random,
    ORIGO_PT_about,
    ORIGO_Prefs,
)

_keymaps = []


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.origo = PointerProperty(type=ORIGO_Props)
    bpy.types.Scene.origo_drop = PointerProperty(type=ORIGO_DropProps)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Object Mode", space_type="EMPTY")
        kmi = km.keymap_items.new("origo.drop", DROP_KEY, "PRESS")
        _keymaps.append((km, kmi))


def unregister():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()

    del bpy.types.Scene.origo_drop
    del bpy.types.Scene.origo
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
