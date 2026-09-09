"""Selection anchors are independent of Blender UIList's active row."""


def select_range(items, field, index, anchor, *, shift=False, toggle=False):
    if not 0 <= index < len(items):
        return anchor
    anchor_index = next((i for i, item in enumerate(items) if item.name == anchor), index)
    if shift:
        first, last = sorted((anchor_index, index))
        for i, item in enumerate(items):
            setattr(item, field, first <= i <= last or (toggle and getattr(item, field)))
        return items[anchor_index].name
    if toggle:
        setattr(items[index], field, not getattr(items[index], field))
    else:
        for i, item in enumerate(items):
            setattr(item, field, i == index)
    return items[index].name
