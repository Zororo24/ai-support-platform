"""A tiny website with a chat box, showing how a company plugs an agent into its own site.

    AGENT_API_KEY=sk_live_... AGENT_ID=<agent id> AGENT_BASE_URL=http://localhost:8000 python server.py

then open http://localhost:8080. The browser only talks to THIS server; this server holds the API
key and talks to the platform. It shows the four things a real integration has to handle:

  * showing replies, and Yes/No buttons when the agent asks the customer to confirm,
  * doing the work itself when the agent asks (status `action_required`), with this site's own
    credentials, then sending the outcome back,
  * telling the customer when their request is waiting for a person (`pending_approval`) and
    receiving the decision later by webhook (POST /webhooks, signature checked),
  * recognising a hand-off to a human.

Standard library plus the Python client in ../../clients/python (which needs httpx).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "python"))

from agent_platform import AgentPlatform, AgentPlatformError, verify_webhook  # noqa: E402

HERE = Path(__file__).parent
API_KEY = os.environ.get("AGENT_API_KEY", "")
AGENT_ID = os.environ.get("AGENT_ID", "")
BASE_URL = os.environ.get("AGENT_BASE_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

agents = AgentPlatform(api_key=API_KEY, agent_id=AGENT_ID, base_url=BASE_URL)

# What this site can do for the agent. Each takes the arguments the AI chose (already checked
# against the schema you defined in the dashboard) and returns the data the AI may use.
def issue_refund(arguments: dict) -> dict:
    print(f"[site] issuing refund for {arguments}")  # call your payments system here
    return {"refund_id": "RF-DEMO-1", "status": "issued"}


ACTIONS = {"issue_refund": issue_refund}


def finish_actions(reply):
    """Keep doing what the agent asks until it has an answer for the customer."""
    while reply.status == "action_required":
        request = reply.action_request
        handler = ACTIONS.get(request["name"])
        try:
            result = handler(request["arguments"]) if handler else None
            if handler is None:
                raise RuntimeError(f"this site cannot do {request['name']}")
            reply = agents.send_tool_result(reply.session_id, request["call_id"], result=result)
        except Exception as exc:  # tell the agent it failed, so it can say so
            reply = agents.send_tool_result(reply.session_id, request["call_id"], error=str(exc)[:300])
    return reply


def present(reply) -> dict:
    return {
        "session_id": reply.session_id,
        "status": reply.status,
        "reply": reply.reply,
        "citations": [c["title"] for c in reply.citations],
        "confirmation": reply.pending_confirmation,
        "handoff": reply.handoff is not None,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            page = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        raw = self._body()
        if self.path == "/webhooks":
            return self._webhook(raw)
        try:
            data = json.loads(raw or b"{}")
            # In a real site the customer id comes from YOUR login, never from the browser.
            customer = {"end_user_id": "demo-customer", "end_user_attributes": {"customer_id": "cust_1"}}
            if self.path == "/api/chat":
                reply = agents.send(str(data["message"]), session_id=data.get("session_id"), **customer)
            elif self.path == "/api/confirm":
                reply = agents.confirm(data["session_id"], data["confirmation_id"], bool(data["approve"]))
            else:
                return self._json(404, {"error": "not found"})
            self._json(200, present(finish_actions(reply)))
        except AgentPlatformError as exc:
            self._json(502 if exc.status_code >= 500 else exc.status_code, {"error": exc.message, "code": exc.code})
        except (KeyError, ValueError):
            self._json(400, {"error": "bad request"})

    def _webhook(self, raw: bytes) -> None:
        header = self.headers.get("X-Webhook-Signature", "")
        if not WEBHOOK_SECRET or not verify_webhook(WEBHOOK_SECRET, header, raw):
            return self._json(401, {"error": "bad signature"})
        event = json.loads(raw)
        # Events are delivered at least once: use event["id"] to ignore repeats.
        print(f"[webhook] {event['type']} {json.dumps(event['data'])[:200]}")
        if event["type"] == "handoff.requested":
            print("[site] a customer needs a person; open a ticket / page the team here")
        self._json(200, {"ok": True})


if __name__ == "__main__":
    if not API_KEY or not AGENT_ID:
        sys.exit("Set AGENT_API_KEY and AGENT_ID (see the Integration tab of your agent).")
    print(f"Chat site on http://localhost:{PORT}  (agent {AGENT_ID} at {BASE_URL})")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
