import asyncio
import logging

import yaml

from backend.app import FrameDisplayApp


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    app = FrameDisplayApp(config)
    asyncio.run(app.start())


if __name__ == "__main__":
    main()
