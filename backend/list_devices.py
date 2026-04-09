"""Print available audio input devices.

Run with: python -m backend.list_devices

Use the printed index in config.yaml as `audio.device` to pin a specific mic.
"""

import sounddevice as sd


def main():
    devices = sd.query_devices()
    default_input = sd.default.device[0] if sd.default.device else None

    print(f"{'idx':>4}  {'in':>3}  {'out':>3}  name")
    print("-" * 60)
    for i, dev in enumerate(devices):
        marker = " *" if i == default_input else "  "
        print(
            f"{i:>4}  {dev['max_input_channels']:>3}  "
            f"{dev['max_output_channels']:>3}  {dev['name']}{marker}"
        )
    print()
    print("* = system default input")
    print("Set `audio.device` in config.yaml to the index of your microphone.")


if __name__ == "__main__":
    main()
