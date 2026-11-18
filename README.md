# Azure Managed Grafana to Grafana Enterprise migration tool

`grafana_migrate.py` copies Grafana configuration from Azure Managed Grafana to a self-managed Grafana Enterprise instance using only Grafana's classic HTTP API (`/api/...`). It needs Python 3.9 or later and nothing outside the standard library.

It automates the migration plan's inventory (phase 1) and copy-to-cutover steps (phases 3 to 7). Data sources, folders, library panels, dashboards and alerting are copied with their original UIDs, in dependency order. You can re-run it safely, and it checks the result afterwards.

## What it does

| Command | What it does | Writes to |
|---|---|---|
| `export` | Reads everything into a directory of JSON files you can commit to Git | Local disk only. The source is opened read-only. |
| `import --dry-run` | Shows every create, update and skip it would make | Nothing |
| `import` | Writes the export to the target in dependency order. Alert rules arrive **paused**. | Target |
| `validate` | Checks every exported UID exists on the target, and runs data source health checks | Nothing |
| `cutover` | Unpauses the migrated rules on the target. `--pause-source` also pauses them on the source. | Target (and source only with `--pause-source`) |

Import order: folders (parents first), data sources, library panels, dashboards, notification templates, contact points, mute timings, notification policy tree, alert rule groups.

**Behaviour that matters**

- **Re-runs are safe.** Anything already on the target is skipped unless you pass `--overwrite`.
- **Waves.** `--folder <uid>` limits import, validate and cutover to that folder and its subfolders, and creates the parent folders it needs. Use `--folder general` for root-level dashboards. Data sources and the global alerting configuration (templates, contact points, mute timings, policies) are applied on every run.
- **No duplicate pages.** Rules are imported paused, so both instances never notify at the same time. `cutover` turns them on, wave by wave if you pass `--folder`. Use `--no-pause-rules` to import them active.
- **Policy tree safety.** The notification policy tree is replaced only while the target still has Grafana's default tree. If someone has already customised it, the tree is skipped unless you pass `--overwrite`.
- **UI editing.** Alerting writes send `X-Disable-Provenance: true`, so teams can still edit migrated alerting in the UI.
- **Reports and exit codes.** Every run writes a JSON report to `<export>/reports/`. The exit code is 1 if any write failed or validation found gaps.

## Setup

1. **Grafana Enterprise target:**
   - Run Grafana 12.4 for the migration.
   - If any Azure data sources use managed identity, set `[azure] managed_identity_enabled = true` (or `workload_identity_enabled` on AKS) and grant the identity its Azure roles first.
2. **Service account tokens:**
   - Target: create an **Admin** service account token.
   - Source: create an Admin token on Azure Managed Grafana, or use an Entra ID identity with the Grafana Admin role (see below).
   - To copy contact point secrets, the source caller must be an org admin.
3. **Environment variables.** The tool never reads tokens from the command line and never writes them to disk.

```bash
export SRC_URL=https://<workspace>.grafana.azure.com
export SRC_TOKEN=<azure-managed-grafana-service-account-token>
# or: export SRC_AUTH=entra   (uses `az account get-access-token --resource https://dashboard.azure.com`)
export DST_URL=https://grafana.example.internal
export DST_TOKEN=<grafana-enterprise-service-account-token>
# optional: SRC_ORG_ID / DST_ORG_ID for a non-default Grafana org; --ca-file for an internal CA
```

## Runbook

```bash
# Phase 1: inventory (read-only) and a dry run
python3 grafana_migrate.py export --out ./export
cat ./export/manifest.json                      # source version and counts
python3 grafana_migrate.py import --from ./export --dry-run

# Pilot: one folder end to end
python3 grafana_migrate.py import   --from ./export --folder <pilot-folder-uid>
python3 grafana_migrate.py validate --from ./export --folder <pilot-folder-uid>

# Full copy (re-run any time; existing items are skipped)
python3 grafana_migrate.py export --out ./export --force --decrypt-secrets   # optional: contact point secrets
python3 grafana_migrate.py import   --from ./export
python3 grafana_migrate.py validate --from ./export
rm ./export/alerting/contact-points.secrets.json                              # delete secrets after import

# Phase 6: parallel run. Teams compare dashboards; rules stay paused on Grafana Enterprise.

# Phase 7: cutover, per wave or all at once
python3 grafana_migrate.py cutover --from ./export --folder <wave-folder-uid> --dry-run
python3 grafana_migrate.py cutover --from ./export --folder <wave-folder-uid> --pause-source
```

To pick up changes teams made in Azure Managed Grafana during the parallel run, export again and import with `--overwrite`.

## Running it in Azure DevOps

`azure-pipelines.yml` runs the same steps with review gates. The source is reached through an Azure service connection using an Entra ID token, so no source token is stored.

| Mode | Stages |
|---|---|
| `migrate` | **Plan:** export plus dry run, published as the `export` artifact for review. **Approval** on the `grafana-enterprise` environment. **Migrate:** import, validate, publish the `reports` artifact. |
| `cutover` | **Approval** on the `grafana-cutover` environment, then cutover for the selected folders. `pauseSource` optionally pauses the same rules on Azure Managed Grafana. |

Setup steps:
1. Create a variable group `grafana-migration` with `SRC_URL`, `DST_URL` and `DST_TOKEN`. Mark `DST_TOKEN` as secret.
2. Create a service connection. Its identity needs the Grafana Admin role on the Azure Managed Grafana workspace.
3. Create the two environments and add approval checks to them.
4. Run the pipeline with `folders` set for each wave.

Contact point secrets are only fetched when `includeContactPointSecrets` is true. They are fetched in the Migrate stage, used, and deleted from the agent, and are never published as an artifact.

## Not migrated

These need manual work or a separate step, as listed in the migration plan:
- **Rebuilt through Entra ID sign-in and team sync:** users, teams and roles, and folder and dashboard permissions.
- **Recreated by hand:** service accounts and tokens, playlists, annotations, reports, and instance settings.
- **Re-entered on the target:** data source secrets. Grafana never returns them through its API. `validate` reports any data source whose health check fails.
- **Imported with errors flagged:** contact point secrets when `--decrypt-secrets` isn't used. The report names the missing fields.
- **Not covered at all:** alert state history and silences, data-source-managed (Prometheus ruler) rules, and dashboards stored in the new v2 dashboard schema.
- **One Grafana org per run.** Run once per org, using `SRC_ORG_ID` and `DST_ORG_ID`.

## Notes

- **Token lifetime.** An Entra ID token from `SRC_AUTH=entra` lasts about an hour. For a very large export, use a service account token instead.
- **API support.** The classic API is documented for Grafana 12.4. Grafana deprecates it from Grafana 13 but still keeps it enabled, so migrate first and upgrade after cutover.
- **Tests.** `tests/run_tests.py` runs the whole flow against an in-memory mock of the Grafana API: export, dry run, import, re-run, overwrite, waves, validate, cutover, and the secrets handling (42 checks). Run it after changing the script. Still do a pilot-folder run against the real instances first.
