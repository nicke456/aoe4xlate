"""
Player name resolution via the AoE4World API.
Profile IDs are cached for the lifetime of the process.
"""

import requests
from typing import Optional

_AOE4WORLD_BASE = "https://aoe4world.com/api/v0/players"
_cache: dict[int, str] = {}
_session = requests.Session()
_session.headers["User-Agent"] = "aoe4xlate/1.0 (github.com/aoe4xlate)"


def resolve(profile_id: int) -> str:
    """Return the player's display name, falling back to the profile ID string."""
    if profile_id in _cache:
        return _cache[profile_id]

    try:
        resp = _session.get(f"{_AOE4WORLD_BASE}/{profile_id}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("name") or str(profile_id)
            _cache[profile_id] = name
            return name
    except Exception as e:
        print(f"[players] Failed to resolve {profile_id}: {e}")

    _cache[profile_id] = str(profile_id)
    return str(profile_id)


def clear_cache():
    _cache.clear()
