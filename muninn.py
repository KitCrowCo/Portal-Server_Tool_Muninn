# tools/muninn/muninn.py
"""
Muninn - memory service. Per login: a raw capture pile (L0, append-only), curated thread pages (L1, Markdown), and a journal of every write.
Other tools and modules call capture() / entries() / threads() / page() / save_page() / search(); the Muninn module is the UI over the same functions.
Storage: data/muninn/users/<username>/
  inbox.jsonl    L0 - one capture per line; rewritten only when you destroy, seal or restore captures (life cycle)
  edits.jsonl    later changes to captures (thread links; next steps added, done, pinned, dated, parked), folded in when read - the captures themselves stay as written
  pages/<slug>.md          L1 - one page per thread: a small front-matter block, then the card text you curate
  pages/.history/<slug>/   every earlier version of a page
  pads/<id>.txt  scratch pads - free text with no syntax and no structure, kept until their content has been moved somewhere or is known not to be needed
  pads/<id>.json           pad title, status (open / closed = in the pile), version, last editor
  pads/<id>.notes.jsonl    notes about the pad (yours or the assistant's) and proposals to move, delete or reduce a block of it, with your answers
  pads/.history/<id>/      earlier versions of each pad (before every proposal applied, and at most every 10 minutes while typing)
  suggestions.jsonl        what the assistant suggests for a capture (a thread, a next step), and your answers
  cards.jsonl              updated thread cards the assistant proposes, and your answers
  signals.jsonl            how much a next step matters (high / some / low, why, can it be done now) - the assistant's judgement, and your corrections, which win
  state.json     small per-person state (last visit, for the "since you were here" line)
  huginn_charter.md / huginn_state.json / huginn_log.jsonl - what the assistant may do, what it has already reviewed, and everything it did (huginn.py)
  archive/<time>.sealed    sealed archives: captures and pads encrypted with a key from your passphrase (never stored); only the date and counts are readable without it
  journal.jsonl  id, layer, operation, author, time for every write (no content) - what the reconciler and later layers build on; lines about destroyed or sealed items keep only id, time and how they left
Capture syntax (all optional): #thread anywhere; a line starting with > is a next step, ? an open question, tried: / nope: something attempted and not liked. Leading #tags do not count as the line start, and " | " starts a new part within a line ("fix sync #finance | > test the window").
A line starting with @name is a command for the module that offers it ("@finance gas 40", "@agent ..."): it is stored as written and runs only when the person taps Run (Today, or the bridge's note after saving).
"""
import re, json, time, base64, hashlib, secrets, threading
from html import escape
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

TOOL_META = {"label": "Muninn", "icon": "&#x1F426;", "description": "Memory service: capture, thread pages, journal"}
router = APIRouter()
ENV = {}
DATA_DIR = Path("./data/muninn/users")   # under users/ so no username can land in data/muninn/static, which the loader serves publicly
_LOCKS = defaultdict(threading.RLock)   # re-entrant: applying a proposal captures to a thread while holding the pad's lock
_TAG = re.compile(r"(?<![\w&])#([A-Za-z0-9][\w-]*)")   # (?<!&) keeps HTML entities such as &#x2713; from reading as tags
_TRIED = re.compile(r"^(?:tried|nope)\s*:\s*", re.I)
_CMD = re.compile(r"^@([A-Za-z][\w-]*)(?:\s+(.*))?$")

def init_module(env: dict):
    ENV.update(env)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "register_bridge_slot" in ENV: ENV["register_bridge_slot"]("muninn", bridge_slot)

# --- Files ---

def _now() -> str: return datetime.now().isoformat(timespec="seconds")
def _new_id() -> str: return f"mn_{time.time_ns() // 1000:x}{secrets.token_hex(2)}"   # time-sortable, unique per capture
def slug(name: str) -> str: return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:60] or "untitled"

def _udir(user: str) -> Path:
    d = DATA_DIR / slug(user)
    if not (d / "pads").is_dir():
        for sub in ("pages", "pads"): (d / sub).mkdir(parents=True, exist_ok=True)
    return d

