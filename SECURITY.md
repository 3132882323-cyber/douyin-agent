# Security Policy

## Data boundary

Dian Agent reads visible data from pages the user is already signed in to. It does not read browser cookies and sends snapshots only to `127.0.0.1` by default.

Never commit files under `bridge/data/`, generated reports, browser profiles, extension signing keys, or exported shop data. These paths are excluded by `.gitignore`.

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities involving data exposure, permission scope, local bridge access, or unsafe browser actions. Avoid including real shop, customer, order, account, or advertising data in reports.

## Execution boundary

The open-source version is read-only. Recommendations do not automatically change budgets, inventory, plan status, orders, refunds, or account funds.

