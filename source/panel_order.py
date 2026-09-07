"""Shared registry of top-level Ultimate panels, plus the sidebar ordering engine.

Panel visibility (the presets checklist) and panel order are two halves of one
feature, so they share this registry: one list of controllable panels, one set
of labels, one default order. ``source/extras/panel_presets.py`` owns the UI and
the per-preset state; this module owns discovery and the ``bl_order`` writes.
"""

import bpy


# Always visible and always pinned to the bottom, so it is never orderable and
# never hideable.
PRESETS_PANEL_ID = "SUB_PT_panel_presets"


# The add-on's default top-level order, with the label shown in the Panel
# Presets list. Panels registered outside this table are appended in bl_order
# then alphabetical order, so new features show up without a code change here.
PANEL_META = (
    ("SUB_PT_import_model", "Model Importer"),
    ("SUB_PT_export_model", "Model Exporter"),
    ("SUB_PT_ultimate_exo_skel", "Magic Exo Skel Maker"),
    ("SUB_PT_import_anim", "Animation Importer"),
    ("SUB_PT_raw_animations", "Raw Animations"),
    ("SUB_PT_export_anim", "Animation Exporter"),
    ("SUB_PT_smash_export_doctor", "Export Doctor"),
    ("SUB_PT_animation_tools", "Animation Tools"),
    ("SUB_PT_model_tools", "Model Tools"),
    ("SUB_PT_collection_presets", "Armature Collection Presets"),
    ("SUB_PT_face_picker", "Easy Facial Animation"),
    ("SUB_PT_misc_utilities", "Misc."),
    ("SUB_PT_stage_tools", "Stage Tools"),
    ("SUB_PT_reimport_materials", "Material Re-Importer"),
    ("SUB_PT_attribute_renamer", "Attribute Renamer"),
    ("SUB_PT_swing_io", "Swing"),
    ("SUB_PT_retargeting_main", "Retargeting"),
    ("SUB_PT_update_plugin", "Update Available!"),
)

_DEFAULT_INDEX = {panel_id: index for index, (panel_id, _label) in enumerate(PANEL_META)}
_DEFAULT_LABELS = dict(PANEL_META)


def _panel_class_id(cls):
    """The id Blender registered the panel under, which bl_parent_id matches."""
    rna = getattr(cls, "bl_rna", None)
    identifier = getattr(rna, "identifier", None) if rna is not None else None
    if identifier:
        return str(identifier)
    return str(cls.__dict__.get("bl_idname") or getattr(cls, "__name__", "") or "")


def _iter_ultimate_panel_classes():
    """Every Ultimate sidebar panel class, parents and children alike.

    The Ultimate category is the add-on's own, so membership in it is the
    filter. Children are included so the ordering pass can re-register a parent
    without orphaning its subpanels, whichever module they were defined in.
    """
    seen = set()
    for attr in dir(bpy.types):
        cls = getattr(bpy.types, attr, None)
        if not isinstance(cls, type):
            continue
        try:
            if not issubclass(cls, bpy.types.Panel):
                continue
        except TypeError:
            continue
        if getattr(cls, "bl_category", None) != "Ultimate":
            continue
        if getattr(cls, "bl_space_type", None) != "VIEW_3D":
            continue
        if getattr(cls, "bl_region_type", None) != "UI":
            continue
        panel_id = _panel_class_id(cls) or attr
        # bpy.types can expose one class under more than one attribute name.
        if not panel_id or panel_id in seen:
            continue
        seen.add(panel_id)
        yield panel_id, cls


def _sort_key(panel_id, cls, label):
    index = _DEFAULT_INDEX.get(panel_id)
    if index is not None:
        return (0, index, "")
    return (1, int(getattr(cls, "bl_order", 0) or 0), label.casefold())


def discover_panels():
    """Controllable top-level Ultimate panels as ``(panel_id, cls, label)``.

    Returned in the add-on's default order. Panel Presets is excluded: it is
    pinned to the bottom and must stay reachable to undo a bad preset.
    """
    found = {}
    for panel_id, cls in _iter_ultimate_panel_classes():
        if panel_id == PRESETS_PANEL_ID:
            continue
        if getattr(cls, "bl_parent_id", None):
            continue
        label = _DEFAULT_LABELS.get(panel_id) or getattr(cls, "bl_label", None) or panel_id
        found[panel_id] = (cls, label)
    return [
        (panel_id, cls, label)
        for panel_id, (cls, label) in sorted(
            found.items(), key=lambda item: _sort_key(item[0], item[1][0], item[1][1])
        )
    ]


def default_order():
    """Panel ids in the add-on's default order."""
    return [panel_id for panel_id, _cls, _label in discover_panels()]


def _registration_sequence(root_ids):
    """Root panels followed by their descendants, parents always first."""
    all_panels = dict(_iter_ultimate_panel_classes())
    children = {}
    for panel_id, cls in all_panels.items():
        parent = getattr(cls, "bl_parent_id", None) or ""
        if parent:
            children.setdefault(str(parent), []).append(panel_id)

    sequence = []
    seen = set()

    def walk(panel_id):
        if panel_id in seen or panel_id not in all_panels:
            return
        seen.add(panel_id)
        sequence.append(all_panels[panel_id])
        for child in children.get(panel_id, ()):
            walk(child)

    for root_id in root_ids:
        walk(root_id)
    return sequence


def _reregister(root_ids):
    """Re-register panels so a changed bl_order takes effect immediately.

    Blender reads bl_order when a panel type is registered, so assigning it to a
    live class is not enough on its own. Children are unregistered first and
    re-registered last; re-registering a parent alone orphans its subpanels into
    loose top-level panels.
    """
    sequence = _registration_sequence(root_ids)
    for cls in reversed(sequence):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    for cls in sequence:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass


def tag_redraw():
    for screen in getattr(bpy.data, "screens", ()):
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


_pending_reregister = None


def _reregister_timer():
    global _pending_reregister
    pending, _pending_reregister = _pending_reregister, None
    if pending:
        try:
            _reregister(pending)
            tag_redraw()
        except Exception as e:
            print(f"Smash_ultimate_blender: Could not apply panel order: {e}")
    return None


def _schedule_reregister(order):
    """Re-register on the next timer tick.

    Order changes arrive from operators and property callbacks, which can run
    close enough to a redraw that swapping panel types out from under Blender is
    risky. Deferring keeps that off the UI's critical path.
    """
    global _pending_reregister
    _pending_reregister = list(order)
    if not bpy.app.timers.is_registered(_reregister_timer):
        bpy.app.timers.register(_reregister_timer, first_interval=0.0)


def apply_order(ordered_ids, *, defer=False):
    """Lay out the Ultimate tab in ``ordered_ids`` order.

    Unknown ids are ignored and registered panels missing from the list are
    appended in default order, so a stale saved list never hides a panel.
    """
    panels = {panel_id: cls for panel_id, cls, _label in discover_panels()}

    final = []
    for panel_id in ordered_ids:
        if panel_id in panels and panel_id not in final:
            final.append(panel_id)
    for panel_id in panels:
        if panel_id not in final:
            final.append(panel_id)

    for index, panel_id in enumerate(final):
        # Gaps leave room to slot in future built-in panels without ties.
        panels[panel_id].bl_order = (index + 1) * 10

    if defer:
        _schedule_reregister(final)
    else:
        _reregister(final)
        tag_redraw()
    return final


def cancel_pending():
    global _pending_reregister
    _pending_reregister = None
    if bpy.app.timers.is_registered(_reregister_timer):
        try:
            bpy.app.timers.unregister(_reregister_timer)
        except Exception:
            pass
