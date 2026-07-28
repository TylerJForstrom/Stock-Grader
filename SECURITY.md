# Security Policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch. Older releases should be
upgraded before a report is evaluated.

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| Earlier versions | No |

## Reporting a vulnerability

Please do not disclose an unpatched vulnerability in a public issue. Use GitHub's private
vulnerability-reporting flow on the repository's **Security** tab when it is available. If that
flow is unavailable, open an issue that contains no vulnerability details and asks the maintainer
to enable a private reporting channel.

Include the affected version or commit, impact, reproduction steps, and any suggested remediation.
Remove API keys, SEC contact information, portfolio data, and other secrets from the report.

You should receive an acknowledgment within seven days and a status update within fourteen days.
Timelines for a fix or coordinated disclosure depend on severity and complexity. Please allow a
reasonable remediation window before public disclosure.

## Scope

Reports involving unsafe file or cache access, credential exposure, dependency compromise, code
execution, data-integrity failures, or network-boundary bypasses are in scope. Incorrect investment
predictions and ordinary differences in model judgment are not security vulnerabilities, though
reproducible data-integrity defects are welcome as regular bug reports.
