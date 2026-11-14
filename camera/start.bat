@echo off
cd /d "%~dp0"

if exist .venv\.installed goto run

echo Cai dat lan dau...
py -3.10 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul
if not exist .venv\Scripts\python.exe (
  echo Can Python 3.10 - 3.12 ^(khuyen dung 3.10^): https://www.python.org/downloads/
  pause
  exit /b 1
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
echo ok> .venv\.installed

:run
if not exist models\AgeModel\model.safetensors if exist download_models.py .venv\Scripts\python download_models.py
.venv\Scripts\python drowsy.py --port auto %*
pause
