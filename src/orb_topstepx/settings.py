"""Settings model and JSON persistence.

Location per platform (Windows being the primary target since Topstep requires
you to run automation on your own machine):
  Windows: %APPDATA%/orb-topstepx/settings.json
  mac/linux: ~/.config/orb-topstepx/settings.json
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PairedStopsSettings:
    account_name: str = ""           # empty = first available account
    contract_id: str = ""            # TopstepX contract id (resolved once per session)
    instrument_name: str = "NQ"      # user-visible symbol; resolved to contract_id on connect
    offset_points: float = 10.0
    quantity: int = 1
    take_profit_ticks: int = 50      # matches IMBA Target
    stop_loss_ticks: int = 40        # matches IMBA StopLoss
    pair_tag_prefix: str = "PAIRSTOP_"
    always_on_top: bool = True


def _settings_dir() -> Path:
    # Honor APPDATA on Windows; otherwise use XDG-style ~/.config.
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "orb-topstepx"
    return Path.home() / ".config" / "orb-topstepx"


def settings_path() -> Path:
    return _settings_dir() / "settings.json"


def load() -> PairedStopsSettings:
    path = settings_path()
    if not path.exists():
        return PairedStopsSettings()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        # Only copy keys we know about; tolerate unknown/missing keys gracefully.
        known = {f.name for f in PairedStopsSettings.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return PairedStopsSettings(**kwargs)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as ex:
        print(f"[orb-topstepx] settings load failed: {ex}; using defaults")
        return PairedStopsSettings()


def save(settings: PairedStopsSettings) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(settings), f, indent=2)
    except OSError as ex:
        print(f"[orb-topstepx] settings save failed: {ex}")


def load_credentials() -> tuple:
    """Return (username, api_key) from environment or .env file in cwd.

    Returns (None, None) if either is missing. Tries .env first for convenience;
    real-world users typically export via Windows system env vars instead.
    """
    username: Optional[str] = None
    api_key: Optional[str] = None

    dotenv = Path.cwd() / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TOPSTEPX_USERNAME" and not username:
                username = v
            elif k == "TOPSTEPX_API_KEY" and not api_key:
                api_key = v

    # Env vars win if set (so system-level env overrides a stale .env).
    username = os.environ.get("TOPSTEPX_USERNAME", username)
    api_key = os.environ.get("TOPSTEPX_API_KEY", api_key)

    return username or None, api_key or None
