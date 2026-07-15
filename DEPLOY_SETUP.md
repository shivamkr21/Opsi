# Deploy laptop setup (Windows Server 2012 R2)

One-time setup for the second laptop (the one with the static public IP)
so that merges to `4SightAI/Aggi` main automatically update and restart it.

## 1. Provision the Aggi checkout

The code has a hardcoded path (`S3_User_Query\Step4_QueryVectorDB.py` uses
`C:\Aggi\AI\chroma_db`), so the checkout **must** live at exactly `C:\Aggi`.

```powershell
# Install Python 3.11.9 first: https://www.python.org/downloads/release/python-3119
git clone https://github.com/4SightAI/Aggi.git C:\Aggi
cd C:\Aggi
git remote add upstream https://github.com/4SightAI/Aggi.git   # if not already origin
python -m venv myvenv
myvenv\Scripts\pip install -r C:\Opsi\requirements.txt
```

Then manually copy these (they're gitignored, so `git pull` never fetches
them — copy from the dev machine however you like: USB, network share,
robocopy over LAN):

- `C:\Aggi\AI\chroma_db` (~764 MB)
- `C:\Aggi\S2_OT_Embedding\EmbeddingModel` (~5.2 GB)
- `C:\Aggi\S1_OT_Chunking\PDF` (only if S1 scripts will ever run here; not
  required for serving the chat app)

Also open `C:\Aggi\S4_WebApp\medassist\settings.py` and add this laptop's
address to `ALLOWED_HOSTS` (and set `DEBUG = False` before this is treated
as a real production box) — not automated by this pipeline.

## 2. Copy the DevOps tooling

Copy this entire `C:\Opsi` folder to the same path, `C:\Opsi`, on the
deploy laptop. It contains `webhook_listener.py`, `deploy.py`,
`deploy_manifest.txt`, and `requirements.txt`.

Edit `C:\Opsi\deploy_manifest.txt` any time you want to change which
top-level Aggi folders get deployed — `deploy.py` re-reads it on every run.

## 3. Configure environment variables

The webhook listener needs a shared secret (also used in step 4). Set
these as machine-level environment variables (System Properties →
Environment Variables), or set them in the Task Scheduler action (step 5):

| Variable         | Value                                         |
|------------------|------------------------------------------------|
| `WEBHOOK_SECRET` | a long random string, e.g. from `openssl rand -hex 32` |
| `WEBHOOK_PORT`   | the port your router already forwards to this laptop |
| `AGGI_DIR`       | `C:\Aggi` (only needed if you ever move the checkout) |

## 4. Configure the GitHub webhook

Requires admin/webhook permission on `4SightAI/Aggi`.

1. Go to `https://github.com/4SightAI/Aggi/settings/hooks` → **Add webhook**.
2. Payload URL: `http://<your-static-public-ip>:<WEBHOOK_PORT>/deploy-hook`
3. Content type: `application/json`
4. Secret: same value as `WEBHOOK_SECRET` above.
5. Events: "Just the push event".
6. Save, then check the **Recent Deliveries** tab — GitHub sends a `ping`
   event immediately, which `webhook_listener.py` answers with `200 pong`
   once it's running (step 5).

## 5. Run the webhook listener at boot (Task Scheduler)

No extra software (like NSSM) needed — Server 2012 R2 has Task Scheduler
built in.

1. Open Task Scheduler → **Create Task** (not "Basic Task", so you get the
   full options).
2. **General** tab: name it `Aggi Webhook Listener`; select "Run whether
   user is logged on or not"; check "Run with highest privileges" only if
   your firewall rule requires it.
3. **Triggers** tab: New → "At startup".
4. **Actions** tab: New → Action "Start a program":
   - Program/script: `C:\Aggi\myvenv\Scripts\python.exe`
   - Arguments: `C:\Opsi\webhook_listener.py`
   - Start in: `C:\Opsi`
5. **Settings** tab: check "If the task fails, restart every: 1 minute",
   and uncheck any "stop if runs longer than" limit (this task runs forever).
6. Save, then right-click the task → **Run** to start it immediately without
   rebooting.

Open the firewall for `WEBHOOK_PORT` if it isn't already:

```powershell
netsh advfirewall firewall add rule name="Aggi Webhook" dir=in action=allow protocol=TCP localport=<WEBHOOK_PORT>
```

## 6. Verify end to end

1. `C:\Opsi\logs\webhook.log` should show `listening on 0.0.0.0:<port>/deploy-hook`
   after the task starts.
2. GitHub's webhook "Recent Deliveries" should show `200` for the `ping`.
3. Merge a trivial PR into `4SightAI/Aggi` main and confirm:
   - `C:\Opsi\logs\webhook.log` logs "valid push ... triggering deploy"
   - `C:\Opsi\logs\deploy.log` shows sparse-checkout → pull → pip → migrate → restart, ending in "deploy finished successfully"
   - `C:\Opsi\logs\runserver.log` shows the Django dev server starting
   - The chat app responds at `http://<static-public-ip>:8000/` (or whatever
     `RUNSERVER_BIND` you configured)

## Manual testing without waiting for a real merge

From the deploy laptop (or the dev machine, pointed at a scratch clone):

```powershell
# Dry run - logs every step, changes nothing
C:\Aggi\myvenv\Scripts\python.exe C:\Opsi\deploy.py --dry-run

# Real run
C:\Aggi\myvenv\Scripts\python.exe C:\Opsi\deploy.py
```
