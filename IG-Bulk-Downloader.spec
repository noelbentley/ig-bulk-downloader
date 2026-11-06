# Build on Windows with the pinned desktop environment.
from importlib.metadata import distributions
from pathlib import Path

root = Path(SPECPATH)
data = [(str(root / 'LICENSE'), 'licenses/instaloader'),
        (str(root / 'THIRD_PARTY_NOTICES.md'), 'licenses'),
        (str(root / 'DESKTOP.md'), '.'),
        (str(root / 'LIVE_THROUGHPUT.md'), '.'),
        (str(root / 'PERFORMANCE.md'), '.')]
data.append((str(root / 'OPTIMIZED_ENGINE.md'), '.'))
# Include installed dependency copyright/license texts, not local runtime data.
for distribution in distributions():
    for entry in distribution.files or ():
        if any('license' in part.lower() or 'copying' in part.lower() or
               'notice' in part.lower() for part in entry.parts):
            source = Path(distribution.locate_file(entry))
            if source.is_file() and source.suffix.lower() not in ('.py', '.pyc', '.pyd', '.dll', '.exe'):
                data.append((str(source), 'licenses/' + distribution.metadata['Name'] + '/' + str(entry.parent)))

analysis = Analysis([str(root / 'run_desktop.py')], pathex=[str(root)], binaries=[], datas=data,
                    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
                    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
                              'PySide6.QtQuick', 'PySide6.QtPdf'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
          name='IG-Bulk-Downloader', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False)
