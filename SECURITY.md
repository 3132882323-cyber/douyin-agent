# Security Policy

## Data boundary

Dian Agent reads visible data from pages the user is already signed in to. It does not read browser cookies and sends snapshots only to `127.0.0.1` by default.

Never commit files under `bridge/data/`, generated reports, browser profiles, extension signing keys, or exported shop data. These paths are excluded by `.gitignore`.

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities involving data exposure, permission scope, local bridge access, or unsafe browser actions. Avoid including real shop, customer, order, account, or advertising data in reports.

## Execution boundary

The open-source version may create and locally confirm Qianchuan operation drafts, but its execution layer is disabled. Confirmation only writes a local audit record. It does not click or submit Qianchuan pages, or change budgets, inventory, plan status, orders, refunds, or account funds.

Budget drafts are blocked unless they bind a selected account, a platform plan ID, a fresh high-quality snapshot, a readable current value, and a policy-compliant target value. A missing budget must never fall back to a pause action.
