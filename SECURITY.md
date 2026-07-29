# Security Policy

## Data boundary

Dian Agent reads visible data from pages the user is already signed in to. It does not read browser cookies and sends snapshots only to `127.0.0.1` by default.

Never commit files under `bridge/data/`, generated reports, browser profiles, extension signing keys, or exported shop data. These paths are excluded by `.gitignore`.

Feishu and DingTalk robot Webhooks are secrets. They are accepted only for the official HTTPS hosts, stored locally in `bridge/data/integrations.json`, and never returned in full by the local service. Do not paste a real Webhook into an issue, screenshot, test fixture, or committed configuration file. If a Webhook is exposed, delete or rotate the robot immediately in the corresponding group settings.

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities involving data exposure, permission scope, local bridge access, or unsafe browser actions. Avoid including real shop, customer, order, account, or advertising data in reports.

## Execution boundary

The supervised executor is limited to a single Qianchuan budget reduction. It requires a fresh page reread, an exact final confirmation phrase, a 60-second single-use grant, a unique account and plan match, a readable current budget, and a reduction of no more than 30%. It submits only when exactly one supported platform confirmation button is present, records the platform receipt, then rereads the page to verify the target value.

Budget increases, bulk changes, plan pause/resume, bids, inventory, orders, refunds and account funds remain disabled. A missing or ambiguous selector, account, plan, value, button or success receipt stops the executor rather than guessing or retrying.

Budget drafts are blocked unless they bind a selected account, a platform plan ID, a fresh high-quality snapshot, a readable current value, and a policy-compliant target value. A missing budget must never fall back to a pause action.
