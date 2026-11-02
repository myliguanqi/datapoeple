@echo off
rem Start live server with real FlashHead models. Change the conda env name if needed.
cd /d %~dp0
call conda activate flashhead
python live_server.py --config live/configs/live.yaml %*
pause
