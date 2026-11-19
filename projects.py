"""tools/muninn/projects.py - projects shared by people and AIs: state, issues, decisions, goals and guides as plain files, read and changed through masks.
A project is a folder under data/_tools/muninn/projects/<slug>/ - typically the checkout of a private repository kept in sync by Dev Studio (so the files travel by git like code).
  _project.json   {"name", "about", "owner", "masks": {who: {"read": [globs], "write": [globs], "how": "direct" | "propose", "direct": [globs written directly even when "how" is propose: an AI's own to-do list or plan in the project]}}}
                  who: a portal user name, or "agent:<profile>" for an AI (a local model's profile, "agent:claude" for an outside one). The owner sees and changes everything; anyone else sees only what a mask gives them.
  <any>.md / .json      the project's files, in whatever folders it has (context/, data/, claude/ ...)
  data/...        files laid out as the portal's own data/ - "put in place" copies one to the same path in the portal (never over an existing file without asking)
  An AI works on a project through the Aviary step "project" (step(), CALLS) as agent:<profile> on behalf of the run's starter: both have to be allowed.
  .muninn/history/<file>/<n>   every earlier version of a file changed here; .muninn/proposals.jsonl what an AI proposed and the answer; .muninn/journal.jsonl who changed what, when
Issues are the rows of a Markdown table whose first column is an id (I-nn) and that has a Status column: they are read and their status changed in place, so the file stays the one source.
Export gives a zip of what one mask sees; import takes such a zip back, writing only what the importer may write, each replaced file kept first."""
import io, re, json, time, shutil, fnmatch, zipfile
from pathlib import Path
from tools.muninn import muninn as MN

ROOT = Path("./data/_tools/muninn/projects")
PORTAL_DATA = Path("./data")
STATUSES = ["open", "built", "test", "proposal", "done"]
_SKIP = (".git", ".muninn")

def init():
    ROOT.mkdir(parents=True, exist_ok=True)

# --- Projects and masks ---

def _meta(slug: str) -> dict: return json.loads(p.read_text()) if (p := ROOT / slug / "_project.json").exists() else {"name": slug, "about": "", "owner": "", "masks": {}}
def _dir(slug: str) -> Path: return ROOT / MN.slug(slug)

def _name(who) -> str: return " for ".join(who) if isinstance(who, tuple) else who

def _may(slug: str, who, what: str, rel: str) -> bool:
    """Whether who may read or write one file of the project. The owner (and anyone, while a project names no owner) may do everything. who may be a tuple (an AI working for a person): every one of them has to be allowed, so the AI never does more than its own mask or than the person may."""
    if isinstance(who, tuple): return all(_may(slug, w, what, rel) for w in who)
    m = _meta(slug)
    if not m.get("owner") or who == m["owner"]: return True
    return any(fnmatch.fnmatch(rel, g) for g in (m.get("masks", {}).get(who, {}).get(what) or []))

def how(slug: str, who, rel: str = "") -> str:
    """Whether who's change to rel is written ("direct") or kept as a proposal: the owner writes directly; anyone else as their mask says - its "how", or a "direct" glob that rel matches."""
    if isinstance(who, tuple): return "direct" if all(how(slug, w, rel) == "direct" for w in who) else "propose"
    m = _meta(slug)
    if not m.get("owner") or who == m["owner"]: return "direct"
    mask = m.get("masks", {}).get(who, {})
    return "direct" if mask.get("how") == "direct" or (rel and any(fnmatch.fnmatch(rel, g) for g in mask.get("direct") or [])) else "propose"

def projects(who: str) -> list:
    """The projects who sees anything of: {slug, name, about, owner, files (count seen)}."""
    out = []
    for d in sorted(x for x in ROOT.iterdir() if x.is_dir()) if ROOT.exists() else []:
        seen = files(d.name, who)
        if seen: out.append({"slug": d.name, **{k: _meta(d.name).get(k, "") for k in ("name", "about", "owner")}, "files": len(seen)})
    return out

def files(slug: str, who: str) -> list:
    """The project's files who may read, as paths relative to the project."""
    d = _dir(slug)
    return [r for f in sorted(d.rglob("*")) if f.is_file() and not set(f.relative_to(d).parts) & set(_SKIP) and _may(slug, who, "read", r := f.relative_to(d).as_posix())]

