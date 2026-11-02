@echo off
rem Start live server in mock mode (no model / no GPU needed). Change the conda env name if needed.
cd /d %~dp0
call conda activate flashhead
python live_server.py --config live/configs/mock.yaml %*
pause
