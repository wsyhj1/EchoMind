"""Shared application skill catalog, available from the backend launch directory."""
from pathlib import Path
import sys

_root = str(Path(__file__).resolve().parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)

from echomind_skills import (  # noqa: E402
    SkillCommandError, build_skill_prompt, public_skills, resolve_command, registry_metadata, get_skill,
)