def _path(slug: str, rel: str) -> Path:
    p = (_dir(slug) / rel).resolve()
    if _dir(slug).resolve() not in p.parents or set(Path(rel).parts) & set(_SKIP): raise ValueError(f"not a file of this project: {rel}")
    return p

def read(slug: str, rel: str, who: str) -> str:
    if not _may(slug, who, "read", rel): raise PermissionError(f"{who} may not read {rel}")
    return _path(slug, rel).read_text(encoding="utf-8")

# --- Changing files (kept history, journal, proposals) ---

def _journal(slug: str, **rec):
    (_dir(slug) / ".muninn").mkdir(exist_ok=True)
    with open(_dir(slug) / ".muninn" / "journal.jsonl", "a", encoding="utf-8") as f: f.write(json.dumps({"t": MN._now(), **rec}, ensure_ascii=False) + "\n")

def _write(slug: str, rel: str, text: str, author: str):
    """Writes a file, its earlier version kept first."""
    p = _path(slug, rel)
    if p.exists():
        h = _dir(slug) / ".muninn" / "history" / rel
        h.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, h / f"""{len(list(h.iterdir())) + 1}{p.suffix}""")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _journal(slug, op="write", file=rel, author=author)

def save(slug: str, rel: str, text: str, who: str, why: str = "") -> dict:
    """A file changed by who: written when who may write it directly, kept as a proposal when who may only propose. Answers what happened."""
    if not _may(slug, who, "write", rel): raise PermissionError(f"{_name(who)} may not change {rel}")
    if rel == "_project.json" and _meta(slug).get("owner") and who != _meta(slug)["owner"]: raise PermissionError("only the project's owner changes who may see and change what (_project.json)")
    if how(slug, who, rel) == "direct":
        _write(slug, rel, text, _name(who))
        return {"done": "written"}
    rec = {"id": MN._new_id(), "t": MN._now(), "file": rel, "text": text, "why": why, "by": _name(who), "status": "pending"}
    earlier = [r for r in proposals(slug) if r["file"] == rel and r["by"] == _name(who)]   # this one is built on them (_base_text), so it replaces them: one answer takes in every change so far
    (_dir(slug) / ".muninn").mkdir(exist_ok=True)
    with open(_dir(slug) / ".muninn" / "proposals.jsonl", "a", encoding="utf-8") as f: f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [*({**r, "status": "superseded", "by_newer": rec["id"]} for r in earlier), rec]))
    return {"done": "proposed", "id": rec["id"], **({"replaces": [r["id"] for r in earlier]} if earlier else {})}

def _base_text(slug: str, rel: str, who) -> str:
    """The text a change by who starts from: their newest proposal for the file still waiting, else the file. So several changes to one file (issue after issue) build on each other and one answer takes them all."""
    mine = [r for r in proposals(slug) if r["file"] == rel and r["by"] == _name(who)]
    return mine[-1]["text"] if mine else read(slug, rel, who)

def proposals(slug: str, status: str = "pending") -> list:
    p = _dir(slug) / ".muninn" / "proposals.jsonl"
    rows = {r["id"]: r for r in (json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip())} if p.exists() else {}   # a later line for the same id is its answer
    return [r for r in rows.values() if not status or r["status"] == status]

def answer(slug: str, pid: str, accept: bool, who: str) -> dict:
    """The owner's answer to a proposal: accepted, it is written (the file's earlier version kept)."""
    if who != _meta(slug).get("owner", who): raise PermissionError("only the project's owner answers proposals")
    r = next(x for x in proposals(slug, "") if x["id"] == pid)
    if accept: _write(slug, r["file"], r["text"], f"""{r["by"]} (accepted by {who})""")
    with open(_dir(slug) / ".muninn" / "proposals.jsonl", "a", encoding="utf-8") as f: f.write(json.dumps({**r, "status": "accepted" if accept else "declined", "answered": MN._now()}, ensure_ascii=False) + "\n")
    return {"done": "accepted" if accept else "declined"}

# --- Issues (rows of Markdown tables, changed in place) ---

_ROW = re.compile(r"^\|\s*([A-Z]+-\d+)\s*\|")

def _cells(line: str) -> list: return [c.strip() for c in line.strip().strip("|").split("|")]

