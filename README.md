# Fine Watch 罰單監察

Checks the Macau PSP online ticket service
(<https://www.fsm.gov.mo/webticket/Webform1.aspx?carClass=L&Lang=C>) every
30 minutes for your car plates. When the result is **有違例紀錄**, your phone
gets a push notification through the free **ntfy** app. While the record stays,
you're reminded again on a schedule you choose (default: once a day), and you
get an "all clear" message when it disappears.

Runs free on GitHub: GitHub Actions does the checking, GitHub Pages hosts the dashboard.

| File | What it does |
|---|---|
| `checker/check.py` | Looks up each plate, writes `docs/status.json`, sends pushes |
| `.github/workflows/check.yml` | Runs the checker every 30 min, on "Check now", and when plates change |
| `docs/index.html` | The dashboard website |
| `docs/config.json` | Plates to watch + reminder interval |

## One-time setup (about 5 minutes)

### 1. Phone
Install **ntfy** ([iPhone](https://apps.apple.com/app/ntfy/id1625396347) ·
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
tap **+**, and subscribe to your topic (keep it secret, since anyone who knows it can read it):

```
fsm-fines-48157d91549a
```

### 2. GitHub
1. Make a free account at <https://github.com/signup>.
2. Create a **public** repository named `test01` (public is needed for free Pages and unlimited Actions minutes).
3. Upload everything in this folder. If you upload through the website, note that
   `.github` is hidden in Finder: press **⌘ ⇧ .** to show it.
4. **Settings → Secrets and variables → Actions → New repository secret**:
   name `NTFY_TOPIC`, value `fsm-fines-48157d91549a`.
5. **Settings → Pages**: Source **Deploy from a branch**, branch `main`, folder `/docs`, then Save.
6. **Actions** tab: if asked, click **I understand… enable workflows**, then open
   **Check for fines → Run workflow** to run the first check.

Your dashboard is at `https://<your-username>.github.io/test01/#topic=fsm-fines-48157d91549a`
(open that link once on each device; it remembers the topic).

### 3. Optional: change plates and use "Check now" from the dashboard
Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new)
limited to the `test01` repo with **Contents: Read and write** and
**Actions: Read and write**, then paste it in the dashboard's ⚙︎ Settings.
It's kept only in that browser. Without it, the dashboard is view-only, and you can
change plates by editing `docs/config.json` on GitHub.

## Notes
- The PSP page only says whether a plate has records; the details need the owner's ID,
  so Fine Watch doesn't fetch them. Tap the notification to open the FSM site.
- GitHub may run scheduled checks a few minutes late during busy periods.
- If the FSM site can't be reached for about 4 hours in a row, you get one low-priority warning.
