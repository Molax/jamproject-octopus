# jamproject-octopus — índice

Overlay de polvo pra streamers: os tentáculos reagem às teclas tocadas (PyQt6 + pynput), pensado pra
capturar no OBS.

Stack: Python 3.10+, PyQt6 + pynput, lint por ruff. **Sem npm, sem uv, sem poetry** — `pip` dentro de
`.venv` criado pelos scripts de run.

## Regras

- **O app inteiro é `octopus.py`** (arquivo único, sem asset externo). Não fatiar em módulos nem
  virar pacote sem ordem explícita: `.github/workflows/release.yml` empacota
  `pyinstaller --onefile … octopus.py`.
- **Rodar = `./run.sh`** (Linux/macOS) ou `run.bat` (Windows): eles criam `.venv` e instalam o
  `requirements.txt` na primeira vez. Nunca rodar na python do sistema.
- **Dependência nova entra em `requirements.txt`.** O `pyproject.toml` NÃO tem `[project]` — ele só
  configura ruff (`line-length` 110, aspas simples, `E501`/`B008` ignorados); não adicionar
  metadados de pacote lá.
- **Keybinding é dado, não código:** default em `presets.toml` (embutido no `.exe`, cópia do usuário
  em `~/.jamproject/`). Mudou o formato ⇒ atualizar na MESMA edição o help inline do `presets.toml`,
  o README §"Key mappings — text mode" e o diálogo GUI de setup.
- **Sem display (CI, WSL, headless) ⇒ `QT_QPA_PLATFORM=offscreen`** em qualquer import/smoke.
- **Binário se gera por tag no CI** (`release.yml`, Windows/Linux/macOS). Local só pra reproduzir:
  `pyinstaller --onefile --noconsole --name JamProject-Octopus octopus.py` → `dist/`.
- **Action de workflow se pina por SHA, nunca por tag móvel** (commit `dfed93d` fez isso; não
  regredir pra `@v4`).
- **Não commitar `.venv/`, `dist/`, `build/`, `octopus.egg-info/`.**

## Mapa

| Caminho | O quê |
| --- | --- |
| `octopus.py` | o app inteiro |
| `presets.toml` | presets de tecla shipados + help inline |
| `run.sh` / `run.bat` | venv + launch de um comando |
| `.github/workflows/ci.yml` | gate: ruff + py_compile + import smoke em 3 OS |
| `.github/workflows/release.yml` | build PyInstaller no push de tag |
| `CONTRIBUTING.md` | setup de dev, checklist de PR, estilo |

## Gates

- `ruff format --check . && ruff check . && python -m py_compile octopus.py` — antes de todo commit
  (é exatamente o job `lint + py_compile` do CI)
- `QT_QPA_PLATFORM=offscreen .venv/bin/python -c "import runpy"` + o import smoke do CI
  (`ci.yml` §"import smoke", carrega `octopus.py` por `importlib.util`)