def issues(slug: str, who: str) -> list:
    """Every issue row of every Markdown file who may read, as who would have it (their changes still waiting included): {id, file, cells: {column: value}}. Columns come from the table's header line."""
    out = []
    for rel in [f for f in files(slug, who) if f.endswith(".md")]:
        head = []
        for line in _base_text(slug, rel, who).splitlines():   # with who's own changes still waiting: an issue it added can be worked on before the answer
            if line.startswith("|") and not _ROW.match(line) and not set(line) <= set("|-: "): head = [h.lower() for h in _cells(line)]
            elif _ROW.match(line) and "status" in head: out.append({"id": _ROW.match(line)[1], "file": rel, "cells": dict(zip(head, _cells(line)))})
    return out

def set_cell(slug: str, iid: str, column: str, value: str, who: str) -> dict:
    """One cell of an issue row changed in place (its status, where it is handled)."""
    it = next(i for i in issues(slug, who) if i["id"] == iid)
    lines, head = _base_text(slug, it["file"], who).splitlines(), []
    for n, line in enumerate(lines):
        if line.startswith("|") and not _ROW.match(line) and not set(line) <= set("|-: "): head = [h.lower() for h in _cells(line)]
        if _ROW.match(line) and _ROW.match(line)[1] == iid:
            cells = _cells(line)
            cells[head.index(column.lower())] = value.replace("|", "/")
            lines[n] = "| " + " | ".join(cells) + " |"
    return save(slug, it["file"], "\n".join(lines) + "\n", who, f"{iid}: {column} = {value}")

def issue_add(slug: str, rel: str, cells: dict, who) -> dict:
    """A new issue row at the end of the issue table in rel: the next id of the table's own prefix (I-07 after I-06), status "open" unless given, the other columns by name (any not named left empty). Written or proposed like any change (save)."""
    lines, head, last, ids = _base_text(slug, rel, who).splitlines(), [], None, []
    for n, line in enumerate(lines):
        if line.startswith("|") and not _ROW.match(line) and not set(line) <= set("|-: "): head = [h.lower() for h in _cells(line)]
        if _ROW.match(line) and "status" in head: last, ids = n, [*ids, _ROW.match(line)[1]]
    if last is None: raise ValueError(f"{rel} has no issue table yet (a Markdown table whose first column is an id like I-01 and that has a Status column)")
    pre, num = ids[-1].split("-")[0], max(int(i.split("-")[1]) for i in ids) + 1
    width = len(ids[-1].split("-")[1])
    iid, given = f"{pre}-{num:0{width}d}", {str(k).lower(): str(v).replace("|", "/").replace("\n", " ") for k, v in cells.items()}
    row = [iid if i == 0 else given.get(h, "open" if h == "status" else "") for i, h in enumerate(head)]
    lines.insert(last + 1, "| " + " | ".join(row) + " |")
    return {**save(slug, rel, "\n".join(lines) + "\n", who, f"""new issue {iid}: {given.get("title") or given.get(head[1] if len(head) > 1 else "", "")}"""), "id": iid}

def search(slug: str, text: str, who, most: int = 60) -> list:
    """Lines holding text (any case) in the files who may read: {file, line (from 1), text}."""
    t, out = text.lower(), []
    for rel in files(slug, who):
        try: lines = read(slug, rel, who).splitlines()
        except UnicodeDecodeError: continue   # a file that is not text
        out += [{"file": rel, "line": n, "text": l[:300]} for n, l in enumerate(lines, 1) if t in l.lower()]
        if len(out) >= most: break
    return out[:most]

# --- Work by AIs (Aviary step "project": a model's tools read and propose through its own mask, never beyond the person who started it) ---

CALLS = {"list": "the projects it may see", "files": "a project's files it may read", "read": "one file's text", "search": "lines holding a text", "issues": "the issue rows (id, file, status and the other columns)", "propose": "a changed or new file, kept as a proposal unless its mask lets it write", "issue_status": "an issue's status changed (kept as a proposal unless its mask lets it write)", "issue_add": "a new issue row in a file's issue table (next id, status open)"}

