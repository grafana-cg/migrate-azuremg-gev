#!/usr/bin/env python3
"""Migrate Grafana configuration from Azure Managed Grafana to self-managed Grafana Enterprise.

Uses only Grafana's classic HTTP API (/api/...) and the Python standard library.

Subcommands:
  export    Read folders, data sources, library panels, dashboards and alerting from the source into a directory.
  import    Write an export directory into the target, in dependency order. Supports --dry-run and waves (--folder).
  validate  Compare an export directory with the target and run data source health checks.
  cutover   Unpause migrated alert rules on the target (optionally pause them on the source).

Connection settings come from environment variables:
  SRC_URL, SRC_TOKEN   source (Azure Managed Grafana) URL and service account token
  DST_URL, DST_TOKEN   target (Grafana Enterprise) URL and service account token
  SRC_ORG_ID, DST_ORG_ID  optional Grafana org IDs (default: the token's org)
Set SRC_AUTH=entra to fetch a Microsoft Entra ID token with the Azure CLI instead of SRC_TOKEN.
"""

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger("grafana-migrate")
ENTRA_AUDIENCE = "https://dashboard.azure.com"
REDACTED = "[REDACTED]"
SECRETS_FILE = "alerting/contact-points.secrets.json"
ALERTING_WRITE_HEADERS = {"X-Disable-Provenance": "true"}


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")


class Grafana:
    """Minimal Grafana HTTP API client with retries. Never logs the token."""

    def __init__(self, name, url, token, org_id=None, ca_file=None, timeout=60, read_only=False):
        if not url or not token:
            raise SystemExit(f"{name}: URL and token are required (see --help for environment variables)")
        self.name = name
        self.base = url.rstrip("/")
        self.token = token
        self.org_id = org_id
        self.timeout = timeout
        self.read_only = read_only
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    def request(self, method, path, body=None, headers=None, ok_404=False):
        if self.read_only and method != "GET":
            raise RuntimeError(f"{self.name} is read-only; refusing {method} {path}")
        url = self.base + path
        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if self.org_id:
            hdrs["X-Grafana-Org-Id"] = str(self.org_id)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    raw = resp.read().decode() or "null"
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 404 and ok_404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(method, path, e.code, text) from None
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"{self.name}: cannot reach {self.base}: {e.reason}") from None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)


# ---------- helpers ----------

def q(value):
    return urllib.parse.quote(str(value), safe="")


def write_json(path, obj, secret=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    if secret:
        os.chmod(path, 0o600)


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def entra_token():
    if not shutil.which("az"):
        raise SystemExit("SRC_AUTH=entra needs the Azure CLI (az) on PATH")
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", ENTRA_AUDIENCE, "--query", "accessToken", "-o", "tsv"],
        check=True, capture_output=True, text=True)
    return out.stdout.strip()


def source_client(args, read_only=True):
    token = entra_token() if os.environ.get("SRC_AUTH", "").lower() == "entra" else os.environ.get("SRC_TOKEN")
    return Grafana("source", os.environ.get("SRC_URL"), token, os.environ.get("SRC_ORG_ID"),
                   args.ca_file, read_only=read_only)


def target_client(args):
    return Grafana("target", os.environ.get("DST_URL"), os.environ.get("DST_TOKEN"),
                   os.environ.get("DST_ORG_ID"), args.ca_file)


def list_folders(g):
    """All folders, parents before children (nested folders supported)."""
    out, queue = [], [None]
    while queue:
        parent = queue.pop(0)
        page = 1
        while True:
            qs = f"?limit=1000&page={page}" + (f"&parentUid={q(parent)}" if parent else "")
            items = g.get("/api/folders" + qs) or []
            for f in items:
                out.append({"uid": f["uid"], "title": f["title"], "parentUid": parent or ""})
                queue.append(f["uid"])
            if len(items) < 1000:
                break
            page += 1
    return out


def list_library_panels(g):
    out, page = [], 1
    while True:
        res = (g.get(f"/api/library-elements?kind=1&perPage=100&page={page}") or {}).get("result", {})
        els = res.get("elements", [])
        out.extend(els)
        if not els or len(out) >= res.get("totalCount", 0):
            return out
        page += 1


