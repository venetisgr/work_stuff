r"""Version 2: digest of a SharePoint folder, read directly through Microsoft Graph.

Usage: python online_digest.py --folder temp-folder-1 --days 14   (see --help for all options)
"""

from sharepoint_digest.cli import main_online

if __name__ == "__main__":
    raise SystemExit(main_online())
