"""Frozen training-selected comparison candidates; never a profit guarantee."""
import json
from pathlib import Path

_path=Path(__file__).resolve().parent.parent/'sources/tradingview/indicator-shortlist.json'
ITEMS=json.loads(_path.read_text('utf-8'))['items'] if _path.exists() else []
IDS={item['id'] for item in ITEMS}
