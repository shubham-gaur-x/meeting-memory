# Real-time push via Composio Gmail trigger

Polling every 5 minutes is fine, but if you want sub-minute latency you can have
Composio push new Gmail messages to a local webhook.

## 1. Expose a local webhook
Use `cloudflared tunnel` (free) or `ngrok`:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:5055
# copy the printed https://<random>.trycloudflare.com URL
```

## 2. Run a tiny FastAPI receiver
Save as `scripts/webhook.py`:

```python
import os, sys
from pathlib import Path
from fastapi import FastAPI, Request
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.composio_gmail import ComposioGmail
from lib.classifier import classify
from lib.extractor import extract
from lib.obsidian_writer import write_meeting
from lib.state import State

app = FastAPI()
gmail = ComposioGmail(entity_id=os.getenv("COMPOSIO_ENTITY_ID", "default"))
vault = Path(os.getenv("VAULT_PATH") or "vault").resolve()
state = State("state.json")

@app.post("/gmail")
async def gmail_event(req: Request):
    payload = await req.json()
    mid = payload.get("messageId") or payload.get("data", {}).get("messageId")
    if not mid or state.has(mid):
        return {"status": "skipped"}
    msg = gmail.fetch_message(mid)
    if not classify(msg).is_meeting:
        return {"status": "not_meeting"}
    ex = extract(msg)
    if ex.confidence < 0.3:
        return {"status": "low_confidence"}
    path = write_meeting(vault, msg, ex)
    state.mark(mid, str(path), kind=ex.kind, confidence=ex.confidence)
    state.save()
    return {"status": "written", "path": str(path)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5055)
```

```bash
pip install fastapi uvicorn
python scripts/webhook.py
```

## 3. Register the trigger in Composio
1. In the Composio dashboard, **Triggers → Gmail → New Gmail Message**.
2. Set the webhook URL to `https://<random>.trycloudflare.com/gmail`.
3. Save. Send yourself a test email — it should appear in `vault/Meetings/` within seconds.
