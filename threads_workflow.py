"""Threads-first shortcut for the isolated social engagement CLI."""
import sys
from engage_social import main

if __name__ == "__main__":
    if any(arg == "--platform" or arg.startswith("--platform=") for arg in sys.argv[1:]):
        sys.exit("threads_workflow.py has a fixed threads platform")
    sys.exit(main(["--platform", "threads", *sys.argv[1:]]))
