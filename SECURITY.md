# Security policy

## Reporting a vulnerability

Please report security issues **privately** using GitHub's private vulnerability reporting (the repository's **Security** tab → **Report a vulnerability**), not in a public issue.

Include what you found, how to reproduce it, and the version or commit you tested. **Never include real Garmin tokens, passwords or personal health data in a report** — redact them or use placeholder values.

## What counts as a vulnerability here

This project makes specific promises (see the README). A report is in scope if it shows that the server:

- accepts, logs, stores or transmits a Garmin password;
- sends data anywhere other than Garmin's own hosts (`*.garmin.com`) and the MCP client that launched it;
- can modify, upload or delete anything in a Garmin account (for example via `call_readonly`);
- leaks account or device identifiers, or GPS coordinates, in a way the scrubber is documented to prevent;
- writes token material to a place it should not (logs, error messages, the repository).

## Known limitations (not vulnerabilities)

These are documented in the README and are not treated as security bugs:

- The scrubber removes *known* identifier keys; raw-payload tools can carry keys it does not know.
- The token file in `~/.garminconnect` is readable by anything running as your user.
- The `garminconnect` library uses undocumented Garmin endpoints and may break or change behaviour.

## Supported versions

Only the latest commit on the default branch is supported.
