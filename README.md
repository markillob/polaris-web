# Polaris Web

Local Flask web server and static UI for browsing Polaris network inventory, endpoints, routes, site photos, wireless access points, and endpoint feed data.

## Run Locally

Install the Python dependency:

```bash
pip install -r requirements.txt
```

Start the server from this repository root:

```bash
cd /Users/mbarreraflores/mcp-files
python3 serve_polaris.py
```

Open:

```text
http://127.0.0.1:8000/polaris-web/index.html
```

## Layout

```text
.
├── serve_polaris.py        Flask server and API routes
├── requirements.txt        Python runtime dependencies
├── polaris-web/            Static HTML/CSS/JS web app
└── polaris/data/           Local data files, databases, and uploaded photos
```

## Required Data

The app expects these local files under `polaris/data/`:

```text
polaris/data/config.yaml
polaris/data/inventory.db
polaris/data/enterprise_endpoints.db
polaris/data/enterprise_routing.json
polaris/data/site_operations.json
polaris/data/site_photos/
```

`config.yaml` must include:

```yaml
site_name:
  main_site: polaris
```

## Main API Routes

```text
/api/config
/api/inventory
/api/<site_id>/devices
/api/enterprise_endpoints
/api/feed
/api/endpoint_vlans
/api/site_photos
/api/wireless_floorplan
/api/access_point_photos
```

## Data Handling

`polaris/data/*` is intentionally ignored by git because it can contain generated output and sensitive site data. Keep required databases and generated CSV/JSON/TXT files there when running locally.

More detailed field requirements are documented in:

```text
polaris-web/README.md
```
