"""End-to-end check against a running memory-core, over REST and MCP.

Uses agent 'smoke' (scopes project:smoke-*, agent:smoke) so it never touches real memories.
Usage: MC_URL=http://127.0.0.1:8420 MC_TOKEN=<smoke token> python smoke.py
Exit code 0 = every check passed.
"""

import base64
import hashlib
import json
import os
import sys
import time

import httpx

URL = os.environ.get("MC_URL", "http://127.0.0.1:8420")
H = {"Authorization": f"Bearer {os.environ['MC_TOKEN']}"}
SCOPE = "project:smoke-test"
failures = 0


def check(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def post(path, body, headers=H):
    t0 = time.perf_counter()
    r = httpx.post(URL + path, json=body, headers=headers, timeout=60)
    return r, (time.perf_counter() - t0) * 1000


check("healthz", httpx.get(URL + "/healthz").json().get("ok") is True)
check("no token is rejected", httpx.post(URL + "/api/v1/recall", json={"query": "x"}).status_code == 401)

seed = [
    "StockTracker runs as a systemd service with uvicorn on port 8765.",
    "The BCA categorizer backend listens on port 8000.",
    "Jellyfin runs in Docker on port 8096.",
    "To add a *.home service, edit ~/caddy/Caddyfile and restart the caddy container.",
]
r, ms = post("/api/v1/remember", {"facts": seed, "scope": SCOPE, "force": True})
saved = {s["text"]: s["id"] for s in r.json().get("saved", [])}
check("remember 4 facts", len(saved) == 4, f"{ms:.0f} ms")
check("a project with no branch asks for one", "branch_needed" in r.json(), json.dumps(r.json())[:160])
tracker_id = saved.get(seed[0])

r, _ = post("/api/v1/remember", {"facts": [seed[1]], "scope": SCOPE})
check("exact duplicate skipped", r.json().get("skipped_duplicates", [{}])[0].get("duplicate_of") == saved.get(seed[1]))

near = "StockTracker is served by uvicorn on port 8765 as a systemd service."
r, _ = post("/api/v1/remember", {"facts": [near], "scope": SCOPE})
held = r.json().get("held_near_duplicates", [])
sim = held[0]["similar"][0]["similarity"] if held else None
check("paraphrase held as near-duplicate", bool(held), f"similarity={sim} body={json.dumps(r.json())[:160] if not held else ''}")

r, _ = post("/api/v1/remember", {"facts": ["Staging DB password: hunter22 for the smoke test."], "scope": SCOPE})
check("password redacted", "password" in r.json().get("redacted", []), json.dumps(r.json())[:160])
leftovers = [s["id"] for s in r.json().get("saved", [])] + [i for t, i in saved.items() if t != seed[0]]

r, _ = post("/api/v1/remember", {"facts": ["StockTracker moved to port 8767."], "scope": SCOPE})
upd = r.json().get("saved", [{}])[0]
check("update saved with the old fact as related", tracker_id in [x["id"] for x in upd.get("related", [])],
      json.dumps(upd.get("related", []))[:120])
post("/api/v1/forget", {"memory_id": upd.get("id"), "reason": "smoke test: only checking related"})

for q in ["which port does stocktracker run on", "stocktracker jalan di port berapa?", "how do I expose a new service on .home"]:
    r, ms = post("/api/v1/recall", {"query": q, "scope": SCOPE, "k": 3})
    top = r.json()["results"][0] if r.json().get("results") else {}
    want = "Caddyfile" if ".home" in q else "8765"
    check(f"recall: {q!r}", want in top.get("text", ""), f"{ms:.0f} ms, matched={top.get('matched')}, top={top.get('text','')[:60]!r}")

r, _ = post("/api/v1/remember", {"facts": ["StockTracker moved to port 8766."], "scope": SCOPE, "supersedes": tracker_id, "verified": True})
new_id = r.json().get("saved", [{}])[0].get("id")
check("supersede retires the old fact", r.json().get("retired") == tracker_id)
r, _ = post("/api/v1/recall", {"query": "stocktracker port", "scope": SCOPE, "k": 5})
ids = [x["id"] for x in r.json()["results"]]
check("retired fact no longer recalled", tracker_id not in ids and new_id in ids)

r, _ = post("/api/v1/forget", {"memory_id": new_id, "reason": "smoke test cleanup"})
check("forget", r.json().get("retired") == new_id)
r, _ = post("/api/v1/remember", {"facts": ["should fail"], "scope": "global"})
check("smoke agent cannot write global", r.status_code == 400, r.json().get("error", ""))

# branches: a project no domains.json pattern matches is filed by an agent; 'other' clears it again
r, _ = post("/api/v1/branches/set", {"scope": SCOPE, "branch": "infra"})
check("branch set", r.json().get("status") == "active", json.dumps(r.json())[:160])
infra = next((b for b in httpx.get(URL + "/api/v1/branches", headers=H).json().get("branches", [])
              if b["name"] == "infra"), {})
check("branches list the filed project", any(p.startswith("smoke-test ") for p in infra.get("projects", [])),
      json.dumps(infra)[:160])
r, _ = post("/api/v1/branches/set", {"scope": SCOPE, "branch": "smoke-new"})
check("new branch without a description refused",
      r.status_code == 400 and "title and a description" in r.json().get("error", ""), json.dumps(r.json())[:160])
r, _ = post("/api/v1/branches/set", {"scope": SCOPE, "branch": "smoke-new", "title": "Smoke new",
                                      "description": "A branch only the smoke test creates."})
check("new branch with a description is created at once", r.json().get("status") == "created",
      json.dumps(r.json())[:160])
fresh = next((b for b in httpx.get(URL + "/api/v1/branches", headers=H).json().get("branches", [])
              if b["name"] == "smoke-new"), {})
check("the created branch holds the project", any(p.startswith("smoke-test ") for p in fresh.get("projects", [])),
      json.dumps(fresh)[:160])
r, _ = post("/api/v1/branches/review", {"branch": "smoke-new", "accept": True})
check("an active branch cannot be accepted again", r.status_code == 400, json.dumps(r.json())[:160])
r, _ = post("/api/v1/branches/review", {"branch": "smoke-new", "accept": False})
check("The user can turn a created branch down", r.json().get("status") == "rejected", json.dumps(r.json())[:160])
r, _ = post("/api/v1/branches/set", {"scope": SCOPE, "branch": "smoke-new", "title": "Smoke new",
                                      "description": "A branch only the smoke test creates."})
check("a turned-down branch cannot be created again",
      r.status_code == 400 and "turned down" in r.json().get("error", ""), json.dumps(r.json())[:160])
r, _ = post("/api/v1/branches/set", {"scope": SCOPE, "branch": "other"})
check("branch cleared", r.json().get("branch") == "other", json.dumps(r.json())[:160])

# --- library -----------------------------------------------------------------------------------
def lib_hash(item_type, title, summary, scope, body):  # as app/library.py and clients/library_sync.py
    return hashlib.sha256(json.dumps([item_type, title, summary, scope, body], ensure_ascii=False).encode()).hexdigest()


doc = {"title": "smoke-test/README.md", "summary": "Deploy notes for the zebracorn smoke service.", "scope": SCOPE,
       "body": "# Zebracorn\n\nThe zebracorn service deploys with `make zebracorn` on port 9911.\n\n## Rollback\n\n"
               "Run `make undo`.", "meta": {}}
doc["hash"] = lib_hash("doc", doc["title"], doc["summary"], doc["scope"], doc["body"])
r, _ = post("/api/v1/library/sync", {"type": "doc", "origin": "smoke",
                                     "manifest": [{"path": "smoke/README.md", "scope": SCOPE, "hash": doc["hash"]}]})
need = r.json().get("need", [])
check("library sync takes the manifest", r.status_code == 200 and r.json().get("found") == 1, json.dumps(r.json())[:160])
if need:
    r, ms = post("/api/v1/library/upsert", {"type": "doc", "items": [doc]})
    check("library upsert indexes the doc", len(r.json().get("saved", [])) == 1, f"{ms:.0f} ms {json.dumps(r.json())[:120]}")
r, ms = post("/api/v1/library/search", {"query": "how do I deploy zebracorn", "type": "doc", "scope": SCOPE, "k": 3})
hits = r.json().get("results", [])
check("library search finds the doc", bool(hits) and hits[0]["title"] == doc["title"], f"{ms:.0f} ms {json.dumps(hits)[:160]}")
lib_id = hits[0]["id"] if hits else 0
r = httpx.get(URL + "/api/v1/library/get", params={"id": lib_id}, headers=H)
check("library get returns the body", "port 9911" in r.json().get("body", ""), json.dumps(r.json())[:120])
bad = {"path": "x", "scope": "global", "hash": doc["hash"]}
r, _ = post("/api/v1/library/sync", {"type": "skill", "origin": "smoke", "manifest": [bad]})
check("smoke agent cannot sync global items", r.status_code == 400, r.json().get("error", ""))

# --- saved references (phone capture over Tailscale, review) ----------------------------------
# What Tailscale serve adds for the user's devices; capture needs it instead of a token.
TS = {"Tailscale-User-Login": os.environ.get("LIVE_VIEWER", "you@github"),
      "Host": "memory.your-tailnet.ts.net:8420"}
shot = base64.b64encode(open(os.path.join(os.path.dirname(__file__), "smoke_ref.png"), "rb").read()).decode()
capture = {"image": shot, "url": "https://example.com/smoke-ref/?utm_source=smoke", "note": "smoke test reference",
           "scope": SCOPE}
for old in post("/api/v1/library/search", {"query": "zebracorn", "type": "ref", "scope": SCOPE, "k": 20})[0].json().get(
        "results", []):  # a reference left by an interrupted run would turn this run's capture into a merge
    post("/api/v1/library/review", {"id": old["id"], "discard": True})
r = httpx.post(URL + "/api/v1/capture/ref", json=capture, headers=H, timeout=120)
check("capture refused without Tailscale identity", r.status_code == 403, str(r.status_code))
r = httpx.post(URL + "/api/v1/capture/ref", json=capture, headers=TS, timeout=120)
ref = r.json()
check("capture saves image + link, OCR reads it", r.status_code == 200 and ref.get("status") == "inbox"
      and ref.get("ocr_chars", 0) > 10, json.dumps(ref)[:200])
ref_id = ref.get("id")
r = httpx.post(URL + "/api/v1/capture/ref", json={**capture, "note": ""}, headers=TS, timeout=120)
check("same image again joins the same reference", r.json().get("id") == ref_id and r.json().get("merged"),
      json.dumps(r.json())[:160])
group = {"note": "smoke zebracorn group", "scope": SCOPE, "group": f"smoke-{time.time():.0f}"}
first = httpx.post(URL + "/api/v1/capture/ref", json={**group, "image": base64.b64encode(
    open(os.path.join(os.path.dirname(__file__), "smoke_ref.png"), "rb").read() + b"\0a").decode()}, headers=TS, timeout=120)
second = httpx.post(URL + "/api/v1/capture/ref", json={**group, "image": base64.b64encode(
    open(os.path.join(os.path.dirname(__file__), "smoke_ref.png"), "rb").read() + b"\0b").decode()}, headers=TS, timeout=120)
check("two images of one share join one reference", second.json().get("id") == first.json().get("id")
      and second.json().get("images") == 2, f"{first.text[:100]} | {second.text[:100]}")
post("/api/v1/library/review", {"id": first.json().get("id"), "discard": True})
r = httpx.get(URL + "/api/v1/library/inbox", headers=H)
check("inbox lists it", ref_id in [x["id"] for x in r.json().get("refs", [])], json.dumps(r.json())[:160])
check("web library refused without Tailscale identity", httpx.get(URL + "/api/v1/web/refs", headers=H).status_code == 403)
r = httpx.get(URL + "/api/v1/web/refs", params={"status": "inbox", "q": "zebracorn"}, headers=TS)
card = next((c for c in r.json().get("refs", []) if c["id"] == ref_id), {})
thumb = httpx.get(URL + f"/api/v1/web/media/{card['images'][0]['sha256']}?size=thumb", headers=TS) if card.get("images") else None
check("web library lists it with a thumbnail", bool(card) and thumb is not None and thumb.status_code == 200
      and thumb.headers.get("content-type") == "image/jpeg", json.dumps(card)[:160])
r, _ = post("/api/v1/library/search", {"query": "zebracorn rollout checklist", "type": "ref", "scope": SCOPE})
check("OCR text is searchable before review", r.json().get("results", [{}])[0].get("id") == ref_id,
      json.dumps(r.json())[:160])
r, _ = post("/api/v1/library/review", {"id": ref_id, "title": "Zebracorn rollout", "category": "planning",
                                       "description": "A rollout checklist for the zebracorn smoke service."})
check("review files it", r.json().get("status") == "reviewed" and r.json().get("category") == "planning",
      json.dumps(r.json())[:160])
r, _ = post("/api/v1/library/search", {"query": "zebracorn rollout", "category": "planning", "scope": SCOPE})
hit = (r.json().get("results") or [{}])[0]
check("reviewed reference found in its category", hit.get("id") == ref_id and hit.get("status") == "reviewed",
      json.dumps(hit)[:160])
r = httpx.get(URL + "/api/v1/library/categories", headers=H)
check("categories list with guides", any(c["name"] == "planning" and c["guide"] for c in r.json().get("categories", [])),
      json.dumps(r.json())[:160])

# --- MCP over Streamable HTTP ----------------------------------------------------------------
MH = {**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params, id_=1):
    r = httpx.post(URL + "/mcp", headers=MH, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, timeout=60)
    body = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    return r.status_code, json.loads(body) if body else {}


status, init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "smoke", "version": "0"}})
check("mcp initialize", status == 200 and "result" in init, json.dumps(init)[:160])
status, tl = rpc("tools/list", {}, 2)
names = sorted(t["name"] for t in tl.get("result", {}).get("tools", []))
check("mcp tools/list", names == ["library_categories", "library_file", "library_get", "library_inbox", "library_review",
                                  "library_search", "library_view", "memory_branch", "memory_branch_review",
                                  "memory_branches", "memory_forget", "memory_profile",
                                  "memory_recall", "memory_remember", "memory_timeline"], str(names))
