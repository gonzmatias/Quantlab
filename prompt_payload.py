"""Lossless deduplication of JSON data sent to the researcher.

Only byte-identical values are shared. No rounding, truncation, field selection,
or model-written summaries: validation data boundaries remain the caller's job.
"""
import json
from collections import Counter


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def research_payload(value):
    raw = compact_json(value)
    marker = "$shared"
    while marker in raw:
        marker += "_"
    counts = Counter()

    def count(item):
        if isinstance(item, (dict, list, str)):
            key = compact_json(item)
            if len(key) >= 120:
                counts[key] += 1
        if isinstance(item, dict):
            for child in item.values():
                count(child)
        elif isinstance(item, list):
            for child in item:
                count(child)

    count(value)
    identifiers, shared = {}, {}

    def children(item):
        if isinstance(item, dict):
            return {key: encode(child) for key, child in item.items()}
        if isinstance(item, list):
            return [encode(child) for child in item]
        return item

    def encode(item):
        key = compact_json(item) if isinstance(item, (dict, list, str)) else None
        if key is not None and counts[key] > 1:
            if key not in identifiers:
                identifier = str(len(identifiers))
                identifiers[key] = identifier
                shared[identifier] = children(item)
            return {marker: identifiers[key]}
        return children(item)

    data = encode(value)
    packed = compact_json({
        "format": "shared-json-v1", "reference_key": marker,
        "instructions": "Cada objeto de un solo campo con reference_key referencia su valor íntegro en shared. Resolver esas referencias antes de interpretar data. No falta información.",
        "shared": shared, "data": data,
    })
    # Pooling small payloads would add overhead. Use whichever representation is
    # shorter, including the reference explanation and dictionary.
    return packed if len(packed) < len(raw) else raw
