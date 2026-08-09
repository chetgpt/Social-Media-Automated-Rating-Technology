"""Browser-related utility functions for the TikTok scraper."""
import subprocess
import socket
import time
import os

def close_edge_tasks():
    """Terminates all running Microsoft Edge processes.

    Returns:
        bool: True if successful, False if an exception occurred
    """
    try:
        cmd = "taskkill /F /IM msedge.exe /T"
        subprocess.run(cmd, shell=True, check=True)
        print("All Microsoft Edge tasks have been terminated.")
        return True
    except Exception as e:
        print("Warning: Could not kill Edge tasks. Error:", e)
        return False

def wait_for_port(host, port, timeout=90):
    """Waits for a network port to become available.

    Args:
        host: The host to connect to
        port: The port to connect to
        timeout: Maximum time to wait in seconds

    Returns:
        bool: True if the port became available, False if it timed out
    """
    start_time = time.time()
    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            if time.time() - start_time > timeout:
                return False
            time.sleep(1)

def get_edge_executable_path():
    """Finds the Microsoft Edge executable path.

    Returns:
        str: Path to the Edge executable, or None if not found
    """
    # Common locations for Edge
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    ]

    for path in edge_paths:
        if os.path.exists(path):
            return path

    return None