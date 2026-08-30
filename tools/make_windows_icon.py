"""Create multi-resolution Windows icon from committed MixMill PNG."""
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "app" / "static" / "icon-512.png"
TARGET = ROOT / ".build" / "MixMill.ico"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(SOURCE) as image:
        image.convert("RGBA").save(
            TARGET,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                   (128, 128), (256, 256)],
        )
    print(f"Windows icon ready: {TARGET}")


if __name__ == "__main__":
    main()
