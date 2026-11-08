"""Пишет файл сведений о версии для PyInstaller. Запуск: python tools/version_info.py ["DZOD Revoicer"]"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ver = re.search(r'VERSION = "([^"]+)"', (ROOT / "app/engine.py").read_text(encoding="utf-8")).group(1)
name = sys.argv[1] if len(sys.argv) > 1 else "DZOD Vocalist"
t = tuple(int(x) for x in ver.split(".")) + (0,)
(ROOT / ("version_info.txt" if name == "DZOD Vocalist" else "version_info_rv.txt")).write_text(f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'ООО "Нейрофильм"'),
      StringStruct('FileDescription', '{name}'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
''', encoding="utf-8")
print(ver)
