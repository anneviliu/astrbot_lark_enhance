from __future__ import annotations


def enabled_memory_types(config: dict) -> set[str]:
    """User/group facts and memes have independent feature switches."""
    result = set()
    if config.get("enable_user_memory", True):
        result.update(("instruction", "preference", "fact"))
    if config.get("enable_meme_memory", True):
        result.add("meme")
    return result
