"""Isolated CPU worker for offline migration, without application/network imports."""
import json
import zlib
from .retention import split_alert
try:
    import orjson
except ImportError:
    orjson = None


def prepare_alert(args):
    row, cutoff, old_blob = args
    row = list(row)
    decode = orjson.loads if orjson else json.loads
    original = decode(row[-1])
    if old_blob:
        original.update(decode(zlib.decompress(old_blob)))
    expired = row[4] < cutoff
    encoded, blob = split_alert(original, expired=expired, encoder=orjson.dumps if orjson else None)
    restored = decode(encoded)
    if blob is not None:
        restored.update(decode(zlib.decompress(blob)))
    if not expired and any(restored.get(k) != v for k,v in original.items()):
        raise RuntimeError('Lossless snapshot check failed: ' + str(row[1]))
    row[-1] = encoded
    return row, blob, expired
