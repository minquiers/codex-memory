from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_NAME = "codex-memory"
DEFAULT_CONFIG_PATH = Path.home() / f".{APP_NAME}" / "config.json"
DEFAULT_CODEX_SESSION_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_LOCAL_SYNC_TEMP_DIR = Path.home() / f".{APP_NAME}" / "sync-repo"


@dataclass
class AppConfig:
    codex_session_dir: str = str(DEFAULT_CODEX_SESSION_DIR)
    git_remote_url: str = ""
    local_sync_temp_dir: str = str(DEFAULT_LOCAL_SYNC_TEMP_DIR)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        defaults = cls()
        return cls(
            codex_session_dir=str(
                data.get("codex_session_dir", defaults.codex_session_dir)
            ),
            git_remote_url=str(data.get("git_remote_url", defaults.git_remote_url)),
            local_sync_temp_dir=str(
                data.get("local_sync_temp_dir", defaults.local_sync_temp_dir)
            ),
        )


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    if config_path is None:
        return DEFAULT_CONFIG_PATH
    return Path(config_path).expanduser().resolve()


def save_config(config: AppConfig, config_path: str | Path | None = None) -> Path:
    path = resolve_config_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = resolve_config_path(config_path)
    if not path.exists():
        config = AppConfig()
        save_config(config, path)
        return config

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse config file: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")

    config = AppConfig.from_dict(data)
    save_config(config, path)
    return config


if __name__ == "__main__":
    current_config = load_config()
    config_file = resolve_config_path()
    print(f"Config file: {config_file}")
    print(json.dumps(current_config.to_dict(), indent=2, ensure_ascii=False))
