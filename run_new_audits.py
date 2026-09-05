import subprocess
import time
import sys

python_exe = r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
operator_script = r".\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py"

# Wait for any existing music_audit_operator.py to finish
print("Waiting for existing audits to finish...")
while True:
    try:
        # Check if the operator is running using tasklist or wmic
        # A simple way in Python on Windows is using tasklist
        # But we need to look for python.exe running the operator script
        result = subprocess.check_output('wmic process where "name=\'python.exe\'" get commandline', text=True)
        if "music_audit_operator.py start" in result or "engage_tiktok.py" in result:
            time.sleep(10)
        else:
            break
    except Exception:
        time.sleep(10)

print("Existing audits finished. Starting new creator audits...")

jobs = [
    ["--creator", "@elevenlabs", "--all-posts", "--run-label", "elevenlabs"],
    ["--creator", "@sunomusic", "--all-posts", "--run-label", "sunomusic"],
    ["--creator", "@fishaudio", "--all-posts", "--run-label", "fishaudio"],
]

for args in jobs:
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting job: {' '.join(args)}")
    cmd = [python_exe, operator_script, "start"] + args
    try:
        res = subprocess.run(cmd, text=True)
        print(f"Job completed with exit code: {res.returncode}")
    except Exception as e:
        print(f"Job failed to start: {e}")
    time.sleep(10)

print("\nAll new audits finished!")
