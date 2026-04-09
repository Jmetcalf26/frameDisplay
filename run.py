import asyncio
import logging
import pathlib
import sys

import yaml

from backend.app import FrameDisplayApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config_path = pathlib.Path("config.yaml")
    if not config_path.exists():
        sys.exit(
            "config.yaml not found. Copy config.example.yaml to config.yaml and edit it."
        )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if not config:
        sys.exit(f"{config_path} is empty or invalid YAML.")

    app = FrameDisplayApp(config)
    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
