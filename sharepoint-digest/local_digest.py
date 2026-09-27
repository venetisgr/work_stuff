r"""Version 1: digest of a folder on this computer, such as a SharePoint folder synced with OneDrive.

Usage: python local_digest.py --folder "C:\Users\you\...\Temp Folder 1" --days 14   (see --help for all options)
"""

from sharepoint_digest.cli import main_local

if __name__ == "__main__":
    raise SystemExit(main_local())