status, call = rpc("tools/call", {"name": "memory_recall", "arguments": {"query": "jellyfin port", "scope": SCOPE, "k": 1}}, 3)
text = json.dumps(call.get("result", call))
check("mcp tools/call memory_recall", "8096" in text, text[:200])
status, call = rpc("tools/call", {"name": "library_search",
                                  "arguments": {"query": "zebracorn rollback", "scope": SCOPE, "k": 1}}, 4)
text = json.dumps(call.get("result", call))
check("mcp tools/call library_search", "smoke-test/README.md" in text, text[:200])
status, call = rpc("tools/call", {"name": "library_view", "arguments": {"item_id": ref_id}}, 5)
kinds = [c.get("type") for c in call.get("result", {}).get("content", [])]
check("mcp library_view returns the image", kinds == ["text", "image"], str(kinds))

# --- /live -----------------------------------------------------------------------------------
r = httpx.get(URL + "/live")
check("live page served without a token", r.status_code == 200 and "memory-core" in r.text)
script = next((part.split('"')[0] for part in r.text.split('src="')[1:] if part.startswith("/live/assets/")), None)
check("live page script served", bool(script) and httpx.get(URL + script).status_code == 200, str(script))
r = httpx.get(URL + "/api/v1/activity/stream", headers={**H, "Tailscale-User-Login": "you@github"})
check("activity feed refused without the Tailscale host", r.status_code == 403, str(r.status_code))
# What Tailscale serve sends: the viewer's login and the ts.net host
viewer = {"Tailscale-User-Login": os.environ.get("LIVE_VIEWER", "you@github"),
          "Host": "memory.your-tailnet.ts.net:8420"}