def search_dashboards(g):
    out, page = [], 1
    while True:
        items = g.get(f"/api/search?type=dash-db&limit=5000&page={page}") or []
        out.extend(items)
        if len(items) < 5000:
            return out
        page += 1


def list_rule_group_keys(g):
    rules = g.get("/api/v1/provisioning/alert-rules") or []
    return sorted({(r["folderUID"], r["ruleGroup"]) for r in rules})


def rule_group_path(folder_uid, group):
    return f"/api/v1/provisioning/folder/{q(folder_uid)}/rule-groups/{q(group)}"


def clean_rule_group(rg, paused=None):
    rg = dict(rg)
    rules = []
    for r in rg.get("rules", []):
        r = {k: v for k, v in r.items() if k not in ("id", "updated", "provenance")}
        if paused is not None:
            r["isPaused"] = paused
        rules.append(r)
    rg["rules"] = rules
    return rg


DS_FIELDS = ("uid", "name", "type", "access", "url", "user", "database", "basicAuth", "basicAuthUser",
             "withCredentials", "isDefault", "jsonData")


# ---------- export ----------

def cmd_export(args):
    src = source_client(args, read_only=True)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} is not empty; use --force to overwrite")
    out.mkdir(parents=True, exist_ok=True)
    (out / ".gitignore").write_text("*.secrets.json\nreports/\n")
    counts = {}

    health = src.get("/api/health") or {}
    LOG.info("Source Grafana version %s", health.get("version", "unknown"))

    folders = list_folders(src)
    write_json(out / "folders.json", folders)
    counts["folders"] = len(folders)

    datasources = []
    for d in src.get("/api/datasources") or []:
        full = src.get(f"/api/datasources/uid/{q(d['uid'])}")
        datasources.append({k: full.get(k) for k in DS_FIELDS if k in full})
    write_json(out / "datasources.json", datasources)
    # Same content as a Grafana provisioning file (JSON is valid YAML). Secrets must be added before use.
    write_json(out / "provisioning-datasources.yaml",
               {"apiVersion": 1, "datasources": [dict(d, editable=True) for d in datasources]})
    counts["datasources"] = len(datasources)

    panels = list_library_panels(src)
    for p in panels:
        write_json(out / "library-panels" / f"{p['uid']}.json",
                   {k: p.get(k) for k in ("uid", "name", "kind", "folderUid", "model")})
    counts["library_panels"] = len(panels)

    dashboards = search_dashboards(src)
    for item in dashboards:
        res = src.get(f"/api/dashboards/uid/{q(item['uid'])}")
        meta = res.get("meta", {})
        write_json(out / "dashboards" / f"{item['uid']}.json", {
            "dashboard": res["dashboard"],
            "folderUid": meta.get("folderUid", ""),
            "provisioned": bool(meta.get("provisioned")),
        })
    counts["dashboards"] = len(dashboards)

    write_json(out / "alerting/templates.json", src.get("/api/v1/provisioning/templates") or [])
    cps = src.get("/api/v1/provisioning/contact-points") or []
    write_json(out / "alerting/contact-points.json", cps)
    if args.decrypt_secrets:
        exp = src.get("/api/v1/provisioning/contact-points/export?decrypt=true&format=json") or {}
        secrets = {r["uid"]: r.get("settings", {})
                   for cp in exp.get("contactPoints", []) for r in cp.get("receivers", [])}
        write_json(out / SECRETS_FILE, secrets, secret=True)
        LOG.warning("Decrypted contact point settings written to %s (mode 600, git-ignored). "
                    "Delete it after the import.", out / SECRETS_FILE)
    write_json(out / "alerting/mute-timings.json", src.get("/api/v1/provisioning/mute-timings") or [])
    write_json(out / "alerting/policies.json", src.get("/api/v1/provisioning/policies") or {})
    groups = []
    for folder_uid, group in list_rule_group_keys(src):
        rg = src.get(rule_group_path(folder_uid, group))
        groups.append(clean_rule_group(rg))
    write_json(out / "alerting/rule-groups.json", groups)
    counts.update(contact_points=len(cps), rule_groups=len(groups),
                  alert_rules=sum(len(g["rules"]) for g in groups))

    write_json(out / "manifest.json", {
        "source_url": src.base, "source_version": health.get("version"),
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts": counts, "secrets_included": bool(args.decrypt_secrets),
    })
    LOG.info("Export complete: %s", json.dumps(counts))


