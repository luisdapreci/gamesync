import os
import shutil
from pathlib import Path
import sys

def main():
    config_dir = Path.home() / ".gamesync"
    
    print(f"Checking for GameSync state directory at: {config_dir}")
    
    if not config_dir.exists():
        print("No GameSync state found. The system is already clean.")
        return

    print("WARNING: This will permanently delete all GameSync configuration,")
    print("known peers, sync history, staged conflicts, and local backups.")
    print("Your actual game save files will NOT be affected.\n")
    
    confirm = input("Are you sure you want to completely reset GameSync? (y/N): ")
    if confirm.lower() not in ('y', 'yes'):
        print("Operation cancelled.")
        return
        
    try:
        shutil.rmtree(config_dir)
        print(f"Successfully deleted {config_dir}")
        print("GameSync state has been completely reset. You can now start from scratch.")
    except Exception as e:
        print(f"Error occurred while deleting state directory: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