with httpx.stream("GET", URL + "/api/v1/activity/stream", headers=viewer, timeout=30) as r:
    first = ""
    for chunk in r.iter_text():  # padding comes first, then the hello event
        first += chunk
        if "event: hello" in first and first.split("event: hello", 1)[1].count("\n\n"):
            break
hello = json.loads(next((line[5:] for line in first.split("event: hello", 1)[-1].splitlines()
                         if line.startswith("data:")), "{}"))
smoke_calls = [e for e in hello.get("recent", []) if e["agent"] == "smoke"]
check("activity feed lists this run's calls", r.status_code == 200 and any(e["action"] == "recall" for e in smoke_calls),
      f"status={r.status_code} smoke_calls={len(smoke_calls)}")

# retire what this run stored, so the next run starts clean (exact duplicates would be skipped)
for memory_id in leftovers:
    post("/api/v1/forget", {"memory_id": memory_id, "reason": "smoke test cleanup"})
r, _ = post("/api/v1/library/sync", {"type": "doc", "origin": "smoke", "manifest": []})
check("library sync with an empty manifest drops the doc", r.json().get("removed") == 1, json.dumps(r.json())[:120])
r, _ = post("/api/v1/library/review", {"id": ref_id, "discard": True})
check("discard deletes the reference", r.json().get("discarded") == ref_id, json.dumps(r.json())[:120])

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
