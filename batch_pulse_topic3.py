import subprocess
import json
import sys

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

topics = ["Seaside band indonesia", "rumahsakit band", "The Cottons band", "Alkateri band", "Thee Marloes"]
summary = []

for topic in topics:
    print(f"--- Pulsing Topic: {topic} ---")
    try:
        res = subprocess.run(
            ["C:\\Users\\DELL\\AppData\\Local\\Programs\\Python\\Python311\\python.exe", "quick_audit_tiktok.py", "collect", "--topic", topic, "--posts", "3"],
            capture_output=True, text=True, timeout=240, encoding='utf-8'
        )
        
        if res.returncode != 0:
            print(f"[{topic}] Error: {res.stderr[:200]}")
            summary.append(f"{topic}: Error/Not found")
            continue
        
        lines = res.stdout.strip().split('\n')
        data = None
        for line in reversed(lines):
            if line.startswith('{') and 'snapshot' in line:
                try:
                    data = json.loads(line)
                    break
                except:
                    pass
                    
        if data and 'snapshot' in data:
            posts = data['snapshot'].get('posts', [])
            print(f"[{topic}] Found {len(posts)} posts.")
            summary.append(f"{topic}: Found {len(posts)} posts")
            for p in posts:
                print(f" - Creator: @{p.get('creator', 'unknown')}")
                print(f" - Caption: {p.get('caption', '')[:100]}...")
        else:
            print(f"[{topic}] Could not parse output.\n")
            summary.append(f"{topic}: Parse error")
            
    except subprocess.TimeoutExpired:
        print(f"[{topic}] Timeout.\n")
        summary.append(f"{topic}: Timeout")
    except Exception as e:
        print(f"[{topic}] Exception: {e}\n")
        summary.append(f"{topic}: Exception")

print("\n--- BATCH SUMMARY ---")
for s in summary:
    print(s)
