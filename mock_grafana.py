"""In-memory mock of the Grafana classic HTTP API endpoints used by grafana_migrate.py."""
import copy
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

DEFAULT_POLICY = {"receiver": "grafana-default-email", "group_by": ["grafana_folder", "alertname"]}


def new_state(version="12.4.0"):
    return {
        "version": version,
        "folders": {},          # uid -> {uid,title,parentUid}
        "datasources": {},      # uid -> body (+id)
        "library": {},          # uid -> element
        "dashboards": {},       # uid -> {dashboard, folderUid, provisioned}
        "templates": {},        # name -> template
        "contact_points": {"defaultcp": {"uid": "defaultcp", "name": "grafana-default-email", "type": "email",
                                         "settings": {"addresses": "<example@email.com>"},
                                         "disableResolveMessage": False}},
        "secrets": {},          # cp uid -> secure settings
        "mute_timings": {},
        "policies": copy.deepcopy(DEFAULT_POLICY),
        "rule_groups": {},      # (folderUid, title) -> group
        "calls": [],
        "provenance_headers": 0,
    }


SECURE_KEYS = {"url", "token", "password"}


def make_handler(state, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj=None):
            data = json.dumps(obj if obj is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def route(self, method):
            if self.headers.get("Authorization") != f"Bearer {token}":
                return self.send(401, {"message": "Unauthorized"})
            u = urlparse(self.path)
            path, qs = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
            state["calls"].append((method, u.path))
            if self.headers.get("X-Disable-Provenance") == "true":
                state["provenance_headers"] += 1
            s = state
            b = self.body() if method in ("POST", "PUT", "PATCH") else None

            if path == "/api/health":
                return self.send(200, {"version": s["version"], "database": "ok"})

            # folders
            if path == "/api/folders" and method == "GET":
                parent = qs.get("parentUid", "")
                items = [f for f in s["folders"].values() if f["parentUid"] == parent]
                lim, page = int(qs.get("limit", 1000)), int(qs.get("page", 1))
                return self.send(200, [{"uid": f["uid"], "title": f["title"]} for f in items[(page-1)*lim:page*lim]])
            if path == "/api/folders" and method == "POST":
                if b["uid"] in s["folders"]:
                    return self.send(409, {"message": "folder exists"})
                if b.get("parentUid") and b["parentUid"] not in s["folders"]:
                    return self.send(400, {"message": "parent folder not found"})
                s["folders"][b["uid"]] = {"uid": b["uid"], "title": b["title"], "parentUid": b.get("parentUid", "")}
                return self.send(200, s["folders"][b["uid"]])

            # datasources
            if path == "/api/datasources" and method == "GET":
                return self.send(200, [{"uid": d["uid"], "name": d["name"], "type": d["type"]} for d in s["datasources"].values()])
            m = re.fullmatch(r"/api/datasources/uid/([^/]+)(/health)?", path)
            if m and method == "GET":
                d = s["datasources"].get(m.group(1))
                if not d:
                    return self.send(404, {"message": "not found"})
                if m.group(2):
                    if d["type"] == "testdata":
                        return self.send(404, {"message": "health not supported"})
                    ok = "secret" in json.dumps(d.get("secureJsonData", {})) or d["type"] != "secure-type"
                    return self.send(200, {"status": "OK" if ok else "ERROR", "message": "x"})
                return self.send(200, dict(d, id=7, orgId=1, readOnly=False, secureJsonFields={}))
            if path == "/api/datasources" and method == "POST":
                if b["uid"] in s["datasources"]:
                    return self.send(409, {"message": "exists"})
                if any(d["name"] == b["name"] for d in s["datasources"].values()):
                    return self.send(409, {"message": "data source with the same name already exists"})
                s["datasources"][b["uid"]] = b
                return self.send(200, {"datasource": b})
            if m and method == "PUT":
                s["datasources"][m.group(1)] = b
                return self.send(200, {"datasource": b})

            # library elements
            if path == "/api/library-elements" and method == "GET":
                els = list(s["library"].values())
                per, page = int(qs.get("perPage", 100)), int(qs.get("page", 1))
                return self.send(200, {"result": {"totalCount": len(els), "elements": els[(page-1)*per:page*per],
                                                  "page": page, "perPage": per}})
            if path == "/api/library-elements" and method == "POST":
                if b["uid"] in s["library"]:
                    return self.send(400, {"message": "exists"})
                s["library"][b["uid"]] = dict(b, version=1)
                return self.send(200, {"result": s["library"][b["uid"]]})
            m = re.fullmatch(r"/api/library-elements/([^/]+)", path)
            if m and method == "PATCH":
                cur = s["library"][m.group(1)]
                if b.get("version") != cur["version"]:
                    return self.send(412, {"message": "version mismatch"})
                s["library"][m.group(1)] = dict(b, version=cur["version"] + 1)
                return self.send(200, {"result": s["library"][m.group(1)]})

            # dashboards
            if path == "/api/search":
                items = [{"uid": u, "title": d["dashboard"]["title"], "folderUid": d["folderUid"], "type": "dash-db"}
                         for u, d in s["dashboards"].items()]
                lim, page = int(qs.get("limit", 5000)), int(qs.get("page", 1))
                return self.send(200, items[(page-1)*lim:page*lim])
            m = re.fullmatch(r"/api/dashboards/uid/([^/]+)", path)
            if m:
                d = s["dashboards"].get(m.group(1))
                if not d:
                    return self.send(404, {"message": "not found"})
                return self.send(200, {"dashboard": dict(d["dashboard"], id=99, version=3),
                                       "meta": {"folderUid": d["folderUid"], "provisioned": d.get("provisioned", False)}})
            if path == "/api/dashboards/db" and method == "POST":
                dash = b["dashboard"]
                if dash.get("id") is not None:
                    return self.send(400, {"message": "id must be null for a new dashboard"})
                if dash["uid"] in s["dashboards"] and not b.get("overwrite"):
                    return self.send(412, {"status": "name-exists"})
                fu = b.get("folderUid", "")
                if fu and fu not in s["folders"]:
                    return self.send(400, {"message": "folder not found"})
                for lp in [p.get("libraryPanel", {}).get("uid") for p in dash.get("panels", [])]:
                    if lp and lp not in s["library"]:
                        return self.send(400, {"message": f"library panel {lp} missing"})
                s["dashboards"][dash["uid"]] = {"dashboard": dash, "folderUid": fu}
                return self.send(200, {"status": "success", "uid": dash["uid"]})

            # alerting provisioning
            if path == "/api/v1/provisioning/templates" and method == "GET":
                return self.send(200, [{"name": n, "template": t, "provenance": ""} for n, t in s["templates"].items()])
            m = re.fullmatch(r"/api/v1/provisioning/templates/(.+)", path)
            if m and method == "PUT":
                s["templates"][m.group(1)] = b["template"]
                return self.send(202, {"name": m.group(1), "template": b["template"]})

            if path == "/api/v1/provisioning/contact-points" and method == "GET":
                out = []
                for c in s["contact_points"].values():
                    st = {k: ("[REDACTED]" if k in SECURE_KEYS else v) for k, v in c["settings"].items()}
                    out.append(dict(c, settings=st))
                return self.send(200, out)
            if path == "/api/v1/provisioning/contact-points/export":
                byname = {}
                for c in s["contact_points"].values():
                    st = dict(c["settings"]) if qs.get("decrypt") == "true" else \
                        {k: ("[REDACTED]" if k in SECURE_KEYS else v) for k, v in c["settings"].items()}
                    byname.setdefault(c["name"], []).append({"uid": c["uid"], "type": c["type"], "settings": st})
                return self.send(200, {"apiVersion": 1, "contactPoints": [
                    {"orgId": 1, "name": n, "receivers": r} for n, r in byname.items()]})
            if path == "/api/v1/provisioning/contact-points" and method == "POST":
                if b["type"] == "slack" and not b["settings"].get("url"):
                    return self.send(400, {"message": "slack: url or token required"})
                s["contact_points"][b["uid"]] = b
                return self.send(202, b)
            m = re.fullmatch(r"/api/v1/provisioning/contact-points/([^/]+)", path)
            if m and method == "PUT":
                s["contact_points"][m.group(1)] = b
                return self.send(202, b)

            if path == "/api/v1/provisioning/mute-timings" and method == "GET":
                return self.send(200, list(s["mute_timings"].values()))
            if path == "/api/v1/provisioning/mute-timings" and method == "POST":
                s["mute_timings"][b["name"]] = b
                return self.send(201, b)
            m = re.fullmatch(r"/api/v1/provisioning/mute-timings/(.+)", path)
            if m and method == "PUT":
                s["mute_timings"][m.group(1)] = b
                return self.send(200, b)

            if path == "/api/v1/provisioning/policies":
                if method == "GET":
                    return self.send(200, s["policies"])
                missing = {r.get("receiver") for r in b.get("routes", [])} - {c["name"] for c in s["contact_points"].values()}
                if missing:
                    return self.send(400, {"message": f"receivers missing: {missing}"})
                s["policies"] = b
                return self.send(202, {"message": "policies updated"})

            if path == "/api/v1/provisioning/alert-rules":
                rules = [dict(r, folderUID=k[0], ruleGroup=k[1]) for k, g in s["rule_groups"].items() for r in g["rules"]]
                return self.send(200, rules)
            m = re.fullmatch(r"/api/v1/provisioning/folder/([^/]+)/rule-groups/(.+)", path)
            if m:
                key = (m.group(1), m.group(2))
                if method == "GET":
                    g = s["rule_groups"].get(key)
                    if not g:
                        return self.send(404, {"message": "not found"})
                    return self.send(200, {"title": key[1], "folderUid": key[0], "interval": g["interval"],
                                           "rules": [dict(r, id=5, updated="2026-01-01T00:00:00Z", provenance="")
                                                     for r in g["rules"]]})
                if key[0] not in s["folders"]:
                    return self.send(400, {"message": "folder does not exist"})
                for r in b["rules"]:
                    if "id" in r:
                        return self.send(400, {"message": "unexpected id"})
                    for q in r.get("data", []):
                        if q.get("datasourceUid") not in s["datasources"] and q.get("datasourceUid") != "__expr__":
                            return self.send(400, {"message": f"datasource {q.get('datasourceUid')} missing"})
                    rcv = (r.get("notification_settings") or {}).get("receiver")
                    if rcv and rcv not in {c["name"] for c in s["contact_points"].values()}:
                        return self.send(400, {"message": f"receiver {rcv} missing"})
                s["rule_groups"][key] = {"interval": b["interval"], "rules": b["rules"]}
                return self.send(200, b)

            return self.send(404, {"message": f"mock: no route {method} {path}"})

        def do_GET(self): self.route("GET")
        def do_POST(self): self.route("POST")
        def do_PUT(self): self.route("PUT")
        def do_PATCH(self): self.route("PATCH")
    return H


def serve(state, token):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