async def step(config: dict, inputs: dict, ctx) -> dict:
    """config: {"call": one of CALLS, "as": "agent:<profile>" (default agent:local), "args": {name: template over the step's inputs}, "give": name (result)}. The run's starter has to be allowed too: a run without a starter sees only projects that name no owner."""
    from tools.aviary import plan as AP
    agent, a = str(config.get("as") or "agent:local"), AP.render(config.get("args") or {}, inputs)
    if not agent.startswith("agent:"): raise PermissionError(f"a project step works as an AI (agent:<profile>), not as {agent}")
    who, call, slug = (agent, str(ctx.run.meta.get("owner") or "")), config["call"], MN.slug(str(a.get("project") or "")) if a.get("project") else ""
    if call not in CALLS: raise ValueError(f"""no project call {call}; the calls are: {", ".join(CALLS)}""")
    if call == "list": out = [{k: p[k] for k in ("slug", "name", "about")} for p in projects(who)]
    elif call == "files": out = files(slug, who)
    elif call == "read": out = read(slug, a["file"], who)
    elif call == "search": out = search(slug, a["text"], who)
    elif call == "issues": out = [{"id": i["id"], "file": i["file"], **i["cells"]} for i in issues(slug, who) if not a.get("status") or i["cells"].get("status", "").strip("* ").lower() == str(a["status"]).lower()]
    elif call == "propose": out = save(slug, a["file"], a["text"], who, a.get("why", ""))
    else: out = set_cell(slug, a["id"], "status", a["status"], who) if a["status"] in STATUSES else {"done": "refused", "why": f"""status is one of {", ".join(STATUSES)}"""}
    ctx.run.note("project", step=ctx.step["id"], call=call, project=slug, who=_name(who))
    return {config.get("give", "result"): out}

class ProjectTools:
    """The agent loop's toolset over projects (tools/aviary/agent_loop.py): the same calls as the step, as tools. config "projects": true (every project its mask shows) or [slugs]; "as": agent:<profile> (agent:local). Both the agent's mask and the run starter's rights have to allow each read and change."""
    def __init__(self, cfg, ctx):
        self.who, self.only = (str(cfg.get("as") or "agent:local"), str(ctx.run.meta.get("owner") or "")), cfg["projects"] if isinstance(cfg["projects"], list) else None
        if not self.who[0].startswith("agent:"): raise PermissionError(f"projects are worked on as an AI (agent:<profile>), not as {self.who[0]}")
    def _slug(self, a: dict) -> str:
        s = MN.slug(str(a.get("project") or ""))
        if self.only is not None and s not in self.only: raise PermissionError(f"this run works on {', '.join(self.only)}, not {s}")
        return s
    async def open(self) -> str: return f"""Projects: shared files (state, issues, decisions, code) you read and change through your mask; every change you propose waits for the project's owner. Start with project_list, then the project's STATE and its open issues. Working as {self.who[0]} for {self.who[1] or "nobody"}."""
    async def tools(self) -> list: return self.catalog({})
    @staticmethod
    def catalog(cfg: dict) -> list:
        t = lambda name, desc, props, ro: {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {k: {"type": "string", "description": d} for k, d in props.items()}, "required": [k for k in props if k not in ("status", "why")]}, "annotations": {"readOnlyHint": ro}}
        P_ = {"project": "its short name, as project_list gives it"}
        return [t("project_list", CALLS["list"], {}, True), t("project_files", CALLS["files"], P_, True), t("project_read", CALLS["read"], {**P_, "file": "path inside the project"}, True), t("project_search", CALLS["search"], {**P_, "text": "text to find"}, True),
                t("project_issues", CALLS["issues"], {**P_, "status": "only this status (open, built, test, proposal, done); empty for all"}, True), t("project_propose", CALLS["propose"], {**P_, "file": "path inside the project", "text": "the whole new text", "why": "one or two sentences for the owner"}, False), t("project_issue_status", CALLS["issue_status"], {**P_, "id": "issue id, like I-03", "status": "open, built, test, proposal or done"}, False),
                t("project_issue_add", CALLS["issue_add"], {**P_, "file": "the file holding the issue table", "columns": "JSON object of the row's columns by their header names, e.g. {\"title\": \"...\", \"area\": \"...\"}"}, False)]
    async def call(self, name, a):
        w = self.who
        if name == "project_list": return True, json.dumps([{k: p[k] for k in ("slug", "name", "about")} for p in projects(w) if self.only is None or p["slug"] in self.only], ensure_ascii=False)
        if name == "project_files": return True, "\n".join(files(self._slug(a), w)) or "(nothing it may read)"
        if name == "project_read": return True, read(self._slug(a), a["file"], w)
        if name == "project_search": return True, "\n".join(f"""{h["file"]}:{h["line"]}: {h["text"]}""" for h in search(self._slug(a), a["text"], w)) or "(no lines)"
        if name == "project_issues": return True, json.dumps([{"id": i["id"], "file": i["file"], **i["cells"]} for i in issues(self._slug(a), w) if not a.get("status") or i["cells"].get("status", "").strip("* ").lower() == str(a["status"]).lower()], ensure_ascii=False)
        if name == "project_propose": return True, json.dumps(save(self._slug(a), a["file"], a["text"], w, a.get("why", "")))
        if name == "project_issue_add":
            try: cols = json.loads(a["columns"]) if isinstance(a["columns"], str) else a["columns"]
            except json.JSONDecodeError as e: return False, f"columns is not a JSON object: {e}"
            return True, json.dumps(issue_add(self._slug(a), a["file"], cols, w))
        if a["status"] not in STATUSES: return False, f"""status is one of {", ".join(STATUSES)}"""
        return True, json.dumps(set_cell(self._slug(a), a["id"], "status", a["status"], w))
    async def close(self, status) -> dict: return {}

