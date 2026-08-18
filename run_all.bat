@echo off
title Esegui tutto

echo Installando le librerie...
python -m pip install -r requirements.txt

echo Avviando il server...
python app.py