# ---------- import ----------

class Plan:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.rows = []

    def record(self, kind, key, action, detail=""):
        self.rows.append({"kind": kind, "key": key, "action": action, "detail": detail})
        level = logging.ERROR if action == "error" else logging.INFO
        LOG.log(level, "%-8s %-15s %s %s", action.upper(), kind, key, detail)

    def run(self, kind, key, action, fn, detail=""):
        """Record an action and execute it unless this is a dry run."""
        if action == "skip" or self.dry_run:
            self.record(kind, key, action if action == "skip" else f"would-{action}", detail)
            return
        try:
            fn()
            self.record(kind, key, action, detail)
        except ApiError as e:
            self.record(kind, key, "error", f"{detail}; {e}" if detail else str(e))

    def summary(self):
        out = {}
        for r in self.rows:
            out.setdefault(r["kind"], {}).setdefault(r["action"], 0)
            out[r["kind"]][r["action"]] += 1
        return out


def folder_scope(folders, selected):
    """UIDs of the selected folders plus all their descendants. None means everything."""
    if not selected:
        return None
    children = {}
    for f in folders:
        children.setdefault(f["parentUid"], []).append(f["uid"])
    scope, stack = set(), [s for s in selected if s != "general"]
    if "general" in selected:
        scope.add("")  # the root "General" level
    while stack:
        uid = stack.pop()
        if uid not in scope:
            scope.add(uid)
            stack.extend(children.get(uid, []))
    return scope


def ancestors(folders, uids):
    parent = {f["uid"]: f["parentUid"] for f in folders}
    out = set()
    for uid in uids:
        p = parent.get(uid)
        while p:
            out.add(p)
            p = parent.get(p)
    return out


