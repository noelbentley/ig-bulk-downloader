# Third-party notices

The original Instaloader engine remains unmodified, under its MIT license. See
LICENSE and AUTHORS.md for its original attribution.

The desktop application uses PySide6 and dynamically loaded Qt libraries. Qt for
Python is offered under LGPLv3/GPLv3 or commercial terms; the modules used here
are Qt Core, Gui and Widgets. See https://doc.qt.io/qtforpython-6/licenses.html and
https://code.qt.io/pyside/pyside-setup.git/ for licenses and corresponding source.
Qt source is available at https://code.qt.io/qt/qtbase.git/ . No Qt changes are
made. You may replace/rebuild these libraries by running this application's
Python source with a compatible PySide6 installation, or rebuilding the executable.

Requests is Apache-2.0 licensed; its dependencies include urllib3 (MIT), idna
(BSD), charset-normalizer (MIT), and certifi (MPL-2.0). PyInstaller is a build
tool under GPL with a bootloader exception. The executable bundles available
dependency copyright and license texts under `licenses` in its extracted runtime.
These notices do not replace individual dependency licenses.

The reproducible source build instructions are in DESKTOP.md. Dependency versions
are recorded in requirements-desktop-lock.txt. No credentials, sessions, history
or downloaded media are included in the executable.
