from __future__ import annotations

from pathlib import Path


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Replace or append `KEY="value"` lines in a .env file, preserving every other line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining and not line.lstrip().startswith("#"):
            out.append(f'{key}="{remaining.pop(key)}"')
        else:
            out.append(line)
    out.extend(f'{k}="{v}"' for k, v in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