def _append(path: Path, rec: dict):
    with open(path, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _jsonl(path: Path) -> list: return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []
def _journal(user: str, op: str, item: str, layer: str, author: str = "user", **extra): _append(_udir(user) / "journal.jsonl", {"t": _now(), "op": op, "id": item, "layer": layer, "author": author, **extra})

# --- L0: captures ---

def line_kind(line: str) -> str:
    """next / question / tried / command / text - how a capture line reads once leading #tags are set aside."""
    c = _TAG.sub("", line).strip()
    return "next" if c.startswith(">") else "question" if c.startswith("?") else "tried" if _TRIED.match(c) else "command" if command_of(c) else "text"

def _marks(text: str) -> dict:
    """Next steps, questions, tried-items and commands: lines whose first non-tag character is >, ? or tried:/nope:, or that start with an @name some module offers."""
    lines = [_TAG.sub("", part).strip() for l in text.splitlines() for part in l.split(" | ")]   # " | " separates parts within one line, for single-line capture boxes
    return {"next": [l[1:].strip() for l in lines if l.startswith(">")], "questions": [l[1:].strip() for l in lines if l.startswith("?")], "tried": [_TRIED.sub("", l) for l in lines if _TRIED.match(l)], "commands": [l for l in lines if command_of(l)]}

def capture(user: str, text: str, source: str = "", ctx: dict = None, refs: list = None, threads: list = None, author: str = "user", plain: bool = False) -> dict:
    """Stores one capture. Threads come from #tags in the text plus any given; each gets a page if it has none.
    plain: text written by a program (an agent's summary) - stored as is, with no #tags, next steps or questions read from it; threads only from the threads argument."""
    text = str(text or "").strip()
    if not text: raise ValueError("empty capture")
    tags = sorted(({slug(t) for t in _TAG.findall(text)} if not plain else set()) | {slug(t) for t in threads or [] if str(t).strip()})
    e = {"id": _new_id(), "t": _now(), "text": text, "source": source, "author": author, "ctx": ctx or {}, "refs": refs or [], "threads": tags, **({"next": [], "questions": [], "tried": [], "commands": [], "plain": True} if plain else _marks(text))}
    with _LOCKS[user]:
        _append(_udir(user) / "inbox.jsonl", e)
        for t in tags: _ensure_page(user, t)
        _journal(user, "capture", e["id"], "L0", author, threads=tags)
    return e

_EDIT_OPS = ("thread_add", "thread_remove", "next_add", "next_done", "next_open", "next_pin", "next_unpin", "next_due", "next_park", "next_unpark", "absorb", "unabsorb", "cmd_run", "cmd_dismiss")

def edit(user: str, entry_id: str, op: str, author: str = "user", **kw):
    """op: thread_add / thread_remove (thread=slug); next_add (text=...); next_done (dropped=True when it was not needed after all) / next_open / next_pin / next_unpin (index=n);
    next_due (index, due=YYYY-MM-DD - replaces a date written in the text; "" clears it); next_park (index, until=YYYY-MM-DD - out of view until then) / next_unpark (index);
    absorb (its content is in a thread card now - it can leave the live notes after a grace period) / unabsorb;
    cmd_run (index, text = the line as it ran, result = the module's one-line answer) / cmd_dismiss (index - not to be run).
    Recorded beside the capture, which stays as written."""
    if op not in _EDIT_OPS: raise ValueError(f"unknown edit {op}")
    if op.startswith("thread"): kw["thread"] = slug(kw["thread"])
    with _LOCKS[user]:
        _append(_udir(user) / "edits.jsonl", {"t": _now(), "id": entry_id, "op": op, "author": author, **kw})
        if op == "thread_add": _ensure_page(user, kw["thread"])
        _journal(user, op, entry_id, "L0", author, **kw)

def entries(user: str, thread: str = "", limit: int = 0) -> list:
    """Captures with their edits applied, newest first; thread narrows to one thread."""
    es = {e["id"]: {**e, "threads": list(e["threads"]), "next": list(e["next"]), "commands": list(e.get("commands", [])), "done": [], "pinned": [], "due": {}, "parked": {}, "absorbed": "", "cmd": {}} for e in _jsonl(_udir(user) / "inbox.jsonl")}
    for x in _jsonl(_udir(user) / "edits.jsonl"):
        e = es.get(x["id"])
        if not e: continue
        if x["op"] == "thread_add" and x["thread"] not in e["threads"]: e["threads"].append(x["thread"])
        elif x["op"] == "thread_remove" and x["thread"] in e["threads"]: e["threads"].remove(x["thread"])
        elif x["op"] == "next_done" and x["index"] not in e["done"]: e["done"].append(x["index"])
        elif x["op"] == "next_open" and x["index"] in e["done"]: e["done"].remove(x["index"])
        elif x["op"] == "next_pin" and x["index"] not in e["pinned"]: e["pinned"].append(x["index"])
        elif x["op"] == "next_unpin" and x["index"] in e["pinned"]: e["pinned"].remove(x["index"])
        elif x["op"] == "next_add": e["next"].append(x["text"])
        elif x["op"] == "next_due": e["due"][x["index"]] = x["due"]
        elif x["op"] == "next_park": e["parked"][x["index"]] = x["until"]
        elif x["op"] == "next_unpark": e["parked"].pop(x["index"], None)
        elif x["op"] in ("absorb", "unabsorb"): e["absorbed"] = x["t"] if x["op"] == "absorb" else ""
        elif x["op"] in ("cmd_run", "cmd_dismiss"): e["cmd"][x["index"]] = x
    out = [e for e in reversed(list(es.values())) if not thread or thread in e["threads"]]
    return out[:limit] if limit else out

def open_next(user: str, thread: str = "") -> list:
    """Next steps not marked done: (entry, index, text), newest capture first."""
    return [(e, i, n) for e in entries(user, thread) for i, n in enumerate(e["next"]) if i not in e["done"]]

# --- Dates and Today ---

_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_DATE_RES = [re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"), re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)]

def due_of(text: str):
    """The first date written in a text (2026-10-20, 10/20, 10/20/26, Oct 20th), or None. A date without a year is this year, or next year if it is more than two months past."""
    today = datetime.now().date()
    for i, rx in enumerate(_DATE_RES):
        m = rx.search(text)
        if not m: continue
        try:
            if i == 0: return datetime(int(m[1]), int(m[2]), int(m[3])).date()
            y, mo, d = (int(m[3]) if m[3] else None, int(m[1]), int(m[2])) if i == 1 else (None, _MONTHS[m[1][:3].lower()], int(m[2]))
            y = (y + 2000 if y and y < 100 else y) or today.year
            dt = datetime(y, mo, d).date()
            return dt.replace(year=dt.year + 1) if not (i == 1 and m[3]) and (today - dt).days > 60 else dt
        except ValueError: continue
    return None

def due(e: dict, i: int, n: str):
    """A next step's date: one set later (next_due) wins over a date written in its text."""
    return datetime.fromisoformat(e["due"][i]).date() if e["due"].get(i) else due_of(n)

def is_parked(e: dict, i: int) -> bool: return e["parked"].get(i, "") > datetime.now().date().isoformat()

# --- Signals: how much a next step matters ---

LEVELS = ("high", "some", "low")
def skey(e: dict, i: int) -> str: return f"""{e["id"]}:{i}"""

def signals(user: str) -> dict:
    """key ("entry:index") -> the latest judgement; a correction by the person outranks any later judgement by the assistant."""
    out = {}
    for r in _jsonl(_udir(user) / "signals.jsonl"):
        if r["author"] == "user" or out.get(r["key"], {}).get("author") != "user": out[r["key"]] = r
    return out

def signal_set(user: str, key: str, level: str, why: str = "", actionable=None, author: str = "user") -> dict:
    """level: high (real consequence - money, a person waiting, a deadline), some, low (nothing much happens if it waits)."""
    if level not in LEVELS: raise ValueError(f"unknown level {level}")
    r = {"t": _now(), "key": key, "level": level, "why": str(why).strip()[:200], "actionable": actionable, "author": author}
    _append(_udir(user) / "signals.jsonl", r)
    return r

def _rank(sig: dict, e: dict, i: int) -> int: return LEVELS.index(sig.get(skey(e, i), {}).get("level", "some"))   # unjudged counts as "some"

def _ustate(user: str) -> dict: return json.loads((_udir(user) / "state.json").read_text(encoding="utf-8")) if (_udir(user) / "state.json").exists() else {}
def _ustate_put(user: str, **kw): (_udir(user) / "state.json").write_text(json.dumps({**_ustate(user), **kw}, indent=1), encoding="utf-8")

def today(user: str, mark_visit: bool = True) -> dict:
    """What deserves attention now:
    focus - pinned next steps (up to 3); now - dated within 2 days, or past their date on the first day that is seen; slipped - past their date and already shown once (no daily repeat, no tally);
    soon - dated 3-14 days out; parked - set aside until a date (back on their own then); suggestions, pad proposals and card proposals waiting; commands not yet run; since - after a day away, what others added."""
    d0, st = datetime.now().date(), _ustate(user)
    nx = [(e, i, n) for e, i, n in open_next(user) if not is_parked(e, i)]
    sig = signals(user)
    dated = sorted(((due(e, i, n), e, i, n) for e, i, n in nx if due(e, i, n) and i not in e["pinned"]), key=lambda x: (x[0], _rank(sig, x[1], x[2])))
    seen, now, slipped = st.get("slipped_seen", {}), [], []   # seen: "entry:index" -> [the date it slipped past, first day it was shown slipped]
    for d, e, i, n in (x for x in dated if (x[0] - d0).days <= 2):
        k = f"""{e["id"]}:{i}"""
        first = seen[k][1] if d < d0 and seen.get(k, [""])[0] == d.isoformat() else d0.isoformat()
        (slipped if first < d0.isoformat() else now).append((d, e, i, n))
        if d < d0: seen[k] = [d.isoformat(), first]
    since = st.get("last_visit", "")
    away = since and (datetime.now() - datetime.fromisoformat(since)).total_seconds() > 86400
    now.sort(key=lambda x: (_rank(sig, x[1], x[2]), x[0]))   # in "now", what matters most comes first, then the earlier date
    out = {"signals": sig, "focus": [(e, i, n) for e, i, n in nx if i in e["pinned"]][:3], "now": now, "slipped": slipped, "soon": [(d, e, i, n) for d, e, i, n in dated if 2 < (d - d0).days <= 14],
           "parked": [(e["parked"][i], e, i, n) for e, i, n in open_next(user) if is_parked(e, i)],
           "suggestions": suggestions(user), "cards": card_props(user), "commands": open_commands(user), "proposals": [(m, sum(1 for r in pad_notes(user, m["id"]) if r.get("status") == "pending")) for m in pads(user)],
           "since": [e for e in entries(user) if e["t"] > since and e.get("author") != "user" or (e["t"] > since and e.get("source") in ("share", "agent"))] if away else []}
    out["proposals"] = [(m, c) for m, c in out["proposals"] if c]
    if mark_visit: _ustate_put(user, last_visit=_now(), slipped_seen={k: v for k, v in seen.items() if any(k == f"""{e["id"]}:{i}""" for _, e, i, _ in slipped + now)})
    return out

# --- Suggestions for captures (a thread, a next step) ---

def suggest(user: str, entry_id: str, thread: str = "", next_step: str = "", reason: str = "", author: str = "huginn") -> dict:
    """What the assistant thinks a capture is missing: the thread it belongs to, and/or the next step it implies. Applied only when accepted."""
    r = {"id": _new_id(), "t": _now(), "kind": "suggestion", "entry": entry_id, "thread": slug(thread) if str(thread).strip() else "", "next": str(next_step).strip(), "reason": reason, "author": author, "status": "pending"}
    _append(_udir(user) / "suggestions.jsonl", r)
    return r

def suggestions(user: str, status: str = "pending") -> list:
    """Suggestions with their answers folded in, oldest first; status narrows (pending / accepted / declined, "" = all)."""
    by_id = {}
    for r in _jsonl(_udir(user) / "suggestions.jsonl"):
        if r["kind"] == "answer" and r["to"] in by_id: by_id[r["to"]]["status"] = r["status"]
        elif r["kind"] == "suggestion": by_id[r["id"]] = {"next": "", **r}
    return [r for r in by_id.values() if not status or r["status"] == status]

def answer_suggestion(user: str, sid: str, accept: bool, thread: str = None, next_step: str = None, author: str = "user"):
    """Accept applies the suggestion - with the thread or next-step text as the person edited it (blank = leave that part out); decline records the answer."""
    s = next((r for r in suggestions(user) if r["id"] == sid), None)
    if not s: raise ValueError("no pending suggestion with that id")
    thread, next_step = (s["thread"] if thread is None else thread.strip()), (s["next"] if next_step is None else next_step.strip())
    if accept and thread: edit(user, s["entry"], "thread_add", author=author, thread=thread)
    if accept and next_step: edit(user, s["entry"], "next_add", author=author, text=next_step)
    _append(_udir(user) / "suggestions.jsonl", {"id": _new_id(), "t": _now(), "kind": "answer", "to": sid, "status": "accepted" if accept else "declined", "author": author})

# --- Commands: "@finance gas 40" - a capture line for another module, run by that module after one tap (never on capture) ---
# A module offers one through the core services registry: register_service(name, {..., "command": {"help": one line with examples, "aliases": optional other names,
#   "parse": async (request, text, ctx) -> {"summary": what Run would do, "args": {...}} or {"error": why not} - changes nothing,
#   "run": async (request, args) -> a one-line result; raises ValueError to refuse}}). ctx is {"threads", "entry"} of the capture the line is in.
# Run parses the line again (as possibly edited) first, so what runs is what the person last saw. Only runs that happened are recorded (cmd_run); a refused or unparsable line stays open.

def command_specs() -> dict:
    """name and aliases (lower case) -> command spec plus the name of the service offering it, for every installed module that offers a command."""
    return {n.lower(): {**svc["command"], "service": name} for name, svc in ENV["services"].items() if "command" in svc for n in [name, *svc["command"].get("aliases", [])]}

def command_of(line: str):
    """(spec, text after the name) when a line starts with an @name some module offers, else None."""
    m = _CMD.match(_TAG.sub("", str(line)).strip())
    spec = command_specs().get(m[1].lower()) if m else None
    return (spec, (m[2] or "").strip()) if spec else None

def open_commands(user: str) -> list:
    """Command lines neither run nor dismissed: (entry, index, line), newest capture first."""
    return [(e, i, c) for e in entries(user) for i, c in enumerate(e["commands"]) if i not in e["cmd"]]

async def command_preview(request, line: str, e: dict) -> dict:
    """{"summary"} of what Run would do with this line, or {"error"}; changes nothing."""
    c = command_of(line)
    if not c: return {"error": f"""no installed module offers {str(line).split(" ")[0] or "that"} - offered: {", ".join("@" + n for n in command_specs()) or "none"}"""}
    return await c[0]["parse"](request, c[1], {"threads": e["threads"], "entry": e["id"]})

async def run_command(request, entry_id: str, index: int, line: str = None, author: str = "user") -> dict:
    """Runs one command line of a capture (as edited when line is given): {"ok", "result"}. Recorded beside the capture only when it ran."""
    user = request.state.user.username
    e = next(x for x in entries(user) if x["id"] == entry_id)
    line = str(e["commands"][index] if line is None else line).strip()
    p = await command_preview(request, line, e)
    if p.get("error"): return {"ok": False, "result": p["error"]}
    try: result = str(await command_of(line)[0]["run"](request, p["args"]))
    except ValueError as x: return {"ok": False, "result": str(x)}
    edit(user, entry_id, "cmd_run", author=author, index=index, text=line, result=result[:300])
    return {"ok": True, "result": result}

def dismiss_command(user: str, entry_id: str, index: int, author: str = "user"): edit(user, entry_id, "cmd_dismiss", author=author, index=index)

# --- L1: thread pages ---

def _page_path(user: str, s: str) -> Path: return _udir(user) / "pages" / f"{slug(s)}.md"

def _parse_page(text: str) -> tuple:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    meta = dict(l.split(": ", 1) for l in m.group(1).splitlines() if ": " in l) if m else {}
    return meta, text[m.end():] if m else text

def _write_page(user: str, s: str, meta: dict, body: str): _page_path(user, s).write_text("---\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n---\n" + body.strip() + "\n", encoding="utf-8")

def _ensure_page(user: str, s: str):
    if not _page_path(user, s).exists(): _write_page(user, s, {"id": _new_id(), "title": s, "class": "private", "created": _now()}, ""); _journal(user, "page_create", s, "L1")

def page(user: str, s: str) -> dict:
    """{slug, title, meta, body}; an empty page for a thread that has none yet."""
    p = _page_path(user, s)
    meta, body = _parse_page(p.read_text(encoding="utf-8")) if p.exists() else ({}, "")
    return {"slug": slug(s), "title": meta.get("title", slug(s)), "meta": meta, "body": body}

def save_page(user: str, s: str, body: str, title: str = "", author: str = "user"):
    """Replaces a page's text (and title if given); the previous version is kept under pages/.history/<slug>/."""
    with _LOCKS[user]:
        _ensure_page(user, s)
        p, cur = _page_path(user, s), page(user, s)
        hist = p.parent / ".history" / slug(s)
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"""{datetime.now().strftime("%Y%m%d-%H%M%S")}.md""").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        _write_page(user, s, {**cur["meta"], "title": title.strip() or cur["title"], "updated": _now()}, body)
        _journal(user, "page_save", slug(s), "L1", author)

# --- L1: card proposals (the assistant's updated card for a thread, applied only when accepted) ---

def card_propose(user: str, s: str, body: str, reason: str = "", author: str = "huginn", sources: list = None) -> dict:
    """base = the card's last save time when proposed: a card saved after that makes the proposal stale instead of overwriting the newer card. sources = the captures the card folds in."""
    r = {"id": _new_id(), "t": _now(), "kind": "proposal", "slug": slug(s), "base": page(user, s)["meta"].get("updated", ""), "body": body.strip(), "reason": reason, "author": author, "status": "pending", "sources": list(sources or [])}
    _append(_udir(user) / "cards.jsonl", r)
    return r

def card_props(user: str, s: str = "", status: str = "pending") -> list:
    """Card proposals with their answers folded in, oldest first; s narrows to one thread, status as for suggestions."""
    by_id = {}
    for r in _jsonl(_udir(user) / "cards.jsonl"):
        if r["kind"] == "answer" and r["to"] in by_id: by_id[r["to"]].update(status=r["status"], reply=r.get("reply", ""))
        elif r["kind"] == "proposal": by_id[r["id"]] = r
    return [r for r in by_id.values() if (not s or r["slug"] == slug(s)) and (not status or r["status"] == status)]

def card_answer(user: str, prop_id: str, accept: bool, reply: str = "", body: str = None, title: str = "", author: str = "user") -> str:
    """Accept saves the proposed card (or the person's edited version of it and title; the page history keeps the one before) and marks the captures it folded in as absorbed - those without an open next step;
    decline keeps a reply the assistant reads next time."""
    p = next((r for r in card_props(user) if r["id"] == prop_id), None)
    if not p: raise ValueError("no pending card proposal with that id")
    with _LOCKS[user]:
        status = "declined"
        if accept and page(user, p["slug"])["meta"].get("updated", "") != p["base"]: status = "stale"
        elif accept:
            save_page(user, p["slug"], p["body"] if body is None else body, title=title, author=author)
            status = "accepted"
            for e in entries(user):
                if e["id"] in p.get("sources", []) and not e["absorbed"] and len(e["done"]) == len(e["next"]): edit(user, e["id"], "absorb", author=author, card=p["slug"])
        _append(_udir(user) / "cards.jsonl", {"id": _new_id(), "t": _now(), "kind": "answer", "to": prop_id, "status": status, "reply": reply.strip(), "author": author})
    return status

def threads(user: str) -> list:
    """Every thread with its title, capture count, open next steps and last activity, most recent first."""
    es, out = entries(user), []
    for p in sorted((_udir(user) / "pages").glob("*.md")):
        mine = [e for e in es if p.stem in e["threads"]]
        out.append({"slug": p.stem, "title": page(user, p.stem)["title"], "count": len(mine), "open": sum(len(e["next"]) - len(e["done"]) for e in mine), "last": max([e["t"] for e in mine] + [datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")]), "last_capture": max((e["t"] for e in mine), default="")})
    return sorted(out, key=lambda t: t["last"], reverse=True)

def import_markdown(user: str, text: str, author: str = "user") -> list:
    """Splits a Markdown document at its ## headings: each section becomes (or is appended to) the page of the thread named by its heading. Text before the first heading is stored as a capture."""
    parts, made = re.split(r"(?m)^##\s+(.+?)\s*$", text), []
    if parts[0].strip(): capture(user, parts[0], source="import", author=author)
    for title, body in zip(parts[1::2], parts[2::2]):
        title = re.sub(r"^\d+[.)]\s*", "", title)   # "## 3. Manga library" -> thread "manga-library"
        s, cur = slug(title), page(user, title)
        save_page(user, s, f"""{cur["body"].strip()}\n\n## Imported {_now()[:10]}\n\n{body.strip()}""" if cur["body"].strip() else body, title=title, author=author)
        made.append(s)
    return made

# --- Pads: scratch space ---

_SNAP_S = 600   # while typing, a pad's history gets a copy at most every 10 minutes (and always before a large deletion or an applied proposal)

def _pid(pid: str) -> str: return re.sub(r"[^\w-]", "", str(pid))   # pad ids are generated (mn_...); this only keeps a crafted id from leaving the folder
def _pad_p(user: str, pid: str, ext: str) -> Path: return _udir(user) / "pads" / f"{_pid(pid)}{ext}"
def _pad_meta(user: str, pid: str) -> dict: return json.loads(_pad_p(user, pid, ".json").read_text(encoding="utf-8"))
def _pad_put(user: str, meta: dict): _pad_p(user, meta["id"], ".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
def pad_text(user: str, pid: str) -> str: return _pad_p(user, pid, ".txt").read_text(encoding="utf-8")

def _snapshot(user: str, pid: str):
    h = _udir(user) / "pads" / ".history" / _pid(pid)
    h.mkdir(parents=True, exist_ok=True)
    (h / f"""{datetime.now().strftime("%Y%m%d-%H%M%S")}.txt""").write_text(pad_text(user, pid), encoding="utf-8")

def _last_snap(user: str, pid: str) -> float:
    h = _udir(user) / "pads" / ".history" / _pid(pid)
    return max((f.stat().st_mtime for f in h.glob("*.txt")), default=0.0) if h.is_dir() else 0.0

def pads(user: str, status: str = "") -> list:
    """Pad metadata, most recently changed first; status narrows to open or closed."""
    ms = [json.loads(f.read_text(encoding="utf-8")) for f in (_udir(user) / "pads").glob("*.json")]
    return sorted((m for m in ms if not status or m["status"] == status), key=lambda m: m["updated"], reverse=True)

def pad_new(user: str, title: str = "", text: str = "", author: str = "user") -> dict:
    meta = {"id": _new_id(), "title": title.strip() or f"""Pad {datetime.now().strftime("%m-%d %H:%M")}""", "status": "open", "created": _now(), "updated": _now(), "version": 1, "editor": ""}
    with _LOCKS[user]:
        _pad_p(user, meta["id"], ".txt").write_text(text, encoding="utf-8")
        _pad_put(user, meta)
        _journal(user, "pad_new", meta["id"], "L0", author)
    return meta

def pad(user: str, pid: str) -> dict: return {**_pad_meta(user, pid), "text": pad_text(user, pid)}

def pad_save(user: str, pid: str, text: str, base_version: int, editor: str = "", author: str = "user") -> dict:
    """Saves typed text. If the pad changed since this editor loaded it (another device, or a proposal applied), nothing is overwritten:
    the incoming text is saved as a new pad and returned as conflict, so no version of anything typed is lost."""
    with _LOCKS[user]:
        meta, cur = _pad_meta(user, pid), pad_text(user, pid)
        if text == cur: return {"meta": meta, "conflict": None}
        if int(base_version) != meta["version"] and not (editor and editor == meta.get("editor")):
            c = {"id": _new_id(), "title": f"""{meta["title"]} (other copy {datetime.now().strftime("%H:%M")})""", "status": "open", "created": _now(), "updated": _now(), "version": 1, "editor": editor}
            _pad_p(user, c["id"], ".txt").write_text(text, encoding="utf-8")
            _pad_put(user, c)
            _journal(user, "pad_conflict_copy", c["id"], "L0", author, of=pid)
            return {"meta": meta, "conflict": c}
        if time.time() - _last_snap(user, pid) > _SNAP_S or len(cur) - len(text) > 200: _snapshot(user, pid)
        _pad_p(user, pid, ".txt").write_text(text, encoding="utf-8")
        meta.update(updated=_now(), version=meta["version"] + 1, editor=editor)
        _pad_put(user, meta)
        _journal(user, "pad_save", pid, "L0", author, chars=len(text))
    return {"meta": meta, "conflict": None}

def pad_set(user: str, pid: str, author: str = "user", **fields):
    """title, or status: open / closed (closed = moved to the pile; still kept until its content is dealt with)."""
    with _LOCKS[user]:
        meta = _pad_meta(user, pid)
        meta.update({k: v for k, v in fields.items() if k in ("title", "status") and v}, updated=_now())
        _pad_put(user, meta)
        _journal(user, "pad_set", pid, "L0", author, **{k: v for k, v in fields.items() if k == "status"})

def pad_delete(user: str, pid: str, author: str = "user"):
    """Removes a pad, its history and its notes. The UI asks first for a pad that still has text."""
    with _LOCKS[user]:
        for ext in (".txt", ".json", ".notes.jsonl"): _pad_p(user, pid, ext).unlink(missing_ok=True)
        h = _udir(user) / "pads" / ".history" / _pid(pid)
        if h.is_dir():
            for f in h.iterdir(): f.unlink()
            h.rmdir()
        _journal(user, "pad_delete", pid, "L0", author)

# --- Pads: notes and proposals ---

def pad_notes(user: str, pid: str) -> list:
    """Notes and proposals for a pad, oldest first, with each proposal's latest answer folded in."""
    out, by_id = [], {}
    for r in _jsonl(_pad_p(user, pid, ".notes.jsonl")):
        if r["kind"] == "answer" and r["to"] in by_id: by_id[r["to"]].update(status=r["status"], reply=r.get("reply", ""), answered=r["t"])
        elif r["kind"] != "answer": by_id[r["id"]] = r; out.append(r)
    return out

def pad_note(user: str, pid: str, text: str, author: str = "user") -> dict:
    """Context about a pad - from you (what a block is for, why it stays) or from the assistant (what it moved where, what it is unsure of)."""
    r = {"id": _new_id(), "t": _now(), "kind": "note", "author": author, "text": text.strip()}
    _append(_pad_p(user, pid, ".notes.jsonl"), r)
    return r

def pad_propose(user: str, pid: str, action: str, block: str, target: str = "", replacement: str = "", reason: str = "", author: str = "huginn") -> dict:
    """A proposed change to one block of a pad, applied only when accepted:
    move - the block becomes a capture on thread `target` and leaves the pad; delete - the block leaves the pad;
    reduce - the block is replaced by `replacement` (the original goes to thread `target` as a capture when one is given);
    wiki - the block is added to the wiki page `target` (a path such as Home/Setup.md) and leaves the pad."""
    if action not in ("move", "delete", "reduce", "wiki"): raise ValueError(f"unknown proposal {action}")
    r = {"id": _new_id(), "t": _now(), "kind": "proposal", "author": author, "action": action, "block": block, "target": (target.strip() if action == "wiki" else slug(target)) if target else "", "replacement": replacement, "reason": reason, "status": "pending"}
    _append(_pad_p(user, pid, ".notes.jsonl"), r)
    return r

def pad_answer(user: str, pid: str, prop_id: str, accept: bool, reply: str = "", author: str = "user", deliver=None) -> str:
    """Accept (apply the change - the pad's history keeps the version before it) or decline with a reply the assistant reads next time.
    A block that no longer appears exactly once in the pad is not applied; the proposal is marked stale.
    deliver(proposal) puts a wiki block in its place before the pad changes; when it raises, nothing changes and the proposal stays pending."""
    p = next((r for r in pad_notes(user, pid) if r["id"] == prop_id and r["kind"] == "proposal"), None)
    if not p or p["status"] != "pending": raise ValueError("no pending proposal with that id")
    with _LOCKS[user]:
        text, meta, status = pad_text(user, pid), _pad_meta(user, pid), "declined"
        if accept and text.count(p["block"]) != 1: status = "stale"
        elif accept:
            if p["action"] == "wiki": deliver(p)
            if p["target"] and p["action"] in ("move", "reduce"): capture(user, p["block"], source=f"""pad:{pid}""", threads=[p["target"]], author=author)
            _snapshot(user, pid)
            _pad_p(user, pid, ".txt").write_text(text.replace(p["block"], p["replacement"] if p["action"] == "reduce" else "", 1), encoding="utf-8")
            meta.update(updated=_now(), version=meta["version"] + 1, editor="")
            _pad_put(user, meta)
            _journal(user, f"""pad_{p["action"]}""", pid, "L0", author, proposal=prop_id)
            status = "accepted"
        _append(_pad_p(user, pid, ".notes.jsonl"), {"id": _new_id(), "t": _now(), "kind": "answer", "to": prop_id, "status": status, "reply": reply.strip(), "author": author})
    return status

# --- Life cycle: active -> absorbed (in a card) -> sealed (encrypted archive) or destroyed ---
# Nothing leaves without the person choosing it. Destroyed means gone from the live notes: its journal lines keep only id, time and how it left. Sealed means encrypted with a key derived from
# a passphrase the person types (scrypt, AES-256-GCM); the passphrase is never stored, so without it nobody can read the archive - this server included. Backups keep their copies until they rotate out.

def moot_propose(user: str, entry_id: str, why: str, author: str = "huginn") -> dict:
    """The assistant thinks a capture no longer matters (an event passed, a bill paid, a decision superseded) and says why. It is listed in Reduce; nothing leaves until the person destroys or seals it there."""
    r = {"id": _new_id(), "t": _now(), "kind": "moot", "entry": entry_id, "why": str(why).strip()[:200], "author": author, "status": "pending"}
    _append(_udir(user) / "suggestions.jsonl", r)
    return r

def moots(user: str, status: str = "pending") -> list:
    """Moot proposals with the person's answers folded in (pending / kept, "" = all), oldest first. A proposal leaves with its capture when that is destroyed or sealed."""
    by_id = {}
    for r in _jsonl(_udir(user) / "suggestions.jsonl"):
        if r["kind"] == "moot": by_id[r["id"]] = dict(r)
        elif r["kind"] == "answer" and r["to"] in by_id: by_id[r["to"]].update(status=r["status"], reply=r.get("reply", ""))
    return [r for r in by_id.values() if not status or r["status"] == status]

def keep_moot(user: str, mid: str, reply: str = "", author: str = "user"):
    """Not moot after all: the capture stays; the reply is read by the assistant on later passes."""
    _append(_udir(user) / "suggestions.jsonl", {"id": _new_id(), "t": _now(), "kind": "answer", "to": mid, "status": "kept", "reply": str(reply).strip()[:300], "author": author})

def reduction(user: str, grace_days: int = 30, moot_days: int = 90) -> dict:
    """What could leave the live notes: captures the assistant thinks no longer matter (with its why); captures absorbed into a card at least grace_days ago; captures that look moot after moot_days
    (every next step closed, or loose: no thread and nothing to do); pads in the Pile that are empty or untouched for moot_days. A capture is listed once, in the first list that has it."""
    now = datetime.now()
    old = lambda t, days: bool(t) and (now - datetime.fromisoformat(t)).days >= days
    es = entries(user)
    by = {e["id"]: e for e in es}
    proposed = [{**by[m["entry"]], "why": m["why"], "mid": m["id"]} for m in moots(user) if m["entry"] in by]
    seen = {e["id"] for e in proposed}
    absorbed = [e for e in es if old(e["absorbed"], grace_days) and e["id"] not in seen]
    closed = lambda e: len(e["done"]) == len(e["next"])
    moot = [{**e, "why": "every next step closed" if e["next"] else "no thread, nothing to do"} for e in es if not e["absorbed"] and e["id"] not in seen and old(e["t"], moot_days) and closed(e) and (e["next"] or not e["threads"])]
    pile = [{**m, "empty": not pad_text(user, m["id"]).strip()} for m in pads(user, "closed")]
    return {"proposed": proposed, "absorbed": absorbed, "moot": moot, "pads": [m for m in pile if m["empty"] or old(m["updated"], moot_days)]}

def _rewrite(path: Path, keep):
    """Rewrites a .jsonl with only the records keep(record) accepts - written beside it, then renamed over it."""
    if not path.exists(): return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _jsonl(path) if keep(r)), encoding="utf-8")
    tmp.replace(path)

def destroy(user: str, entry_ids: list = (), pad_ids: list = (), how: str = "destroyed", author: str = "user") -> dict:
    """Removes captures (with their edits, suggestions and signals) and pads (with their history and notes) from the live notes. Journal lines about them keep only id, time and how they left."""
    ids, pids = set(entry_ids), set(pad_ids)
    with _LOCKS[user]:
        d = _udir(user)
        gone_sugg = {r["id"] for r in _jsonl(d / "suggestions.jsonl") if r.get("entry") in ids}
        _rewrite(d / "inbox.jsonl", lambda r: r["id"] not in ids)
        _rewrite(d / "edits.jsonl", lambda r: r["id"] not in ids)
        _rewrite(d / "suggestions.jsonl", lambda r: r.get("entry") not in ids and r.get("to") not in gone_sugg)
        _rewrite(d / "signals.jsonl", lambda r: r["key"].split(":")[0] not in ids)
        for pid in pids: pad_delete(user, pid, author=author)
        _rewrite(d / "journal.jsonl", lambda r: True)   # normalises the file before redaction below
        jl = [({"t": r["t"], "op": "redacted", "id": r["id"], "how": how} if r.get("id") in ids | pids else r) for r in _jsonl(d / "journal.jsonl")]
        (d / "journal.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in jl), encoding="utf-8")
        _journal(user, how.split(":")[0], "(batch)", "L0", author, captures=len(ids), pads=len(pids))
    return {"captures": len(ids), "pads": len(pids)}

def _b64(b: bytes) -> str: return base64.b64encode(b).decode()
def _key(passphrase: str, salt: bytes) -> bytes: return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=64 << 20)

def seal(user: str, passphrase: str, entry_ids: list = (), pad_ids: list = (), author: str = "user") -> str:
    """Encrypts captures (with their edits) and pads (text, title, notes) into one archive file, then removes them from the live notes as destroy does. Returns the archive's name."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # the cryptography package (python-jose[cryptography] brings it)
    if len(passphrase) < 8: raise ValueError("use a passphrase of at least 8 characters")
    ids, d = set(entry_ids), _udir(user)
    payload = {"captures": [r for r in _jsonl(d / "inbox.jsonl") if r["id"] in ids], "edits": [r for r in _jsonl(d / "edits.jsonl") if r["id"] in ids],
               "pads": [{**_pad_meta(user, p), "text": pad_text(user, p), "notes": pad_notes(user, p)} for p in pad_ids]}
    salt, nonce, name = secrets.token_bytes(16), secrets.token_bytes(12), datetime.now().strftime("%Y%m%d-%H%M%S")
    (d / "archive").mkdir(exist_ok=True)
    (d / "archive" / f"{name}.sealed").write_text(json.dumps({"v": 1, "kdf": "scrypt n=32768 r=8 p=1", "salt": _b64(salt), "nonce": _b64(nonce), "sealed": _now(), "count": {"captures": len(payload["captures"]), "pads": len(payload["pads"])},
                                                              "ct": _b64(AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None))}), encoding="utf-8")
    destroy(user, list(ids), list(pad_ids), how=f"sealed:{name}", author=author)
    return name

def archives(user: str) -> list:
    """Sealed archives, newest first: name, when, how many captures and pads (nothing else is readable without the passphrase)."""
    return [{"name": f.stem, **{k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items() if k in ("sealed", "count")}} for f in sorted((_udir(user) / "archive").glob("*.sealed"), reverse=True)] if (_udir(user) / "archive").is_dir() else []

def unseal(user: str, name: str, passphrase: str) -> dict:
    """The archive's contents; ValueError when the passphrase is wrong (or the file was changed)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    a = json.loads((_udir(user) / "archive" / f"{_pid(name)}.sealed").read_text(encoding="utf-8"))
    try: return json.loads(AESGCM(_key(passphrase, base64.b64decode(a["salt"]))).decrypt(base64.b64decode(a["nonce"]), base64.b64decode(a["ct"]), None))
    except InvalidTag: raise ValueError("wrong passphrase (or the archive file was changed)")

def restore(user: str, name: str, passphrase: str, author: str = "user") -> dict:
    """Puts an archive's captures (in time order, with their edits) and pads back into the live notes, then removes the archive file."""
    x, d = unseal(user, name, passphrase), _udir(user)
    with _LOCKS[user]:
        for fname, rows in (("inbox.jsonl", x["captures"]), ("edits.jsonl", x["edits"])):
            merged = sorted(_jsonl(d / fname) + rows, key=lambda r: r["t"])
            (d / fname).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged), encoding="utf-8")
        for p in x["pads"]:
            _pad_p(user, p["id"], ".txt").write_text(p["text"], encoding="utf-8")
            _pad_p(user, p["id"], ".notes.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in p["notes"]), encoding="utf-8")
            _pad_put(user, {k: v for k, v in p.items() if k not in ("text", "notes")})
        (d / "archive" / f"{_pid(name)}.sealed").unlink()
        _journal(user, "restored", name, "L0", author, captures=len(x["captures"]), pads=len(x["pads"]))
    return {"captures": len(x["captures"]), "pads": len(x["pads"])}

# --- Search ---

def search(user: str, q: str) -> dict:
    """Case-insensitive text match over captures and pages."""
    ql = q.strip().lower()
    if not ql: return {"entries": [], "pages": []}
    return {"entries": [e for e in entries(user) if ql in e["text"].lower()], "pages": [t for t in threads(user) if ql in t["title"].lower() or ql in page(user, t["slug"])["body"].lower()]}

# --- Capture surfaces ---

# Unsaved text is never lost in an editor (textarea) that carries data-keep-url (where its save goes), data-keep-vals (JSON added to the form's fields), data-keep-status (the element showing its state)
# and optionally data-keep-tab (a tab to mark): typing shows "not saved yet" (and a dot on the tab); before any other htmx request from the page runs (a tab switch, close, a note, another pad),
# when the editor is about to be replaced, and when the page is hidden or closed (switching apps on a phone), the text is sent first. The editor's own saves clear the mark when they succeed.
KEEP_JS = """<script>window.mnKeep = window.mnKeep || (function () {
    var Q = 'textarea[data-keep-url]';
    function mark(t, txt) {   /* txt null: only the tab dot follows the dirty state */
        var s = document.getElementById(t.dataset.keepStatus), b = t.dataset.keepTab && document.getElementById(t.dataset.keepTab);
        if (s && txt !== null) (s.querySelector('.status-label') || s).textContent = txt;
        if (b) b.classList.toggle('keep-dirty', !!t.dataset.dirty);
    }
    function own(el) { return el && el.matches ? (el.matches(Q) ? el : el.tagName === 'FORM' ? el.querySelector(Q) : null) : null; }   /* the editor itself, or its form's Save */
    function send(t) {
        if (!t.dataset.dirty) return Promise.resolve();
        var fd = new FormData(t.form), x = JSON.parse(t.dataset.keepVals || '{}'), n = t.dataset.n;
        for (var k in x) fd.set(k, x[k]);
        delete t.dataset.dirty;
        return fetch(t.dataset.keepUrl, {method: 'POST', body: fd, credentials: 'same-origin', keepalive: t.value.length < 60000}).then(function (r) {   /* keepalive (it outlives the page) is limited to 64 KB */
            if (!r.ok) throw r.status;
            if (t.isConnected && t.dataset.n === n) mark(t, '\\u2713 saved');
        }).catch(function (why) { t.dataset.dirty = '1'; mark(t, '\\u25CF not saved (' + why + ') - press Save'); });
    }
    function all() { return Promise.all(Array.prototype.map.call(document.querySelectorAll(Q + '[data-dirty]'), send)); }
    document.addEventListener('input', function (e) { var t = e.target; if (!t.matches || !t.matches(Q)) return; t.dataset.dirty = '1'; t.dataset.n = (+t.dataset.n || 0) + 1; mark(t, '\\u25CF not saved yet'); });
    document.addEventListener('htmx:confirm', function (e) {   /* fired for every htmx request: dirty editors are saved first, then the request goes on (its own confirm dialog, if any, still shows) */
        if (own(e.detail.elt) || !document.querySelector(Q + '[data-dirty]')) return;
        e.preventDefault();
        all().then(function () { e.detail.issueRequest(); });
    });
    document.addEventListener('htmx:beforeRequest', function (e) { var t = own(e.detail.elt); if (t) t.dataset.sent = t.dataset.n || ''; });
    document.addEventListener('htmx:afterRequest', function (e) { var t = own(e.detail.elt); if (t && e.detail.successful && t.dataset.sent === (t.dataset.n || '')) { delete t.dataset.dirty; mark(t, null); } });
    document.addEventListener('htmx:beforeCleanupElement', function (e) { if (e.detail.elt.matches && e.detail.elt.matches(Q)) send(e.detail.elt); });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') all(); });
    window.addEventListener('pagehide', all);
    return {send: send, all: all};
})();</script><style>.tab.keep-dirty::after{content:" \\25CF";color:var(--accent);}</style>"""

async def context(request: Request) -> dict:
    """Where the person was: the portal tab open at capture time (the portal keeps it per login)."""
    nav = await ENV["get_state"](request, scope="user", namespace="_nav") or {}
    tab = (nav.get("tabs") or {}).get(nav.get("active", ""), {})
    return {"tab": tab.get("label", ""), "path": tab.get("path", "")}

def bridge_slot(user) -> str:
    """Capture box in the Input Bridge row. Plain form post (works without JS); htmx keeps the page in place; Alt+M opens the bar and focuses the box."""
    if not user: return ""
    return """<form id="mn-cap" class="row grow" action="/tool/muninn/capture" method="post" hx-post="/tool/muninn/capture" hx-target="#mn-cap-status" hx-swap="innerHTML" hx-on::after-request="if(event.detail.successful) this.reset()" style="flex-wrap:nowrap;padding:0 .3rem;margin:0">
                   <button type="button" class="btn-icon" title="Pads (Alt+P)" hx-get="/tool/muninn/bp" hx-target="#accessibility_toolbar" hx-swap="afterbegin" hx-on::before-request="if (document.getElementById('mnb-panel')) { mnbClose(); event.preventDefault(); }">&#x1F4DD;</button>
                   <input name="text" class="module-select grow" style="margin:0;min-width:7rem" placeholder="Muninn (Alt+M): anything - #thread | &gt; next | ? question | tried: ... | @finance gas 40" autocomplete="off" required>
                   <input type="hidden" name="source" value="bridge"><span id="mn-cap-status" class="status-label" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0"></span>
               </form>
               <script>document.addEventListener('keydown', function(e) { if (!e.altKey || e.key.toLowerCase() !== 'm') return; e.preventDefault(); var bar = document.getElementById('accessibility_toolbar'), btn = document.getElementById('btn-accessibility_toolbar'); if (bar && btn && !bar.classList.contains('active')) btn.click(); setTimeout(function() { var i = document.querySelector('#mn-cap [name=text]'); if (i) i.focus(); }, 60); });
                       document.addEventListener('keydown', function(e) { if (!e.altKey || e.key.toLowerCase() !== 'p') return; e.preventDefault(); var b = document.querySelector('#mn-cap .btn-icon'); if (b) htmx.trigger(b, 'click'); });
                       /* The pad panel lives inside the bottom (Input Bridge) toolbar, which grows to hold it - the page above shrinks instead of being covered, so side bars stay reachable */
                       window.mnbOpen = function() { var b = document.getElementById('accessibility_toolbar'); if (!b) return; if (!b.classList.contains('active')) document.getElementById('btn-accessibility_toolbar').click(); if (!b.dataset.mnbPrev) b.dataset.mnbPrev = b.dataset.size || '3rem'; b.dataset.size = 'min(60vh, 34rem)'; updateLayout(b.closest('[data-shell]')); };
                       window.mnbClose = function() { if (window.mnKeep) mnKeep.all(); var p = document.getElementById('mnb-panel'), b = document.getElementById('accessibility_toolbar'); if (p) p.remove(); if (b && b.dataset.mnbPrev) { b.dataset.size = b.dataset.mnbPrev; delete b.dataset.mnbPrev; updateLayout(b.closest('[data-shell]')); } };</script>
               <style>
                   #mnb-panel { flex:1; min-height:0; display:flex; flex-direction:column; gap:.2rem; padding:.3rem; }
                   #accessibility_toolbar:has(#mnb-panel) > .toolbar-internal { flex:0 0 auto; }   /* the panel takes the grown height; the bridge row keeps its own */
                   @media (max-width: 46rem) { #im-bridge-outer:has(#mn-cap) > #im-status-bar:empty { flex:0 0 0; padding:0; } }   /* phones: an empty portal status line gives its room to the capture box and its note */
                   #mnb-panel textarea { flex:1; min-height:0; max-height:none; resize:none; font-family:var(--font-mono); font-variant-ligatures:none; font-feature-settings:"liga" 0, "calt" 0; }
               </style>""" + KEEP_JS

# --- Bridge pad panel (a pad editor that opens from the Input Bridge on any page - one tab on a phone) ---

async def _bp_pid(request, pid: str = "") -> str:
    """The pad to show: the one asked for, else the last one used from the bridge, else the newest open pad, else a new one."""
    user, ids = request.state.user.username, [m["id"] for m in pads(request.state.user.username, "open")]
    last = await ENV["get_state"](request, scope="user", namespace="muninn", key="bridge_pad")
    pid = pid if pid in ids else last if last in ids else ids[0] if ids else pad_new(user)["id"]
    await ENV["set_state"](request, pid, scope="user", namespace="muninn", key="bridge_pad")
    return pid

def _bp_html(user: str, pid: str, note: str = "") -> str:
    p = pad(user, pid)
    opts = "".join(f"""<option value="{m["id"]}"{" selected" if m["id"] == pid else ""}>{escape(m["title"])}</option>""" for m in pads(user, "open"))
    return f"""<div id="mnb-panel">
                   <div class="row" style="flex-wrap:nowrap">
                       <select name="pid" class="module-select grow" style="margin:0" hx-get="/tool/muninn/bp" hx-target="#mnb-panel" hx-swap="outerHTML" hx-trigger="change">{opts}</select>
                       <button type="button" class="btn-icon" title="New pad" hx-post="/tool/muninn/bp_new" hx-target="#mnb-panel" hx-swap="outerHTML">+</button>
                       <button type="button" class="btn-icon" title="Close the panel (the pad stays open)" onclick="mnbClose()">&#x2715;</button>
                   </div>
                   <form id="mnb-form" class="node-form grow" style="min-height:0" hx-post="/tool/muninn/bp_save" hx-target="#mnb-meta" hx-swap="innerHTML">
                       <input type="hidden" name="pid" value="{pid}"><input type="hidden" name="editor" value="{secrets.token_hex(4)}">
                       <textarea name="text" class="cm-input" placeholder="Anything - it stays in this pad until it has been moved somewhere or you know it is not needed." hx-post="/tool/muninn/bp_save" hx-target="#mnb-meta" hx-swap="innerHTML" hx-trigger="keyup changed delay:1500ms, blur" hx-include="#mnb-form" data-keep-url="/tool/muninn/bp_save" data-keep-status="mnb-meta">{escape(p["text"])}</textarea>
                       <div class="row"><span id="mnb-meta" class="row grow"><input type="hidden" name="version" value="{p["version"]}"><span class="status-label">{escape(note) or "saved " + p["updated"][5:16].replace("T", " ")}</span></span><button type="submit" class="ui-btn">Save</button></div>
                   </form>
                   <script>mnbOpen()</script>
               </div>"""

@router.get("/bp")
async def bp_route(request: Request, pid: str = ""): return HTMLResponse(_bp_html(request.state.user.username, await _bp_pid(request, pid)))

@router.post("/bp_new")
async def bp_new_route(request: Request): return HTMLResponse(_bp_html(request.state.user.username, await _bp_pid(request, pad_new(request.state.user.username)["id"])))

@router.post("/bp_save")
async def bp_save_route(request: Request, pid: str = Form(...), text: str = Form(""), version: int = Form(0), editor: str = Form("")):
    """Saves the bridge editor. On a conflict the whole panel is replaced by the copy that now holds the typed text (pad_save never overwrites a changed pad)."""
    user = request.state.user.username
    r = pad_save(user, pid, text, version, editor=editor)
    if r["conflict"]:
        await _bp_pid(request, r["conflict"]["id"])
        return HTMLResponse(_bp_html(user, r["conflict"]["id"], "This pad changed elsewhere - your text continues in this copy"), headers={"HX-Retarget": "#mnb-panel", "HX-Reswap": "outerHTML"})
    return HTMLResponse(f"""<input type="hidden" name="version" value="{r["meta"]["version"]}"><span class="status-label">saved {r["meta"]["updated"][5:16].replace("T", " ")}</span>""")

def _saved_note(e: dict) -> str: return " ".join(["&#x2713; saved", *("#" + t for t in e["threads"]), *([f"""({len(e["next"])} next)"""] if e["next"] else [])])

async def _bridge_cmds(request, e: dict) -> str:
    """The saved note for a bridge capture with @command lines: a Run chip per line, labelled with the start of what it would do (all of it in the tooltip), or why it cannot run - short, for a phone's bridge row. Each line also waits on Today until it is run or dismissed."""
    out = "&#x2713;"
    for i, c in enumerate(e["commands"]):
        p = await command_preview(request, c, e)
        out += f""" <span class="status-label" style="color:#ff5f5f" title="{escape(p["error"])}">{escape(p["error"][:40])}</span>""" if p.get("error") else f""" <button type="button" class="chip" title="Run: {escape(p["summary"])}" hx-post="/tool/muninn/cmd" hx-vals='{json.dumps({"entry": e["id"], "index": i})}' hx-target="#mn-cap-status" hx-swap="innerHTML">&#x25B6; {escape(p["summary"].split(" - ")[0][:40])}</button>"""
    return out

@router.post("/capture")
async def capture_route(request: Request, text: str = Form(...), source: str = Form("form"), thread: str = Form("")):
    e = capture(request.state.user.username, text, source=source, ctx=await context(request), threads=[thread] if thread else None)
    if request.headers.get("HX-Request"): return HTMLResponse(await _bridge_cmds(request, e) if e["commands"] else _saved_note(e))
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

@router.post("/cmd")
async def cmd_route(request: Request, entry: str = Form(...), index: int = Form(...)):
    """The bridge's Run chip: runs the command line as captured; its answer replaces the note."""
    r = await run_command(request, entry, index)
    return HTMLResponse(f"""<span title="{escape(r["result"])}">{"&#x2713;" if r["ok"] else "&#x2715;"} {escape(r["result"][:100])}</span>""")

@router.post("/share")
async def share_route(request: Request, title: str = Form(""), text: str = Form(""), url: str = Form("")):
    """PWA share target (static/manifest.json share_target): anything shared from another app on the phone becomes a capture."""
    e = capture(request.state.user.username, "\n".join(x for x in (title, text, url) if x.strip()), source="share", refs=[url] if url.strip() else None)
    return HTMLResponse(f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="2;url=/"><body style="font-family:sans-serif;background:#020408;color:#ddd;padding:2rem">{_saved_note(e)} - back to the portal in a moment. <a href="/" style="color:#00d4ff">Go now</a></body>""")

from tools.muninn import huginn   # the assistant (Huginn) works through the functions above; imported last because it imports this module
