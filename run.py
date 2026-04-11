import asyncio
import logging
import pathlib
import sys

import yaml

from backend.app import FrameDisplayApp


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent


def _setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = log_cfg.get("file")
    if log_file:
        log_path = pathlib.Path(log_file)
        if not log_path.is_absolute():
            log_path = PROJECT_ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def main():
    config_path = pathlib.Path("config.yaml")
    if not config_path.exists():
        sys.exit(
            "config.yaml not found. Copy config.example.yaml to config.yaml and edit it."
        )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if not config:
        sys.exit(f"{config_path} is empty or invalid YAML.")

    _setup_logging(config)

    app = FrameDisplayApp(config)
    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
