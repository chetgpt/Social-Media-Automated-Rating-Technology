"""Full Page ENGAGE; the older linkedin_workflow.py remains collection-only."""
import sys
from engage_social import main

if __name__ == "__main__":
    if any(arg == "--platform" or arg.startswith("--platform=") for arg in sys.argv[1:]):
        sys.exit("linkedin_engage.py has a fixed linkedin platform")
    sys.exit(main(["--platform", "linkedin", *sys.argv[1:]]))
