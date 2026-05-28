# Contributing to JamProject Octopus

Thanks for considering a contribution! This repo is a small, **single-file Python overlay** — easy to read, easy to hack. PRs welcome.

## What belongs in this repo

- The PyQt6 overlay window (`octopus.py`)
- Cross-platform input plumbing (pynput, keymap defaults)
- Build / release pipeline (PyInstaller workflows, packaging)
- Issue / PR templates, contributor docs

## What does NOT belong here

- Gameplay code, scoring formulas, chart parsing — that's in [jam-legend-revival](https://github.com/Molax/jam-legend-revival)
- Account / API / database — separate backend
- The Angular in-game version of the overlay — that lives in `jam-legend-revival/jam-legend-web/src/app/features/streamer/octopus-overlay/`. This Python port should track its behaviour but is intentionally standalone and may diverge.

If your contribution touches the web frontend or the API, please open it against [jam-legend-revival](https://github.com/Molax/jam-legend-revival) instead.

## Dev setup

```bash
git clone https://github.com/Molax/jamproject-octopus.git
cd jamproject-octopus

python -m venv .venv
# Windows:   .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, pyinstaller (created on demand)

python octopus.py
```

The whole app is in `octopus.py`. There's no build step for dev — edit the file, re-run `python octopus.py`.

## Code style

- **Python:** 3.10+ syntax (`match`, `|` union types, `from __future__ import annotations`)
- Format / lint with [Ruff](https://docs.astral.sh/ruff/): `ruff format . && ruff check .`
- Type hints on all new functions; we run `mypy --strict` in CI on PRs that touch core logic
- **Imports:** stdlib → third-party → local, alphabetised within groups
- **No global mutable state** outside the `OctopusWidget` / `State` instances
- **No `print` for diagnostics** in shipped code — Qt's logging or `sys.stderr` is fine for fatal startup errors

## Pull request checklist

Before opening a PR:

- [ ] `python -m py_compile octopus.py` succeeds
- [ ] `ruff check .` is clean (or your PR fixes the warning intentionally — explain why)
- [ ] The app launches and the window appears on at least your dev OS
- [ ] You can press your default lane keys and the tentacles + KPS react
- [ ] If you changed defaults (DEFAULT_KEYS, mood thresholds, etc.), the README is updated
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) — e.g. `feat(menu): add chroma-key background toggle`, `fix(macos): wayland keyboard listener`

## How the visuals work

The octopus is drawn with `QPainter` inside a 200×240 SVG-style "viewBox" that's then scaled to the current window size. All the geometry constants (`BODY_CX`, `KEY_AREA_TOP`, `LANE_COLORS`, etc.) live at the top of `octopus.py` and mirror the Angular component in [`jam-legend-revival`](https://github.com/Molax/jam-legend-revival/blob/main/jam-legend-web/src/app/features/streamer/octopus-overlay/octopus-overlay.component.ts) — keeping them numerically identical means visual parity between the web and desktop versions.

Animation runs off a single `QTimer` ticking every ~33 ms (≈30 Hz). The `State` dataclass holds all keystroke-derived state and is queried by `paintEvent`. No background threads except pynput's listener (which is daemonised).

## Reporting security issues

Please **do not** open a public issue for security-sensitive reports (e.g. a way the global keyboard listener could be abused). Instead, contact the maintainer privately via Discord or email — details on [jamproject.net/contact](https://jamproject.net/contact).

## Code of conduct

Be kind. Disagree on technical things, not on people. Anyone making the community worse will be removed.
