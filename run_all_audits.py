import subprocess
import time
import sys

python_exe = r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
operator_script = r".\.agents\skills\google-3.1-music-audit-instructions\scripts\music_audit_operator.py"

jobs = [
    ["--creator", "@elevenlabsio", "--all-posts", "--run-label", "elevenlabsio"],
    ["--creator", "@luma.ai", "--all-posts", "--run-label", "lumaai"],
    ["--creator", "@capcut", "--all-posts", "--run-label", "capcut"],
    ["--topic", "suno ai", "--all-posts", "--run-label", "sunoai"],
]

for args in jobs:
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting job: {' '.join(args)}")
    cmd = [python_exe, operator_script, "start"] + args
    try:
        result = subprocess.run(cmd, text=True)
        print(f"Job completed with exit code: {result.returncode}")
    except Exception as e:
        print(f"Job failed to start: {e}")
    time.sleep(10)  # brief pause before next job

print("\nAll audits finished!")
