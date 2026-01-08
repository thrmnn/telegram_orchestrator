#!/usr/bin/env python3
"""Kill all running bot processes."""

import os
import signal
import psutil

killed = 0
for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cmdline = proc.info.get('cmdline', [])
        if cmdline and 'python' in cmdline[0] and 'main.py' in ' '.join(cmdline):
            print(f"Killing process {proc.info['pid']}: {' '.join(cmdline)}")
            os.kill(proc.info['pid'], signal.SIGTERM)
            killed += 1
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

print(f"Killed {killed} bot process(es)")
