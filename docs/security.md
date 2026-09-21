# Security

## The API key

The installer needs one API key. `./install --print-api-key-request` prints the request that creates it with exactly these rights and
no others:

| Permission | Scope |
|---|---|
| cluster `manage_index_templates`, `manage_ingest_pipelines` | cluster-wide (Elasticsearch cannot limit these to a name prefix) |
| cluster `monitor` | read-only: version and node count |
| indices `manage`, `create_index`, `read`, `write`, `view_index_metadata` | only indices matching `kismet-cartographer-*` |
| Kibana `space_all` | only the one space you install into |

It cannot read or change other indices, users, roles, or other Kibana spaces. Because the two template/pipeline rights are cluster-wide,
the scripts add their own guard rails: they refuse to create, change or delete any resource whose name does not start with
`kismet-cartographer-`, and refuse to overwrite an existing template or pipeline that is not tagged as this project's. These are
safety nets against mistakes, not a security boundary: treat the key as able to manage templates and pipelines cluster-wide.

**Give the key an expiry** (the request sets 90 days), keep it in a file with mode 600 or in an environment variable, and invalidate it
when you are done installing:

```
DELETE /_security/api_key
{ "ids": ["<the id shown when you created it>"] }
```

### A narrower key for loading captures

Installing needs the rights above, but afterwards, loading and validating captures only touches the indices. If the machine that runs
the ingest is not the one you installed from, give it this instead (no cluster rights, no Kibana):

```
POST /_security/api_key
{
  "name": "kismet-cartographer-ingest",
  "expiration": "180d",
  "role_descriptors": {
    "ingest": {
      "indices": [{ "names": ["kismet-cartographer-*"],
                    "privileges": ["create_index", "create_doc", "index", "write", "read", "view_index_metadata"] }]
    }
  }
}
```

It cannot manage templates or pipelines or delete indices; it can delete documents (needed for `--purge`). Two answers you will
see with it and can ignore: `GET /` and `_refresh` return 403, and the scripts do not depend on them.

## Handling secrets

* Nothing secret is stored in this repository. Give the key with `--api-key-file`, the `ELASTIC_API_KEY` environment variable, or an
  env file (`.env.example`; keep it mode 600 and out of version control).
* **`--api-key` on the command line is visible to other users on the machine (`ps`) and stays in your shell history.** Prefer a file.
* The installer hands the key to its helper scripts through the environment, never through their command lines.
* `./install --save-config` writes the connection settings to a mode-600 `.env`, **without the key**: only the path of the file that holds
  it. If you gave the key on the command line (or in an environment variable) nothing about it is saved, and the installer says so.
* Scripts never print a key or an `Authorization` header, and a warning is shown if a key or env file is readable by other users.
* `.gitignore` excludes `.env`, `*.env`, `*.key`, `*.pem`, `*.crt`, your `wardrives/` directory, `out/`, `state/` and `exports/`.
  Check `git status` before you commit anything you did not write.
* **If a key or password was ever pasted into a chat, ticket, issue or commit, treat it as compromised** and invalidate it.

## TLS

Use HTTPS. If your stack does not have it, `--host` connects over plain `http://`; then the API key and all data cross the network
unencrypted, so only do that on a network you trust (the installer prints a warning).

Certificates are verified against the CA you pass with `--ca` (or the system trust store if you pass none), including the host name.
`--tls-server-name` changes only which name is checked (for `kubectl port-forward`); verification stays on. `--insecure` disables
verification entirely and is meant for throw-away tests: anyone on the network path could read the key and the data.

## The data itself is sensitive

Wardriving captures contain **your movements** (the GPS track), **other people's networks** (names, BSSIDs, client MAC addresses, and
probed SSIDs that can reveal where a device has been) and aircraft identities. Loading them makes all of it searchable and mappable.

* Restrict who can read `kismet-cartographer-*` (a dedicated Kibana space and a role with `read` on those indices only).
* Consider whether client MAC addresses and probed SSIDs should be kept at all; `wifi.probed_ssids` and the stored `kismet.raw` can be
  dropped by editing `kismet_cartographer/normalize.py` before loading.
* Snapshots and backups of these indices inherit the same sensitivity.
* Only capture and store data you are legally allowed to collect where you are. Rules on intercepting wireless traffic and on
  processing personal data (MAC addresses can be personal data) differ by country.
* Do not publish screenshots of the map or dashboards: they show where you drove.

## Input handling

* `.kismet` files are opened read-only and immutable. SQL is built only from a fixed column list, and values are bound or parsed,
  never interpolated into SQL.
* Source JSON is parsed with the standard `json` module and stored, never executed.
* A malformed file fails only itself, with a clear message and a non-zero exit code; other files are unaffected.
* Names used in URLs are fixed constants under the `kismet-cartographer-` prefix, not user input.

## Kibana

Saved objects are created only with ids starting `kismet-cartographer-`; existing objects with other ids are never modified. An
existing data view titled `kismet-cartographer-*` is reused, not replaced.
