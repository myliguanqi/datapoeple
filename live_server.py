"""启动实时直播服务：python live_server.py --config live/configs/live.yaml"""
from live.server import main

if __name__ == "__main__":   # Windows 多进程(spawn)必须有这个保护
    main()
