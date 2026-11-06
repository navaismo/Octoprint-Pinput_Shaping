# Repository Guidelines

## Project Structure & Module Organization

The Python package is `octoprint_Pinput_Shaping/`. Its entry point integrates OctoPrint permissions, hooks and downloads; `calibration.py` owns sessions and recovery; `firmware.py` parses Marlin reports; `motion.py` plans bounded excitation; `capture.py` manages sensor processes; `inputshaping_analyzer.py` fits shapers. Knockout.js lives in `static/js/`, with Jinja2 views in `templates/`.

Offline tests and the stored c13 C++ contract are in `tests/`. Sensor installers are in `bash/`; prebuilt firmware, mounting models and README images are in `FW_RPi2040/`, `STL Models/` and `assets/`. Read `docs/firmware-contract.md` before changing control or analysis behavior.

## Build, Test, and Development Commands

Use the Python environment where OctoPrint is installed:

- `python -m pip install -e '.[test]'` installs the editable plugin and test tools.
- `octoprint serve` runs the development server; restart after backend changes.
- `python -m pytest -q` exercises numerical validation, motion limits, simulated transport failures and recovery.
- `node tests/test_frontend.js` checks result application and frontend state.
- `PINPUT_FIRMWARE_ROOT=/path/to/Marlin_c13_octoprint python -m pytest -q tests/test_c13_contract.py` compiles and compares the actual firmware shaping functions.
- `python setup.py sdist` packages a source archive under `dist/`.

There is no frontend build step. The CI matrix checks Python 3.9, 3.11 and 3.12 with the pinned scientific dependencies.

## Coding Style & Naming Conventions

Write all code comments, README content and documents in English. Follow `.editorconfig`: UTF-8, LF, final newlines, no trailing whitespace and four-space Python/JavaScript indentation. Use Python `snake_case`, class `PascalCase` and existing JavaScript `camelCase`. Preserve the case-sensitive `Pinput_Shaping` identifier. No formatter or linter is configured.

## Testing Guidelines

Use pytest and `tests/test_*.py`; no coverage threshold is configured. Exercise observable failures rather than implementation details. Keep serial callbacks nonblocking and test restoration after cancellation, disconnection, timeout and analysis errors. Use synthetic captures and simulated transports by default. Physical printer motion requires explicit approval.

## Commit & Pull Request Guidelines

History uses descriptive subjects, often `Fix:`, plus release version subjects. Describe behavior, link relevant issues, record validation and include screenshots for UI changes. Keep work on `plugin_optimization`; do not commit or push without explicit approval. Preserve unrelated local edits.

## Firmware & Configuration

Match control commands and numerical behavior to `Marlin_c13_octoprint`. Read its source without modifying or flashing it unless separately authorized. Never infer missing runtime settings from factory defaults. Keep packaged resources consistent with `MANIFEST.in`; review privileged installers before execution.
