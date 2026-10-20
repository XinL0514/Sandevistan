# Repository Guidelines

## Project Structure & Module Organization

This is a macOS Qt desktop app for Android Debug Bridge (ADB). `main_qt.py` is the UI entry point; `adb_client.py` wraps ADB commands. Feature logic lives in root-level modules such as `dart_network.py`, `logcat_tools.py`, and `performance_tools.py`. `theme.py` and `style.qss` define the appearance. Unit tests are in `tests/test_*.py`. Packaging scripts are in `scripts/`, the PyInstaller configuration is `Sandevistan ADB.spec`, and license materials are in `licenses/` and `distribution/sources/`. Generated `build/` and `dist/` output is ignored.

## Build, Test, and Development Commands

Run from the repository root on macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-qt.txt pyinstaller
.venv/bin/python main_qt.py
.venv/bin/python -m unittest discover -s tests
./scripts/build_qt_app.sh
```

The last command builds and verifies `dist/Sandevistan ADB.app` and a development ZIP. It fetches required LGPL source archives and uses macOS signing tools. Install Android SDK Platform-Tools separately; put `adb` on `PATH` or set `ADB_PATH`.

## Coding Style & Naming Conventions

Use four spaces in Python, `snake_case` for functions and modules, and `PascalCase` for classes. Follow the surrounding type hints and docstrings. Keep ADB process handling in `adb_client.py` and UI behavior in `main_qt.py`. Background workers must pass results to the GUI thread before changing Qt widgets. Update `theme.py` tokens and `style.qss` together when changing visual styles. No formatter or linter configuration is currently committed.

## Testing Guidelines

Tests use the standard-library `unittest` framework and `unittest.mock`; name new files `tests/test_<feature>.py` and methods `test_<behavior>`. Run the full discovery command above after changes. Mock ADB and device responses for unit tests; add focused cases for parsing, errors, and device-specific behavior. There is no stated coverage threshold. The full suite needs the Qt dependencies because `test_install_targets.py` imports `main_qt.py`.

## Commit & Pull Request Guidelines

This repository has no commits yet, so no historical commit format is established. Use short, imperative subjects that describe the change, such as `Handle offline devices during install`. Pull requests should explain user-visible behavior, list tests run, link relevant issues, and include screenshots for UI changes. For packaging changes, report bundle verification and any license or source-archive updates.
