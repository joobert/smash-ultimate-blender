"""Selection anchors are independent of Blender UIList's active row."""


def select_range(items, field, index, anchor, *, shift=False, toggle=False, visible_indices=None):
    if not 0 <= index < len(items):
        return anchor
    visible = list(range(len(items))) if visible_indices is None else list(visible_indices)
    if index not in visible:
        return anchor
    anchor_index = next((i for i, item in enumerate(items) if item.name == anchor and i in visible), index)
    if shift:
        first, last = sorted((visible.index(anchor_index), visible.index(index)))
        selected = set(visible[first:last + 1])
        for i, item in enumerate(items):
            setattr(item, field, i in selected or (toggle and getattr(item, field)))
        return items[anchor_index].name
    if toggle:
        setattr(items[index], field, not getattr(items[index], field))
    else:
        for i, item in enumerate(items):
            setattr(item, field, i == index)
    return items[index].name


def visible_list_indices(ui_list, items):
    """Use Blender's own filter matching and displayed alphabetical order."""
    import bpy
    flags = bpy.types.UI_UL_list.filter_items_by_name(
        ui_list.filter_name, ui_list.bitflag_filter_item, items, 'name')
    indices = [i for i in range(len(items))
               if (not flags or bool(flags[i] & ui_list.bitflag_filter_item))
               != ui_list.use_filter_invert]
    if ui_list.use_filter_sort_alpha:
        indices.sort(key=lambda i: items[i].name.casefold())
    if ui_list.use_filter_sort_reverse:
        indices.reverse()
    return indices
