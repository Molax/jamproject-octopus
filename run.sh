#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo
echo ">> JamProject Octopus"
echo

if [ ! -f ".venv/bin/python" ]; then
    echo " Criando ambiente virtual..."
    python3 -m venv .venv
    echo " Instalando dependencias..."
    .venv/bin/pip install --quiet -r requirements.txt
    echo " Pronto!"
    echo
fi

.venv/bin/python octopus.py
