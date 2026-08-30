# MixMill Desktop support

## Supported systems

MixMill Desktop 1.0 supports 64-bit Windows 10 version 1809 or newer and
Windows 11. No Python, Docker, Git, or terminal is required on the target PC.

## Before reporting a problem

1. Note the MixMill version shown in the window title.
2. Reproduce the problem once, then close MixMill.
3. Copy `%LOCALAPPDATA%\MixMill\logs\desktop.log` somewhere safe.
4. Include Windows version, what you clicked, what you expected, and the exact
   error shown. Say whether the installer or portable build was used.
5. Remove personal folder names or release titles from the log if needed.

Report ordinary bugs through [GitHub Issues](https://github.com/nextthrive/mixmill-class-remixer/issues).
Do not post media files, choreography notes, database backups, or desktop-session
URLs publicly.

## Recovery

- Data and automatic database backups live in `%LOCALAPPDATA%\MixMill\data`.
- Reinstalling or upgrading does not remove that directory.
- **MixMill - Change Media Folder** in the Start Menu selects a moved library.
- If startup reports database damage, preserve the whole data folder before
  replacing `mixmill.db` with a recent file from `data\backups`.

For a suspected security problem, use
[private vulnerability reporting](https://github.com/nextthrive/mixmill-class-remixer/security/advisories/new).
Include the affected version and minimal reproduction steps; do not publish an
active exploit first.
