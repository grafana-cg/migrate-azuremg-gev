import copy, json, os, shutil, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.dirname(__file__))
import grafana_migrate as gm
from mock_grafana import new_state, serve

PASS = []
def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    PASS.append(msg)


def seed(s):
    s["version"] = "12.2.1"
    for uid, title, parent in [("ops", "Ops", ""), ("ops-net", "Network", "ops"), ("apps", "Apps", "")]:
        s["folders"][uid] = {"uid": uid, "title": title, "parentUid": parent}
    s["datasources"] = {
        "azmon": {"uid": "azmon", "name": "Azure Monitor", "type": "grafana-azure-monitor-datasource", "access": "proxy",
                  "jsonData": {"azureAuthType": "msi"}, "isDefault": True},
        "prom": {"uid": "prom", "name": "Azure Prometheus", "type": "prometheus", "access": "proxy",
                 "url": "https://example.prometheus.monitor.azure.com", "jsonData": {}},
        "td": {"uid": "td", "name": "TestData", "type": "testdata", "access": "proxy", "jsonData": {}},
    }
    s["library"]["lp1"] = {"uid": "lp1", "name": "CPU panel", "kind": 1, "folderUid": "ops", "model": {"type": "timeseries"}, "version": 4}
    s["dashboards"] = {
        "d1": {"dashboard": {"uid": "d1", "title": "Ops overview", "panels": [{"libraryPanel": {"uid": "lp1"}}]}, "folderUid": "ops"},
        "d2": {"dashboard": {"uid": "d2", "title": "Network", "panels": []}, "folderUid": "ops-net"},
        "d3": {"dashboard": {"uid": "d3", "title": "Home", "panels": []}, "folderUid": ""},
        "d4": {"dashboard": {"uid": "d4", "title": "Azure VM insights", "panels": []}, "folderUid": "apps", "provisioned": True},
    }
    s["templates"]["slack.title"] = '{{ define "slack.title" }}[{{ .Status }}]{{ end }}'
    s["contact_points"]["slackcp"] = {"uid": "slackcp", "name": "Ops Slack", "type": "slack",
                                       "settings": {"url": "https://hooks.example.invalid/T000/B000/XXXX", "recipient": "#ops"},
                                       "disableResolveMessage": False}
    s["mute_timings"]["weekends"] = {"name": "weekends", "time_intervals": [{"weekdays": ["saturday", "sunday"]}]}
    s["policies"] = {"receiver": "grafana-default-email", "routes": [{"receiver": "Ops Slack", "object_matchers": [["team", "=", "ops"]]}]}
    rule = lambda uid, ds, rcv=None: {"uid": uid, "title": uid, "condition": "A", "isPaused": False,
                                       "data": [{"refId": "A", "datasourceUid": ds, "model": {}}],
                                       **({"notification_settings": {"receiver": rcv}} if rcv else {})}
    s["rule_groups"][("ops", "cpu")] = {"interval": 60, "rules": [rule("r1", "prom", "Ops Slack")]}
    s["rule_groups"][("apps", "latency")] = {"interval": 300, "rules": [rule("r2", "azmon"), rule("r3", "azmon")]}


def env(src_url, dst_url):
    os.environ.update(SRC_URL=src_url, SRC_TOKEN="src-test-token", DST_URL=dst_url, DST_TOKEN="dst-test-token")
    os.environ.pop("SRC_AUTH", None)


src = new_state(); seed(src)
srv_s, url_s = serve(src, "src-test-token")
work = Path(tempfile.mkdtemp())

# ---- Export is read-only and complete
dst1 = new_state(); srv_d1, url_d1 = serve(dst1, "dst-test-token")
env(url_s, url_d1)
before = copy.deepcopy({k: v for k, v in src.items() if k != "calls"})
gm.main(["export", "--out", str(work / "exp"), "--decrypt-secrets"])
check(all(m == "GET" for m, _ in src["calls"]), "export only issues GET requests to the source")
check({k: v for k, v in src.items() if k != "calls"} == before, "source state unchanged by export")
man = json.loads((work / "exp/manifest.json").read_text())
check(man["counts"] == {"folders": 3, "datasources": 3, "library_panels": 1, "dashboards": 4,
                        "contact_points": 2, "rule_groups": 2, "alert_rules": 3}, f"export counts {man['counts']}")
