"""Read original reflection identity for the existing external alias store.

This is display identity, not the normalized physical ABI projection. Keeping
the original top-level names is necessary to match qrenderdoc's layout key.
"""


def reflection_alias_blocks(reflection, fields_for_constants, annotations_complete):
    blocks = []
    for index, block in enumerate(list(getattr(reflection, "constantBlocks", []) or [])):
        variables = list(getattr(block, "variables", []) or [])
        members = [{"name": str(getattr(item, "name", "")),
                    "offset": int(getattr(item, "byteOffset", -1))}
                   for item in variables]
        fields = fields_for_constants(variables)
        byte_size = int(getattr(block, "byteSize", 0))
        complete = bool(fields and annotations_complete(fields, byte_size))
        leaves = []

        def collect(values):
            for field in values:
                # Array children need indexed panel paths and per-element
                # stride expansion; do not treat element zero as the whole
                # reflected field until that mapping is explicitly supported.
                if int(field.get("elements", 1)) > 1:
                    continue
                children = field.get("members") or []
                if children:
                    collect(children)
                else:
                    leaves.append({"path": str(field.get("path", "")),
                                   "offset": int(field.get("absoluteByteOffset", -1)),
                                   "byteSize": int(field.get("byteSize", 0))})

        if complete:
            collect(fields)
        blocks.append({
            "block": str(getattr(block, "name", "")),
            "bind": int(getattr(block, "fixedBindNumber", index)),
            "space": int(getattr(block, "fixedBindSetOrSpace", 0)),
            "byteSize": byte_size,
            "members": members,
            "leaves": leaves,
            "memberMappingAvailable": complete,
        })
    return blocks
