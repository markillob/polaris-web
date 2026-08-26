# Polaris Web

Web front end for Polaris inventory data.

## Structure

```text
polaris-web/
├── README.md
├── index.html
└── site.html
```

## Run

Start the Flask server from the parent `mcp-files` directory:

```bash
cd /Users/mbarreraflores/mcp-files
python3 serve_polaris.py
```

Then open:

```text
http://127.0.0.1:8000/polaris-web/index.html
```

## Data Sources

This web app expects the separate `polaris` folder/repo to exist beside `serve_polaris.py`:

```text
./polaris/data/inventory.db
./polaris/data/enterprise_endpoints.db
```

Inventory data is served from:

```text
/api/inventory
/polaris/data/inventory.db
```

The SSH status panel loads:

```text
/polaris/data/CHECK_SSH_All_Sites_20260521_185956.csv
```

Endpoint data is served from:

```text
/api/enterprise_endpoints
/polaris/data/enterprise_endpoints.db
```
