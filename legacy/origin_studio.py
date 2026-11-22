# SPDX-License-Identifier: GPL-3.0-or-later
#
# Origin Studio
# Copyright (C) 2026 Z.ai Studio
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Origin Studio - quick origin & 3D-cursor tools for Blender 4.2+ (incl. 5.2 LTS).

Sidebar: 3D Viewport > N-panel > "Origin Studio"

Tab 1 (Object Mode):
    * World / Local coordinate space
    * Per-axis origin placement: Top / Mid / Down (instant, one undo step)
    * Quick presets: Ground (bottom-center) and Center
    * Works on every selected Mesh / Curve / Surface / Text object
    * Objects never jump visually, and their children stay in place

Tab 2 (Edit Mode):
    * Big "Cursor -> Selection" button (median of the selected geometry)
    * Bonus: cursor to the bounding-box center of the selection
"""

bl_info = {
    "name": "Origin Studio",
    "author": "Z.ai Studio",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Origin Studio",
    "description": (
        "Per-axis origin placement (Top/Mid/Down in World or Local space) "
        "and a big Edit-Mode cursor-to-selection button"
    ),
    "category": "Object",
}

import bmesh
import bpy
from bpy.props import EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix, Vector

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

GEO_TYPES = {'MESH', 'CURVE', 'SURFACE', 'FONT'}
AXIS_INDEX = {'X': 0, 'Y': 1, 'Z': 2}


def _axis_value(position, lo, hi):
    """Resolve a MAX / CENTER / MIN request on one axis to a scalar."""
    if position == 'MIN':
        return lo
    if position == 'MAX':
        return hi
    return (lo + hi) * 0.5


def _bbox_corners(obj, space):
    """Eight bounding-box corners of `obj` in the requested space."""
    mw = obj.matrix_world
    if space == 'LOCAL':
        return [Vector(c) for c in obj.bound_box]
    return [mw @ Vector(c) for c in obj.bound_box]


def _bbox_min_max(corners):
    """Return (mins, maxs) Vectors computed from eight corners."""
    mins = Vector((
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        min(c[2] for c in corners),
    ))
    maxs = Vector((
        max(c[0] for c in corners),
        max(c[1] for c in corners),
        max(c[2] for c in corners),
    ))
    return mins, maxs


def _move_origin(obj, world_target):
    """Move `obj`'s origin to `world_target` without changing the visuals.

    The object data is shifted so the geometry stays exactly where it is,
    direct children are compensated so they never jump, and multi-user
    data is made single-user first to avoid touching other users.
    """
    if obj.data.users > 1:
        obj.data = obj.data.copy()

    mw = obj.matrix_world.copy()
    t_local = Matrix.Translation(mw.inverted_safe() @ world_target)

    obj.data.transform(t_local.inverted())
    obj.matrix_world = mw @ t_local

    # Keep every direct child exactly where it was.
    t_inv = t_local.inverted()
    for child in obj.children:
        child.matrix_parent_inverse = t_inv @ child.matrix_parent_inverse.copy()


def _world_target_for(obj, space, plan):
    """Compute the world-space origin target for a {axis_index: position} plan."""
    mw = obj.matrix_world
    if space == 'LOCAL':
        mins, maxs = _bbox_min_max(_bbox_corners(obj, 'LOCAL'))
        local = Vector((0.0, 0.0, 0.0))
        for i, pos in plan.items():
            local[i] = _axis_value(pos, mins[i], maxs[i])
        return mw @ local

    mins, maxs = _bbox_min_max(_bbox_corners(obj, 'WORLD'))
    world = mw.to_translation()
    for i, pos in plan.items():
        world[i] = _axis_value(pos, mins[i], maxs[i])
    return world


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class OS_PG_settings(PropertyGroup):
    """Scene-level settings for Origin Studio."""

    space: EnumProperty(
        name="Space",
        description=(
            "Coordinate space used by the Top/Mid/Down buttons\n"
            "World: global axis-aligned bounding box\n"
            "Local: the object's own axes"
        ),
        items=[
            ('WORLD', "World", "Place the origin using the World-space bounding box"),
            ('LOCAL', "Local", "Place the origin using the object's Local-space bounding box"),
        ],
        default='WORLD',
    )


# ---------------------------------------------------------------------------
# Operators - Object Mode
# ---------------------------------------------------------------------------

class OS_OT_origin_axis(Operator):
    """Move the origin along one axis to the Top / Middle / Down of the bounding box (all selected objects, nothing jumps visually).

    مبدا را روی یک محور به بالا / وسط / پایینِ جعبهٔ محیطی ببر."""
    bl_idname = "os.origin_axis"
    bl_label = "Set Origin (Axis)"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(
        name="Axis",
        description="Axis to move the origin along",
        items=[
            ('X', "X", "X axis"),
            ('Y', "Y", "Y axis"),
            ('Z', "Z", "Z axis"),
        ],
        default='Z',
    )
    position: EnumProperty(
        name="Position",
        description="Where on this axis the origin should go",
        items=[
            ('MAX', "Top", "Positive side of the axis (top of the bounding box)"),
            ('CENTER', "Mid", "Middle of the bounding box on this axis"),
            ('MIN', "Down", "Negative side of the axis (bottom of the bounding box)"),
        ],
        default='CENTER',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        space = context.scene.os_settings.space
        axis_idx = AXIS_INDEX[self.axis]
        plan = {axis_idx: self.position}

        done, skipped = 0, 0
        for obj in context.selected_objects:
            if obj.type not in GEO_TYPES or obj.data is None or obj.data.library is not None:
                skipped += 1
                continue
            try:
                _move_origin(obj, _world_target_for(obj, space, plan))
                done += 1
            except Exception as ex:  # pragma: no cover - safety net per object
                skipped += 1
                print(f"Origin Studio: could not move origin of {obj.name!r}: {ex}")

        if done == 0:
            self.report({'WARNING'}, "No movable object selected (Mesh / Curve / Surface / Text)")
            return {'CANCELLED'}

        msg = f"Origin -> {self.position.lower()} of {self.axis}, {done} object(s)"
        if skipped:
            msg += f" ({skipped} skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class OS_OT_origin_preset(Operator):
    """One-click origin presets for all selected objects.

    پریست‌های آمادهٔ اورجین برای همهٔ آبجکت‌های انتخاب‌شده."""
    bl_idname = "os.origin_preset"
    bl_label = "Origin Preset"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Preset",
        items=[
            ('BOTTOM_CENTER', "Ground",
             "Origin to the bottom-center of the bounding box - perfect for grounding props "
             "before physics, snapping or stacking"),
            ('CENTER', "Center",
             "Origin to the center of the bounding box on all axes"),
        ],
        default='BOTTOM_CENTER',
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        space = context.scene.os_settings.space
        if self.mode == 'CENTER':
            plan = {0: 'CENTER', 1: 'CENTER', 2: 'CENTER'}
        else:  # BOTTOM_CENTER
            plan = {0: 'CENTER', 1: 'CENTER', 2: 'MIN'}

        done, skipped = 0, 0
        for obj in context.selected_objects:
            if obj.type not in GEO_TYPES or obj.data is None or obj.data.library is not None:
                skipped += 1
                continue
            try:
                _move_origin(obj, _world_target_for(obj, space, plan))
                done += 1
            except Exception as ex:  # pragma: no cover - safety net per object
                skipped += 1
                print(f"Origin Studio: could not move origin of {obj.name!r}: {ex}")

        if done == 0:
            self.report({'WARNING'}, "No movable object selected (Mesh / Curve / Surface / Text)")
            return {'CANCELLED'}

        label = "Center" if self.mode == 'CENTER' else "Ground (bottom-center)"
        msg = f"Origin -> {label}, {done} object(s)"
        if skipped:
            msg += f" ({skipped} skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operators - Edit Mode
# ---------------------------------------------------------------------------

class OS_OT_cursor_to_selection(Operator):
    """Snap the 3D cursor to the median of the current selection (vertices / edges / faces).

    کرسر 3D می‌رود دقیقاً وسطِ چیزی که در Edit Mode انتخاب شده."""
    bl_idname = "os.cursor_to_selection"
    bl_label = "Cursor to Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        cos = [v.co for v in bm.verts if v.select and not v.hide]
        if not cos:
            self.report({'WARNING'}, "Nothing selected - select some geometry first")
            return {'CANCELLED'}

        median = Vector((0.0, 0.0, 0.0))
        for co in cos:
            median += co
        median /= len(cos)

        context.scene.cursor.location = obj.matrix_world @ median
        return {'FINISHED'}


class OS_OT_cursor_to_bbox(Operator):
    """Snap the 3D cursor to the bounding-box center of the selection.

    کرسر به مرکز جعبهٔ محیطیِ انتخاب می‌رود."""
    bl_idname = "os.cursor_to_bbox"
    bl_label = "Cursor to Selection Box Center"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def execute(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        cos = [v.co for v in bm.verts if v.select and not v.hide]
        if not cos:
            self.report({'WARNING'}, "Nothing selected - select some geometry first")
            return {'CANCELLED'}

        mins = Vector((
            min(c[0] for c in cos),
            min(c[1] for c in cos),
            min(c[2] for c in cos),
        ))
        maxs = Vector((
            max(c[0] for c in cos),
            max(c[1] for c in cos),
            max(c[2] for c in cos),
        ))
        center = (mins + maxs) * 0.5

        context.scene.cursor.location = obj.matrix_world @ center
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class VIEW3D_PT_os_origin(Panel):
    """Tab 1 - fast origin placement for Object Mode."""
    bl_label = "Origin  ·  Object Mode"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Origin Studio"
    bl_context = 'objectmode'

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def draw(self, context):
        layout = self.layout
        props = context.scene.os_settings

        # --- Space selector --------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="Coordinate Space")
        row = col.row(align=True)
        row.prop(props, "space", expand=True, text="")

        # --- Per-axis buttons -------------------------------------------
        col = box.column(align=True)
        col.separator()
        col.label(text="Per Axis  ·  Top / Mid / Down")
        for axis in ("X", "Y", "Z"):
            row = col.row(align=True)
            split = row.split(factor=0.18, align=True)
            split.label(text=axis)
            buttons = split.row(align=True)

            op = buttons.operator(OS_OT_origin_axis.bl_idname, text="Top")
            op.axis = axis
            op.position = 'MAX'

            op = buttons.operator(OS_OT_origin_axis.bl_idname, text="Mid")
            op.axis = axis
            op.position = 'CENTER'

            op = buttons.operator(OS_OT_origin_axis.bl_idname, text="Down")
            op.axis = axis
            op.position = 'MIN'

        # --- Quick presets ----------------------------------------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="Quick Presets")
        row = col.row(align=True)
        op = row.operator(OS_OT_origin_preset.bl_idname, text="Ground", icon='TRIA_DOWN')
        op.mode = 'BOTTOM_CENTER'
        op = row.operator(OS_OT_origin_preset.bl_idname, text="Center", icon='PIVOT_BOUNDBOX')
        op.mode = 'CENTER'

        layout.label(text="Runs on all selected objects", icon='INFO')


class VIEW3D_PT_os_cursor(Panel):
    """Tab 2 - 3D cursor tools for Edit Mode."""
    bl_label = "Cursor  ·  Edit Mode"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Origin Studio"
    bl_context = 'editmode'

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.edit_object is not None

    def draw(self, context):
        layout = self.layout

        col = layout.column()
        col.scale_y = 2.0
        col.operator(OS_OT_cursor_to_selection.bl_idname,
                     text="Cursor → Selection", icon='CURSOR')

        col = layout.column()
        col.operator(OS_OT_cursor_to_bbox.bl_idname,
                     text="Selection Box Center", icon='PIVOT_BOUNDBOX')

        layout.label(text="Snaps to the median of the selection", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    OS_PG_settings,
    OS_OT_origin_axis,
    OS_OT_origin_preset,
    OS_OT_cursor_to_selection,
    OS_OT_cursor_to_bbox,
    VIEW3D_PT_os_origin,
    VIEW3D_PT_os_cursor,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.os_settings = PointerProperty(type=OS_PG_settings)


def unregister():
    del bpy.types.Scene.os_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
