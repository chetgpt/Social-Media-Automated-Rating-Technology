---
name: social-browser-checker
description: >-
  Use this skill to automatically open other social platforms (YouTube, Instagram, Facebook, X) in the Edge browser and check their login status.
---

# Social Browser Checker

When you are asked to check the login status of social media platforms or open them to log in automatically, follow this process:

## Process

1. **Verify the status first**:
   Run `C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe social_browser.py status`.
   If it's already logged in to all platforms, you are done.

2. **Open the browser visibly**:
   The browser must be opened interactively to allow automatic login (using saved profile sessions) for platforms like YouTube, Instagram, Facebook, and X.
   Execute this PowerShell command to create a scheduled task that opens the tabs visibly:
   ```powershell
   cmd.exe /c 'schtasks /create /tn "OpenEdge" /tr "\"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe\" --profile-directory=\"Profile 7\" https://youtube.com https://instagram.com https://facebook.com https://x.com" /sc once /st 00:00 /it /f'
   ```

3. **Run the task**:
   ```powershell
   schtasks /run /tn "OpenEdge"
   ```

4. **Clean up the task**:
   ```powershell
   schtasks /delete /tn "OpenEdge" /f
   ```

5. **Wait for automatic login**:
   Wait a few seconds (around 5-10 seconds) for the pages to load and the session cookies to refresh.

6. **Close the visible browser and restart the background controller**:
   Close the interactive Edge window and ensure the automation controller connects:
   ```powershell
   Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
   C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe social_browser.py stop
   C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe social_browser.py start
   ```

7. **Check the status again**:
   Run `C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe social_browser.py status` to confirm that the platforms are now logged in.