def cmd_import(args):
    src_dir = Path(args.source_dir)
    if not (src_dir / "manifest.json").exists():
        raise SystemExit(f"{src_dir} is not an export directory (manifest.json missing)")
    dst = target_client(args)
    plan = Plan(args.dry_run)
    skip = set(args.skip or [])
    overwrite = args.overwrite
    pause = not args.no_pause_rules

    LOG.info("Target Grafana version %s%s", (dst.get("/api/health") or {}).get("version", "unknown"),
             " (dry run: no changes will be made)" if args.dry_run else "")

    folders = read_json(src_dir / "folders.json", [])
    scope = folder_scope(folders, args.folder)
    in_scope = (lambda uid: True) if scope is None else (lambda uid: uid in scope)

    # 1. Folders (parents first; ancestors of a wave are created too)
    if "folders" not in skip:
        needed = None if scope is None else scope | ancestors(folders, scope)
        existing = {f["uid"] for f in list_folders(dst)}
        for f in folders:
            if needed is not None and f["uid"] not in needed:
                continue
            body = {"uid": f["uid"], "title": f["title"]}
            if f["parentUid"]:
                body["parentUid"] = f["parentUid"]
            action = "skip" if f["uid"] in existing else "create"
            plan.run("folder", f["uid"], action, lambda b=body: dst.request("POST", "/api/folders", b), f["title"])

    # 2. Data sources (global, not filtered by wave)
    if "datasources" not in skip:
        existing = {d["uid"] for d in dst.get("/api/datasources") or []}
        for d in read_json(src_dir / "datasources.json", []):
            body = {k: v for k, v in d.items() if v is not None}
            if d["uid"] in existing:
                action = "update" if overwrite else "skip"
                fn = lambda b=body: dst.request("PUT", f"/api/datasources/uid/{q(b['uid'])}", b)
            else:
                action, fn = "create", (lambda b=body: dst.request("POST", "/api/datasources", b))
            plan.run("datasource", d["uid"], action, fn, f"{d.get('name')} ({d.get('type')})")

    # 3. Library panels
    if "library-panels" not in skip:
        existing = {p["uid"]: p for p in list_library_panels(dst)}
        for path in sorted((src_dir / "library-panels").glob("*.json")):
            p = read_json(path)
            if not in_scope(p.get("folderUid") or ""):
                continue
            body = {k: v for k, v in p.items() if v is not None}
            if not body.get("folderUid"):
                body.pop("folderUid", None)
            if p["uid"] in existing:
                action = "update" if overwrite else "skip"
                body["version"] = existing[p["uid"]].get("version", 1)
                fn = lambda b=body: dst.request("PATCH", f"/api/library-elements/{q(b['uid'])}", b)
            else:
                action, fn = "create", (lambda b=body: dst.request("POST", "/api/library-elements", b))
            plan.run("library-panel", p["uid"], action, fn, p.get("name", ""))

    # 4. Dashboards
    if "dashboards" not in skip:
        existing = {d["uid"] for d in search_dashboards(dst)}
        for path in sorted((src_dir / "dashboards").glob("*.json")):
            rec = read_json(path)
            folder_uid = rec.get("folderUid") or ""
            if not in_scope(folder_uid):
                continue
            dash = dict(rec["dashboard"])
            if rec.get("provisioned") and args.skip_provisioned_dashboards:
                plan.record("dashboard", dash["uid"], "skip", "provisioned on source")
                continue
            dash["id"] = None
            dash.pop("version", None)
            body = {"dashboard": dash, "overwrite": overwrite, "message": "Migrated from Azure Managed Grafana"}
            if folder_uid:
                body["folderUid"] = folder_uid
            action = ("update" if overwrite else "skip") if dash["uid"] in existing else "create"
            plan.run("dashboard", dash["uid"], action,
                     lambda b=body: dst.request("POST", "/api/dashboards/db", b), dash.get("title", ""))

    # 5-8. Global alerting configuration
    if "alerting" not in skip:
        import_alerting_config(dst, src_dir, plan, overwrite)

    # 9. Alert rule groups
    if "rules" not in skip:
        existing = set(list_rule_group_keys(dst))
        for rg in read_json(src_dir / "alerting/rule-groups.json", []):
            if not in_scope(rg["folderUid"]):
                continue
            key = (rg["folderUid"], rg["title"])
            body = clean_rule_group(rg, paused=True if pause else None)
            action = ("update" if overwrite else "skip") if key in existing else "create"
            plan.run("rule-group", "/".join(key), action,
                     lambda b=body, k=key: dst.request("PUT", rule_group_path(*k), b, headers=ALERTING_WRITE_HEADERS),
                     f"{len(body['rules'])} rules" + (", paused" if pause else ""))

    write_report(src_dir, "import", plan)
    return 1 if any(r["action"] == "error" for r in plan.rows) else 0


