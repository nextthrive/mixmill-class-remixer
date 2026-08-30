# Security policy

Security fixes target the current MixMill Desktop release on supported Windows
versions. Older builds may be asked to upgrade before a report is investigated.

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/nextthrive/mixmill-class-remixer/security/advisories/new).
Include the affected version, impact, and minimal reproduction. Do not send
copyrighted media, databases, session URLs, or secrets. Allow time for a fix
before public disclosure.

MixMill's desktop trust boundary is local-only: the backend binds to
`127.0.0.1`, session requests require a random launch secret, state changes
require an integrity header, and the selected media tree is treated as
read-only input. Generated data lives under `%LOCALAPPDATA%\MixMill`.
