# MixMill Desktop privacy

MixMill Desktop is a local application. It has no account system, advertising,
analytics, telemetry, crash-reporting service, or cloud sync. MixMill does not
upload your media, track markers, mixes, exports, or usage data.

The application server listens only on the local loopback interface
(`127.0.0.1`) and uses a random session secret for each launch. Your selected
media library is read as input; MixMill stores its own state separately under
`%LOCALAPPDATA%\MixMill`:

- `settings.json` — selected media-folder path;
- `data\mixmill.db` and `data\backups` — library metadata, mixes, and backups;
- `data\exports`, caches, and temporary files — generated output;
- `logs\desktop.log` — diagnostics, which may include local file paths and
  release titles;
- `webview` — local Microsoft Edge WebView2 browser data.

Download actions open a Windows Save As dialog. The chosen copy is written to
the destination you select; completed exports also remain in MixMill's local
data until you delete them.

MixMill itself needs no internet connection after installation. If Microsoft
Edge WebView2 is missing, the installer runs Microsoft's bootstrapper, which
downloads that prerequisite from Microsoft. WebView2 and Windows may manage
their own updates under Microsoft's settings and privacy terms.

Uninstalling MixMill preserves `%LOCALAPPDATA%\MixMill` to prevent accidental
loss. To erase all MixMill data, uninstall the app, then manually delete that
exact folder after making any backup you want to keep.

Last updated: 30 August 2026.