def import_alerting_config(dst, src_dir, plan, overwrite):
    # 5. Notification templates
    existing = {t["name"] for t in dst.get("/api/v1/provisioning/templates") or []}
    for t in read_json(src_dir / "alerting/templates.json", []):
        action = ("update" if overwrite else "skip") if t["name"] in existing else "create"
        plan.run("template", t["name"], action,
                 lambda t=t: dst.request("PUT", f"/api/v1/provisioning/templates/{q(t['name'])}",
                                         {"template": t["template"]}, headers=ALERTING_WRITE_HEADERS))

    # 6. Contact points (secrets merged from the optional secrets file)
    secrets = read_json(src_dir / SECRETS_FILE, {})
    target_cps = dst.get("/api/v1/provisioning/contact-points") or []
    by_uid = {c["uid"]: c for c in target_cps}
    by_name_type = {(c["name"], c["type"]): c for c in target_cps}
    for cp in read_json(src_dir / "alerting/contact-points.json", []):
        settings = dict(cp.get("settings") or {})
        settings.update(secrets.get(cp["uid"], {}))
        missing = sorted(k for k, v in settings.items() if v == REDACTED)
        settings = {k: v for k, v in settings.items() if v != REDACTED}
        body = {"uid": cp["uid"], "name": cp["name"], "type": cp["type"], "settings": settings,
                "disableResolveMessage": cp.get("disableResolveMessage", False)}
        match = by_uid.get(cp["uid"]) or by_name_type.get((cp["name"], cp["type"]))
        detail = f"{cp['name']} ({cp['type']})" + (f"; secrets missing: {', '.join(missing)}" if missing else "")
        if match:
            body["uid"] = match["uid"]
            action = "update" if overwrite else "skip"
            fn = lambda b=body: dst.request("PUT", f"/api/v1/provisioning/contact-points/{q(b['uid'])}", b,
                                            headers=ALERTING_WRITE_HEADERS)
        else:
            action = "create"
            fn = lambda b=body: dst.request("POST", "/api/v1/provisioning/contact-points", b,
                                            headers=ALERTING_WRITE_HEADERS)
        plan.run("contact-point", cp["uid"], action, fn, detail)

    # 7. Mute timings
    existing = {m["name"] for m in dst.get("/api/v1/provisioning/mute-timings") or []}
    for m in read_json(src_dir / "alerting/mute-timings.json", []):
        body = {"name": m["name"], "time_intervals": m.get("time_intervals", [])}
        if m["name"] in existing:
            action = "update" if overwrite else "skip"
            fn = lambda b=body: dst.request("PUT", f"/api/v1/provisioning/mute-timings/{q(b['name'])}", b,
                                            headers=ALERTING_WRITE_HEADERS)
        else:
            action = "create"
            fn = lambda b=body: dst.request("POST", "/api/v1/provisioning/mute-timings", b,
                                            headers=ALERTING_WRITE_HEADERS)
        plan.run("mute-timing", m["name"], action, fn)

    # 8. Notification policy tree: replaced only if the target still has a default tree, or with --overwrite
    policies = read_json(src_dir / "alerting/policies.json", {})
    if policies:
        current = dst.get("/api/v1/provisioning/policies") or {}
        is_default = not current.get("routes")
        action = "update" if (is_default or overwrite) else "skip"
        detail = "replaces the whole tree" if action == "update" else "target tree already customised; use --overwrite"
        plan.run("policies", "tree", action,
                 lambda: dst.request("PUT", "/api/v1/provisioning/policies", policies, headers=ALERTING_WRITE_HEADERS),
                 detail)


def report_stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def write_report(src_dir, name, plan):
    stamp = report_stamp()
    path = src_dir / "reports" / f"{name}-{stamp}{'-dry-run' if plan.dry_run else ''}.json"
    write_json(path, {"summary": plan.summary(), "actions": plan.rows})
    LOG.info("Summary: %s", json.dumps(plan.summary()))
    LOG.info("Report written to %s", path)


# ---------- validate ----------

def cmd_validate(args):
    src_dir = Path(args.source_dir)
    dst = target_client(args)
    folders = read_json(src_dir / "folders.json", [])
    scope = folder_scope(folders, args.folder)
    in_scope = (lambda uid: True) if scope is None else (lambda uid: uid in scope)
    problems, results = 0, {}

    def compare(kind, expected, actual):
        nonlocal problems
        missing = sorted(set(expected) - set(actual))
        results[kind] = {"expected": len(set(expected)), "missing": missing}
        problems += len(missing)
        LOG.info("%-15s expected %4d  missing %4d%s", kind, len(set(expected)), len(missing),
                 f"  e.g. {missing[:5]}" if missing else "")

    compare("folders", [f["uid"] for f in folders if scope is None or f["uid"] in scope],
            [f["uid"] for f in list_folders(dst)])
    compare("datasources", [d["uid"] for d in read_json(src_dir / "datasources.json", [])],
            [d["uid"] for d in dst.get("/api/datasources") or []])
    compare("library-panels", [read_json(p)["uid"] for p in (src_dir / "library-panels").glob("*.json")
                               if in_scope(read_json(p).get("folderUid") or "")],
            [p["uid"] for p in list_library_panels(dst)])
    compare("dashboards", [read_json(p)["dashboard"]["uid"] for p in (src_dir / "dashboards").glob("*.json")
                           if in_scope(read_json(p).get("folderUid") or "")],
            [d["uid"] for d in search_dashboards(dst)])
    compare("rule-groups", ["/".join((g["folderUid"], g["title"]))
                            for g in read_json(src_dir / "alerting/rule-groups.json", []) if in_scope(g["folderUid"])],
            ["/".join(k) for k in list_rule_group_keys(dst)])

    health = {}
    for d in read_json(src_dir / "datasources.json", []):
        try:
            res = dst.get(f"/api/datasources/uid/{q(d['uid'])}/health") or {}
            health[d["uid"]] = res.get("status", "unknown")
        except ApiError as e:
            health[d["uid"]] = "not-supported" if e.status in (404, 501) else f"error: HTTP {e.status}"
    failing = {k: v for k, v in health.items() if v not in ("OK", "not-supported")}
    results["datasource_health"] = health
    LOG.info("datasource health: %d OK, %d failing%s", sum(v == "OK" for v in health.values()), len(failing),
             f" {failing}" if failing else "")

    write_json(src_dir / "reports" / f"validate-{report_stamp()}.json", results)
    return 1 if problems or failing else 0