def new(slug: str, name: str, about: str, owner: str, masks: dict = None) -> str:
    """A project folder with its _project.json: the owner sees and changes everything; nobody else sees anything but what masks give (none unless given). Refused if the folder exists."""
    d = _dir(slug)
    if d.exists(): raise FileExistsError(f"a project {d.name} exists already")
    d.mkdir(parents=True)
    (d / "_project.json").write_text(json.dumps({"name": name or d.name, "about": about, "owner": owner, "masks": masks or {}}, indent=1, ensure_ascii=False))
    _journal(d.name, op="new", file="_project.json", author=owner)
    return d.name

# --- Files laid out as the portal's data/ ---

def placed(slug: str, who: str) -> list:
    """The project's data/... files and where they stand in the portal: missing (can be put in place), same, or differs."""
    return [{"file": r, "to": r, "state": "missing" if not (PORTAL_DATA.parent / r).exists() else "same" if (PORTAL_DATA.parent / r).read_bytes() == _path(slug, r).read_bytes() else "differs"} for r in files(slug, who) if r.startswith("data/")]

def put_in_place(slug: str, rel: str, who: str, replace: bool = False) -> dict:
    """A data/... file copied to the same path in the portal. An existing one is replaced only when asked, and kept first beside the project (.muninn/replaced/)."""
    if not rel.startswith("data/") or not _may(slug, who, "read", rel): raise PermissionError(f"{who} may not place {rel}")
    to = PORTAL_DATA.parent / rel
    if to.exists() and not replace: return {"done": "exists", "note": "a file is there already; compare, then replace if you mean to"}
    if to.exists():
        k = _dir(slug) / ".muninn" / "replaced" / time.strftime("%Y%m%d-%H%M%S") / rel
        k.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(to, k)
    to.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_path(slug, rel), to)
    _journal(slug, op="placed", file=rel, author=who, replaced=bool(replace))
    return {"done": "placed"}

# --- Export and import of a masked view ---

def export(slug: str, who: str) -> bytes:
    """A zip of what who sees of the project, with a note of the mask it was made through."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files(slug, who): z.write(_path(slug, rel), rel)
        z.writestr("_export.json", json.dumps({"project": slug, "through": who, "t": MN._now()}))
    return buf.getvalue()

def import_preview(slug: str, data: bytes, who: str) -> list:
    """What an import would do, file by file: new, changed, same, or refused (not writable by who)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [{"file": n, "state": "refused" if not _may(slug, who, "write", n) else "new" if not (_dir(slug) / n).exists() else "same" if (_dir(slug) / n).read_bytes() == z.read(n) else "changed"} for n in z.namelist() if not n.endswith("/") and n != "_export.json"]

def new_from(data: bytes, owner: str, slug: str = "") -> str:
    """A new project from an exported or starter zip: its _project.json gives the name, about and masks (the one making it is the owner, whatever the zip says), its other files are written as they are."""
    with zipfile.ZipFile(io.BytesIO(data)) as z: m = json.loads(z.read("_project.json")) if "_project.json" in z.namelist() else {}
    s = new(slug or m.get("slug") or m.get("name") or "project", m.get("name", ""), m.get("about", ""), owner, m.get("masks"))
    import_apply(s, data, owner, [n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n != "_project.json"])
    return s

def import_apply(slug: str, data: bytes, who: str, only: list = None) -> list:
    """Writes the new and changed files of an import that who may write (only: these of them), each earlier version kept; proposes instead where who may only propose."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [{**r, **save(slug, r["file"], z.read(r["file"]).decode("utf-8"), who, "import")} for r in import_preview(slug, data, who) if r["state"] in ("new", "changed") and (only is None or r["file"] in only)]