check("src-test-token" not in "".join(p.read_text() for p in (work / "exp").rglob("*") if p.is_file()), "token never written to disk")
sec = work / "exp" / gm.SECRETS_FILE
check(oct(sec.stat().st_mode)[-3:] == "600", "secrets file is mode 600")
check("*.secrets.json" in (work / "exp/.gitignore").read_text(), "secrets file is git-ignored")
cps = json.loads((work / "exp/alerting/contact-points.json").read_text())
check(all(c["settings"].get("url") != "https://hooks.example.invalid/T000/B000/XXXX" for c in cps), "main contact point file holds no secrets")
rg = json.loads((work / "exp/alerting/rule-groups.json").read_text())
check(all("id" not in r and "updated" not in r for g in rg for r in g["rules"]), "rule ids/timestamps stripped")
folders = json.loads((work / "exp/folders.json").read_text())
check([f["uid"] for f in folders].index("ops") < [f["uid"] for f in folders].index("ops-net"), "parent folders exported before children")

# ---- Dry run writes nothing
dst1["calls"].clear()
rc = gm.main(["import", "--from", str(work / "exp"), "--dry-run"])
check(rc == 0, "dry run exits 0")
check(all(m == "GET" for m, _ in dst1["calls"]), "dry run only issues GET requests to the target")
check(not dst1["dashboards"] and not dst1["folders"], "dry run created nothing")

# ---- Full import
rc = gm.main(["import", "--from", str(work / "exp")])
check(rc == 0, "full import exits 0")
check(set(dst1["folders"]) == {"ops", "ops-net", "apps"} and dst1["folders"]["ops-net"]["parentUid"] == "ops", "nested folders recreated with same UIDs")
check(set(dst1["datasources"]) == {"azmon", "prom", "td"}, "data sources recreated with same UIDs")
check(set(dst1["dashboards"]) == {"d1", "d2", "d3", "d4"} and dst1["dashboards"]["d2"]["folderUid"] == "ops-net", "dashboards in correct folders")
check("lp1" in dst1["library"], "library panel created before dependent dashboard")
check(dst1["contact_points"]["slackcp"]["settings"]["url"] == "https://hooks.example.invalid/T000/B000/XXXX", "contact point secret restored from secrets file")
check(sum(1 for c in dst1["contact_points"].values() if c["name"] == "grafana-default-email") == 1, "default contact point not duplicated")
check(dst1["policies"]["routes"][0]["receiver"] == "Ops Slack", "policy tree imported (target was default)")
check(all(r["isPaused"] for g in dst1["rule_groups"].values() for r in g["rules"]), "rules imported paused by default")
check(dst1["provenance_headers"] >= 6, "alerting writes send X-Disable-Provenance")

# ---- Idempotent re-run
dst1["calls"].clear()
rc = gm.main(["import", "--from", str(work / "exp")])
writes = [c for c in dst1["calls"] if c[0] != "GET"]
check(rc == 0 and not writes, f"second import is a no-op (writes: {writes})")

# ---- Overwrite updates library panel with target version
rc = gm.main(["import", "--from", str(work / "exp"), "--overwrite", "--skip", "alerting", "--skip", "rules"])
check(rc == 0 and dst1["library"]["lp1"]["version"] == 2, "overwrite patches library panel using target version")

# ---- Validate passes
rc = gm.main(["validate", "--from", str(work / "exp")])
check(rc == 0, "validate exits 0 after full import")

