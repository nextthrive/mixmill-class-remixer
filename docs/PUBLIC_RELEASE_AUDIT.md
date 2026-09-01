# MixMill Desktop 1.0.1 public-release audit

Audit date: 1 September 2026
Target: Windows x64, Windows 10 1809+ and Windows 11

## Verdict

**Open-source community release: ready after one clean-device acceptance pass.**

The app, portable package, NSIS installer, upgrade, uninstall, security, and
dependency checks pass locally. Authenticode is optional: current artifacts are
honestly marked `signed_release: false`, and release instructions disclose the
expected Windows SmartScreen warning.

Overall engineering readiness: **96/100**.

| Area | Score | Evidence |
|---|---:|---|
| Reliability | 96 | Repeat detection, scan, mix creation, export, download, ZIP, PDF, backup, and recovery tests pass |
| Security | 96 | Loopback/session controls, path containment, read-only media, limits, and dependency audit pass |
| Accessibility | 92 | Labels, live regions, focus states, reduced motion, and keyboard controls |
| Responsive UI | 94 | Phone through wide desktop, coarse-pointer, and short-landscape rules |
| Distribution | 96 | AGPL source, free NSIS tooling, versioned artifacts, checksums, notices, and lifecycle pass |

## Remaining P2 acceptance work

Test the actual hosted unsigned installer on clean Windows 10 and Windows 11
devices, including one without WebView2. Confirm the SmartScreen instructions
match current Windows behavior and every hosted hash matches `SHA256SUMS.txt`.

## Release controls

- MixMill source is licensed AGPL-3.0-or-later and `LICENSE` ships with portable
  and installed copies.
- NSIS 3.12 replaces Inno Setup; it is free for any use and SHA-256 pinned.
- The unsigned community path runs the same installer lifecycle suite as the
  optional signed path.
- Manifest records version, license, distribution type, sizes, hashes, and true
  signing state.
- The pinned FFmpeg corresponding-source archive is produced and checksummed
  with every build.
- Privacy, support, security, and third-party notices ship beside the executable.
- App data remains under `%LOCALAPPDATA%\MixMill` across upgrade and uninstall.

## Verification evidence

- Python compile: pass
- `tests/desktop_smoke.py`: pass
- `tests/security.py`: pass
- `tests/smoke.py`: pass
- Packaged `MixMill.exe --smoke-test`: pass
- Installer install/launch/upgrade/uninstall: pass
- `pip check`: pass
- pinned Windows requirements vulnerability audit: no known vulnerabilities
- SHA-256 artifact recomputation: pass
- Windows file/product version: `1.0.1`: pass

## Publish gate

1. Build from the exact public source commit.
2. Run automated verification and clean-device acceptance.
3. Tag that commit `v1.0.1` and create a GitHub Release.
4. Attach installer, portable ZIP, FFmpeg source ZIP, manifest, and checksums.
5. Clearly label the binaries unsigned and explain SmartScreen verification.
6. Link license, source, privacy, support, security, and third-party notices.

Optional Authenticode signing can be added later without changing the app or its
runtime performance.
