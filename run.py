import os
import sys

# Ensure parent directory is in path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from gamesync.main import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass  # Ctrl+C — uvicorn already logged the shutdown