# ---------- cutover ----------

def set_paused(g, paused, plan, scope_fn):
    for folder_uid, group in list_rule_group_keys(g):
        if not scope_fn(folder_uid):
            continue
        rg = g.get(rule_group_path(folder_uid, group))
        if all(r.get("isPaused") == paused for r in rg.get("rules", [])):
            plan.record("rule-group", f"{g.name}:{folder_uid}/{group}", "skip", "already " + ("paused" if paused else "active"))
            continue
        body = clean_rule_group(rg, paused=paused)
        plan.run("rule-group", f"{g.name}:{folder_uid}/{group}", "pause" if paused else "unpause",
                 lambda b=body, f=folder_uid, n=group: g.request("PUT", rule_group_path(f, n), b,
                                                                 headers=ALERTING_WRITE_HEADERS))


def cmd_cutover(args):
    src_dir = Path(args.source_dir)
    folders = read_json(src_dir / "folders.json", [])
    scope = folder_scope(folders, args.folder)
    in_scope = (lambda uid: True) if scope is None else (lambda uid: uid in scope)
    plan = Plan(args.dry_run)
    if args.pause_source:
        set_paused(source_client(args, read_only=False), True, plan, in_scope)
    set_paused(target_client(args), False, plan, in_scope)
    write_report(src_dir, "cutover", plan)
    return 1 if any(r["action"] == "error" for r in plan.rows) else 0


# ---------- CLI ----------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ca-file", help="CA bundle for TLS verification (for an internal CA on the target)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="export the source into a directory (read-only on the source)")
    e.add_argument("--out", required=True)
    e.add_argument("--force", action="store_true", help="write into a non-empty directory")
    e.add_argument("--decrypt-secrets", action="store_true",
                   help="also export decrypted contact point settings to a git-ignored file (needs org admin)")

    for name, help_text in (("import", "write an export into the target"),
                            ("validate", "compare an export with the target"),
                            ("cutover", "unpause rules on the target (and optionally pause them on the source)")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--from", dest="source_dir", required=True, help="export directory")
        s.add_argument("--folder", action="append",
                       help="limit to this folder UID and its subfolders (repeatable; 'general' = root level)")
        if name in ("import", "cutover"):
            s.add_argument("--dry-run", action="store_true", help="show what would change without writing")
        if name == "import":
            s.add_argument("--overwrite", action="store_true", help="update resources that already exist on the target")
            s.add_argument("--no-pause-rules", action="store_true",
                           help="import alert rules active (default: paused, to avoid duplicate notifications)")
            s.add_argument("--skip-provisioned-dashboards", action="store_true",
                           help="skip dashboards that were file-provisioned on the source")
            s.add_argument("--skip", action="append",
                           choices=["folders", "datasources", "library-panels", "dashboards", "alerting", "rules"])
        if name == "cutover":
            s.add_argument("--pause-source", action="store_true",
                           help="also pause the same rule groups on the source (writes to the source)")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    handler = {"export": cmd_export, "import": cmd_import, "validate": cmd_validate, "cutover": cmd_cutover}
    return handler[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
