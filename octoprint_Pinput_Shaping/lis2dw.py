"""LIS2DW range negotiation checks; CSV acceleration is already expressed in g."""

from .firmware import finite_number


COUNTS_PER_G = {2: 16384, 4: 8192, 8: 4096, 16: 2048}
RANGE_UPGRADE = "Update lis2dwusb and its matching RP2040 firmware for range negotiation, or select +/-2 g for legacy support."


def validate_range(value):
    value = finite_number(value, "LIS2DW accelerometer range")
    if value not in COUNTS_PER_G:
        raise ValueError("LIS2DW accelerometer range must be 2, 4, 8 or 16 g")
    return int(value)


def scale_metadata(metadata, requested_range, required=False):
    """Verify scale without converting CSV values or guessing a negotiated range.

    Old protocol-2 metadata has no range fields and is accepted only for the
    legacy fixed +/-2 g path, with its explicitly reported 16384 counts/g.
    """
    requested_range = validate_range(requested_range)
    if not isinstance(metadata, dict):
        raise ValueError("LIS2DW scale metadata must be an object")
    negotiated = "requested_range_g" in metadata or "range_g" in metadata
    if not negotiated and (required or requested_range != 2):
        raise ValueError("LIS2DW range negotiation metadata is missing. " + RANGE_UPGRADE)
    if negotiated:
        for key in ("requested_range_g", "range_g"):
            if type(metadata.get(key)) is not int or metadata[key] != requested_range:
                raise ValueError(f"LIS2DW {key} does not match requested +/-{requested_range} g. " + RANGE_UPGRADE)
    scale = metadata.get("counts_per_g")
    if type(scale) is not int or scale != COUNTS_PER_G[requested_range] or metadata.get("units") != "g":
        raise ValueError(f"LIS2DW scale metadata must report g and {COUNTS_PER_G[requested_range]} counts_per_g "
                         f"for +/-{requested_range} g. " + RANGE_UPGRADE)
    return {"requested_range_g": requested_range, "range_g": requested_range,
            "counts_per_g": scale, "units": "g", "range_negotiated": negotiated}
