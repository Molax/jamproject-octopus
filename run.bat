@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ^>^> JamProject Octopus
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  Criando ambiente virtual...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo  ERRO: Python nao encontrado. Instale Python 3.10+ em https://python.org
        pause
        exit /b 1
    )
    echo  Instalando dependencias...
    .venv\Scripts\pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo  ERRO ao instalar dependencias.
        pause
        exit /b 1
    )
    echo  Pronto!
    echo.
)

.venv\Scripts\python octopus.py