# ---- Cutover: unpause target, optional pause source
src["calls"].clear()
rc = gm.main(["cutover", "--from", str(work / "exp"), "--dry-run"])
check(rc == 0 and all(r["isPaused"] for g in dst1["rule_groups"].values() for r in g["rules"]), "cutover dry run changes nothing")
check(all(m == "GET" for m, _ in src["calls"]), "cutover without --pause-source never writes to source")
rc = gm.main(["cutover", "--from", str(work / "exp"), "--folder", "apps"])
check(not any(r["isPaused"] for r in dst1["rule_groups"][("apps", "latency")]["rules"]), "cutover wave unpauses apps rules")
check(all(r["isPaused"] for r in dst1["rule_groups"][("ops", "cpu")]["rules"]), "cutover wave leaves other folders paused")
rc = gm.main(["cutover", "--from", str(work / "exp"), "--folder", "ops", "--pause-source"])
check(src["rule_groups"][("ops", "cpu")]["rules"][0]["isPaused"] is True, "--pause-source pauses source rules")
check(not dst1["rule_groups"][("ops", "cpu")]["rules"][0]["isPaused"], "cutover unpauses target ops rules")
src["rule_groups"][("ops", "cpu")]["rules"][0]["isPaused"] = False  # restore source for later runs

# ---- Wave import into a fresh target: only the ops subtree
dst2 = new_state(); srv_d2, url_d2 = serve(dst2, "dst-test-token")
env(url_s, url_d2)
rc = gm.main(["import", "--from", str(work / "exp"), "--folder", "ops-net"])
check(rc == 0, "wave import exits 0")
check(set(dst2["folders"]) == {"ops", "ops-net"}, "wave creates selected folder plus its ancestors")
check(set(dst2["dashboards"]) == {"d2"}, "wave imports only dashboards in scope")
rc = gm.main(["validate", "--from", str(work / "exp"), "--folder", "ops-net"])
check(rc == 0, "wave validate passes for the wave scope")

# ---- Without secrets: errors are reported, exit code non-zero
exp2 = work / "exp-nosecrets"
gm.main(["export", "--out", str(exp2)])
check(not (exp2 / gm.SECRETS_FILE).exists(), "no secrets file without --decrypt-secrets")
dst3 = new_state(); srv_d3, url_d3 = serve(dst3, "dst-test-token")
env(url_s, url_d3)
rc = gm.main(["import", "--from", str(exp2)])
report = json.loads(sorted((exp2 / "reports").glob("import-*.json"))[-1].read_text())
errs = [r for r in report["actions"] if r["action"] == "error"]
check(rc == 1 and any(r["kind"] == "contact-point" for r in errs), "missing contact point secrets surface as errors (exit 1)")
check(any("secrets missing: url" in r["detail"] for r in report["actions"] if r["kind"] == "contact-point"), "report names the missing secret field")
rc = gm.main(["validate", "--from", str(exp2)])
check(rc == 1, "validate fails when rule groups are missing")

# ---- Skip provisioned dashboards
dst4 = new_state(); srv_d4, url_d4 = serve(dst4, "dst-test-token")
env(url_s, url_d4)
gm.main(["import", "--from", str(work / "exp"), "--skip-provisioned-dashboards"])
check("d4" not in dst4["dashboards"] and "d1" in dst4["dashboards"], "--skip-provisioned-dashboards skips source-provisioned dashboards")

# ---- Customised policy tree on target is not overwritten
dst4["policies"] = {"receiver": "grafana-default-email", "routes": [{"receiver": "grafana-default-email", "object_matchers": [["x", "=", "y"]]}]}
gm.main(["import", "--from", str(work / "exp")])
check(dst4["policies"]["routes"][0]["object_matchers"] == [["x", "=", "y"]], "customised target policy tree is preserved without --overwrite")

# ---- Read-only guard
g = gm.Grafana("source", url_s, "src-test-token", read_only=True)
try:
    g.request("POST", "/api/folders", {"uid": "x", "title": "x"}); check(False, "read-only guard")
except RuntimeError:
    check(True, "read-only client refuses writes")

print(f"\nALL {len(PASS)} CHECKS PASSED")
for p in PASS:
    print("  ok -", p)
