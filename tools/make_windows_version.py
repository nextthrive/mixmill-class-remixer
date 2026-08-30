"""Generate PyInstaller Windows version resources from desktop.version."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.version import APP_VERSION, FILE_VERSION  # noqa: E402


TARGET = ROOT / ".build" / "MixMill-version.txt"


def main() -> None:
    numeric = ", ".join(str(part) for part in FILE_VERSION)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({numeric}),
    prodvers=({numeric}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'MixMill'),
          StringStruct('FileDescription', 'MixMill Desktop'),
          StringStruct('FileVersion', '{APP_VERSION}'),
          StringStruct('InternalName', 'MixMill'),
          StringStruct('LegalCopyright', 'MixMill contributors'),
          StringStruct('OriginalFilename', 'MixMill.exe'),
          StringStruct('ProductName', 'MixMill Desktop'),
          StringStruct('ProductVersion', '{APP_VERSION}'),
        ],
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""",
        encoding="utf-8",
    )
    print(f"Windows version metadata ready: {TARGET}")


if __name__ == "__main__":
    main()
