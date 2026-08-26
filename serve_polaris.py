#!/usr/bin/env python3
import sqlite3
from argparse import ArgumentParser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "polaris-web"
DATA_ROOT = ROOT / "polaris" / "data"
DB_PATH = DATA_ROOT / "enterprise_endpoints.db"
INVENTORY_DB_PATH = DATA_ROOT / "inventory.db"
PHOTO_ROOT = DATA_ROOT / "site_photos"
ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


@app.errorhandler(HTTPException)
def json_http_error(error):
    response = error.get_response()
    response.data = jsonify({
        "error": error.name,
        "message": error.description,
        "status": error.code,
    }).data
    response.content_type = "application/json"
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(error):
    return jsonify({
        "error": "request too large",
        "message": "Upload is larger than 100 MB. Upload fewer or smaller photos.",
        "status": 413,
    }), 413


ENDPOINT_COLUMNS = """
    device_fqdn,
    device_ip,
    device_port,
    entry_ip,
    entry_mac,
    entry_vlan,
    entry_port,
    hostname,
    site,
    description,
    active,
    status,
    error,
    date,
    first_seen,
    last_seen
"""

INVENTORY_COLUMNS = """
    id,
    imported_at,
    site,
    fqdn,
    ip_address,
    tech_room,
    serial_number,
    access,
    device_type,
    device_role,
    location,
    device_model,
    os_version,
    env_type,
    isp_provider,
    isp_type,
    isp_speed,
    isp_circuit_id,
    ha_status,
    ha,
    l3_gateway,
    physical_address,
    comment_01
"""


def read_endpoint_rows(site_filter=""):
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    where_clause = ""
    params = []
    if site_filter:
        where_clause = "WHERE site = ?"
        params.append(site_filter)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT {ENDPOINT_COLUMNS}
            FROM endpoints
            {where_clause}
            ORDER BY
                CAST(entry_vlan AS INTEGER),
                active,
                site,
                device_fqdn,
                hostname
        """, params).fetchall()
        last_updated = conn.execute(f"""
            SELECT COALESCE(MAX(NULLIF(last_seen, '')), MAX(NULLIF(date, '')), '') AS last_updated
            FROM endpoints
            {where_clause}
        """, params).fetchone()["last_updated"]

    return [dict(row) for row in rows], last_updated or ""


def read_inventory_rows(site_filter=""):
    if not INVENTORY_DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {INVENTORY_DB_PATH}")

    where_clause = ""
    params = []
    if site_filter:
        where_clause = "WHERE site = ?"
        params.append(site_filter)

    with sqlite3.connect(INVENTORY_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT {INVENTORY_COLUMNS}
            FROM devices
            {where_clause}
            ORDER BY site, fqdn, ip_address
        """, params).fetchall()
        imported_at = conn.execute(f"""
            SELECT COALESCE(MAX(NULLIF(imported_at, '')), '') AS imported_at
            FROM devices
            {where_clause}
        """, params).fetchone()["imported_at"]

    return [dict(row) for row in rows], imported_at or ""


def safe_site_id(site):
    cleaned = secure_filename(str(site or "").strip())
    if not cleaned:
        abort(400, "Missing site")
    return cleaned


def photo_directory(site):
    return PHOTO_ROOT / safe_site_id(site)


def photo_url(site, filename):
    return f"/polaris/data/site_photos/{safe_site_id(site)}/{filename}"


def list_site_photos(site):
    directory = photo_directory(site)
    if not directory.exists():
        return []

    photos = []
    for path in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS:
            photos.append({
                "filename": path.name,
                "url": photo_url(site, path.name),
                "size": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
    return photos


def unique_photo_path(directory, filename):
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def site_photo_filename(site, original_name):
    site_prefix = safe_site_id(site)
    filename = secure_filename(original_name or "")
    if not filename:
        return ""

    if filename.lower().startswith(f"{site_prefix.lower()}_"):
        return filename

    return f"{site_prefix}_{filename}"


def snapshot_values(rows):
    return sorted({
        str(row.get("last_seen") or "").strip()
        for row in rows
        if str(row.get("last_seen") or "").strip()
    })


def endpoint_key(row):
    return "|".join([
        str(row.get("entry_mac") or "").strip().lower(),
        str(row.get("device_fqdn") or "").strip().lower(),
        str(row.get("entry_ip") or "").strip().lower(),
    ])


def add_diff_fields(row, state, label):
    diff_row = dict(row)
    diff_row["diff_state"] = state
    diff_row["diff_label"] = label
    diff_row["endpoint_key"] = endpoint_key(row)
    return diff_row


def build_endpoint_diff(rows):
    snapshots = snapshot_values(rows)
    current_snapshot = snapshots[-1] if snapshots else ""
    previous_snapshot = snapshots[-2] if len(snapshots) > 1 else ""
    newer = []
    disappeared = []
    current = []
    missing_metadata = []

    for row in rows:
        first_seen = str(row.get("first_seen") or "").strip()
        last_seen = str(row.get("last_seen") or "").strip()

        if not first_seen or not last_seen:
            missing_metadata.append(add_diff_fields(row, "missing_metadata", "Missing Metadata"))
        elif last_seen != current_snapshot:
            disappeared.append(add_diff_fields(row, "disappeared", "Disappeared"))
        elif first_seen == current_snapshot:
            newer.append(add_diff_fields(row, "newer", "Newer"))
        else:
            current.append(add_diff_fields(row, "current", "Current"))

    return {
        "current_snapshot": current_snapshot,
        "previous_snapshot": previous_snapshot,
        "summary": {
            "total": len(rows),
            "current": len(current),
            "newer": len(newer),
            "disappeared": len(disappeared),
            "missing_metadata": len(missing_metadata),
        },
        "newer": newer,
        "disappeared": disappeared,
        "missing_metadata": missing_metadata,
    }


def parse_endpoint_time(value):
    text = str(value or "").strip()
    if not text:
        return None

    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass

    try:
        return datetime.strptime(text.split()[0], "%Y-%m-%d")
    except ValueError:
        return None


def week_bucket(value):
    parsed = parse_endpoint_time(value)
    if not parsed:
        return {
            "key": "unknown",
            "label": "unknown week",
            "start": "",
            "end": "",
            "sort": "",
        }

    start = parsed - timedelta(days=parsed.weekday())
    end = start + timedelta(days=6)
    return {
        "key": start.strftime("%Y-%m-%d"),
        "label": f"{start.strftime('%b-%d').lower()} week",
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "sort": start.date().isoformat(),
    }


def normalize_feed_endpoint(row, state, event_time=""):
    return {
        "state": state,
        "event_time": event_time,
        "site": row.get("site") or "-",
        "device_fqdn": row.get("device_fqdn") or "-",
        "hostname": row.get("hostname") or "-",
        "entry_ip": row.get("entry_ip") or "-",
        "entry_mac": row.get("entry_mac") or "-",
        "entry_vlan": row.get("entry_vlan") or "-",
        "device_port": row.get("device_port") or row.get("entry_port") or "-",
        "active": row.get("active") or row.get("status") or "-",
        "first_seen": row.get("first_seen") or "-",
        "last_seen": row.get("last_seen") or "-",
    }


def build_endpoint_feed(rows):
    snapshots = snapshot_values(rows)
    current_snapshot = snapshots[-1] if snapshots else ""
    weeks = {}
    live_by_site = {}

    def ensure_week(bucket):
        if bucket["key"] not in weeks:
            weeks[bucket["key"]] = {
                "key": bucket["key"],
                "label": bucket["label"],
                "start": bucket["start"],
                "end": bucket["end"],
                "sort": bucket["sort"],
                "sites": {},
                "summary": {
                    "appeared": 0,
                    "disappeared": 0,
                    "available": 0,
                },
            }
        return weeks[bucket["key"]]

    def ensure_site(week, site):
        if site not in week["sites"]:
            week["sites"][site] = {
                "site": site,
                "appeared": [],
                "disappeared": [],
                "available": [],
                "summary": {
                    "appeared": 0,
                    "disappeared": 0,
                    "available": 0,
                },
            }
        return week["sites"][site]

    for row in rows:
        site = str(row.get("site") or "-").strip() or "-"
        first_seen = str(row.get("first_seen") or "").strip()
        last_seen = str(row.get("last_seen") or "").strip()

        if last_seen == current_snapshot:
            live_by_site.setdefault(site, []).append(normalize_feed_endpoint(row, "available", last_seen))

        if first_seen:
            week = ensure_week(week_bucket(first_seen))
            site_entry = ensure_site(week, site)
            site_entry["appeared"].append(normalize_feed_endpoint(row, "appeared", first_seen))
            site_entry["summary"]["appeared"] += 1
            week["summary"]["appeared"] += 1

        if last_seen and last_seen != current_snapshot:
            week = ensure_week(week_bucket(last_seen))
            site_entry = ensure_site(week, site)
            site_entry["disappeared"].append(normalize_feed_endpoint(row, "disappeared", last_seen))
            site_entry["summary"]["disappeared"] += 1
            week["summary"]["disappeared"] += 1

    for week in weeks.values():
        for site, site_entry in week["sites"].items():
            site_entry["available"] = sorted(
                live_by_site.get(site, []),
                key=lambda row: (
                    str(row.get("last_seen") or ""),
                    str(row.get("device_fqdn") or ""),
                    str(row.get("entry_mac") or ""),
                ),
                reverse=True,
            )
            site_entry["summary"]["available"] = len(site_entry["available"])
            week["summary"]["available"] += len(site_entry["available"])

        week["sites"] = sorted(
            week["sites"].values(),
            key=lambda site_entry: (
                -site_entry["summary"]["appeared"] - site_entry["summary"]["disappeared"],
                str(site_entry["site"]),
            ),
        )

    return {
        "current_snapshot": current_snapshot,
        "weeks": sorted(weeks.values(), key=lambda week: week["sort"], reverse=True),
    }


def vlan_sort_key(vlan):
    text = str(vlan or "").strip()
    try:
        return (0, int(text), text)
    except ValueError:
        return (1, text.lower(), text)


def build_endpoint_vlans(rows):
    vlans = {}

    for row in rows:
        site = str(row.get("site") or "-").strip() or "-"
        vlan = str(row.get("entry_vlan") or "").strip()
        if not vlan:
            continue

        key = (site, vlan)
        if key not in vlans:
            vlans[key] = {
                "site": site,
                "vlan": vlan,
                "endpoint_count": 0,
                "mac_count": 0,
                "network_device_count": 0,
                "network_devices": set(),
                "macs": set(),
            }

        entry = vlans[key]
        entry["endpoint_count"] += 1
        mac = str(row.get("entry_mac") or "").strip().lower()
        device = str(row.get("device_fqdn") or "").strip()

        if mac:
            entry["macs"].add(mac)
        if device:
            entry["network_devices"].add(device)

    rows_out = []
    for entry in vlans.values():
        rows_out.append({
            "site": entry["site"],
            "vlan": entry["vlan"],
            "endpoint_count": entry["endpoint_count"],
            "mac_count": len(entry["macs"]),
            "network_device_count": len(entry["network_devices"]),
            "network_devices": sorted(entry["network_devices"]),
        })

    return sorted(rows_out, key=lambda row: (str(row["site"]), vlan_sort_key(row["vlan"])))


@app.get("/")
def root():
    return redirect("/polaris-web/index.html")


@app.get("/State")
@app.get("/state")
def state_page():
    return redirect("/endpoint_feed")


@app.get("/vlans")
@app.get("/VLANs")
def vlans_page():
    return send_from_directory(WEB_ROOT, "vlans.html")


@app.get("/endpoint_feed")
@app.get("/endpoint_feeed")
def endpoint_feed_page():
    return send_from_directory(WEB_ROOT, "endpoint-feed.html")


@app.get("/polaris-web/endpoint-diff.html")
def legacy_endpoint_diff_page():
    return redirect("/endpoint_feed")


@app.get("/polaris-web/state.html")
def legacy_state_page():
    return redirect("/endpoint_feed")


@app.get("/polaris-web/locations.html")
def locations_page():
    return send_from_directory(WEB_ROOT, "addresses.html")


@app.get("/polaris-web/addresses.html")
def legacy_addresses_page():
    return redirect("/polaris-web/locations.html")


@app.get("/polaris-web/")
@app.get("/polaris-web/<path:path>")
def polaris_web(path="index.html"):
    return send_from_directory(WEB_ROOT, path)


@app.get("/polaris/data/<path:path>")
def polaris_data(path):
    return send_from_directory(DATA_ROOT, path)


@app.get("/polaris/data/site_photos/<site>/<path:filename>")
def polaris_site_photo(site, filename):
    return send_from_directory(photo_directory(site), filename)


@app.get("/api/enterprise_endpoints")
def enterprise_endpoints():
    try:
        rows, last_updated = read_endpoint_rows(request.args.get("site", "").strip())
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read enterprise_endpoints.db: {error}")

    return jsonify({
        "source": "/polaris/data/enterprise_endpoints.db",
        "last_updated": last_updated,
        "total": len(rows),
        "endpoints": rows,
    })


@app.get("/api/inventory")
@app.get("/api/network_inventory")
def inventory():
    site_filter = request.args.get("site", "").strip()
    try:
        rows, imported_at = read_inventory_rows(site_filter)
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read inventory.db: {error}")

    return jsonify({
        "source": "/polaris/data/inventory.db",
        "site": site_filter,
        "imported_at": imported_at,
        "total": len(rows),
        "devices": rows,
    })


@app.get("/api/endpoint_vlans")
@app.get("/api/endpoints/vlans")
def endpoint_vlans():
    site_filter = request.args.get("site", "").strip()
    try:
        rows, last_updated = read_endpoint_rows(site_filter)
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read enterprise_endpoints.db: {error}")

    vlans = build_endpoint_vlans(rows)
    return jsonify({
        "source": "/polaris/data/enterprise_endpoints.db",
        "site": site_filter,
        "last_updated": last_updated,
        "total": len(vlans),
        "vlans": vlans,
    })


@app.get("/api/site_photos")
def site_photos():
    site = request.args.get("site", "").strip()
    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "photos": list_site_photos(site),
    })


@app.post("/api/site_photos")
def upload_site_photos():
    site = request.form.get("site", "").strip()
    site_dir = photo_directory(site)
    files = request.files.getlist("photos")
    if not files:
        abort(400, "No photos uploaded")

    site_dir.mkdir(parents=True, exist_ok=True)
    uploaded = []
    skipped = []
    for file_storage in files:
        original_name = site_photo_filename(site, file_storage.filename)
        suffix = Path(original_name).suffix.lower()
        if not original_name or suffix not in ALLOWED_PHOTO_EXTENSIONS:
            skipped.append(file_storage.filename or "unknown")
            continue

        target = unique_photo_path(site_dir, original_name)
        file_storage.save(target)
        uploaded.append({
            "filename": target.name,
            "url": photo_url(site, target.name),
            "size": target.stat().st_size,
        })

    if not uploaded:
        abort(400, "No supported image files uploaded")

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "uploaded": uploaded,
        "skipped": skipped,
        "photos": list_site_photos(site),
    })


@app.get("/api/enterprise_endpoint_diff")
@app.get("/api/endpoint_diff")
@app.get("/api/endpoints/diff")
def enterprise_endpoint_diff():
    site_filter = request.args.get("site", "").strip()
    try:
        rows, last_updated = read_endpoint_rows(site_filter)
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read enterprise_endpoints.db: {error}")

    diff = build_endpoint_diff(rows)
    return jsonify({
        "source": "/polaris/data/enterprise_endpoints.db",
        "site": site_filter,
        "last_updated": last_updated,
        **diff,
    })


@app.get("/api/endpoint_feed")
@app.get("/api/endpoint_feeed")
@app.get("/api/endpoints/feed")
def endpoint_feed_api():
    try:
        rows, last_updated = read_endpoint_rows()
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read enterprise_endpoints.db: {error}")

    feed = build_endpoint_feed(rows)
    return jsonify({
        "source": "/polaris/data/enterprise_endpoints.db",
        "last_updated": last_updated,
        **feed,
    })


def main():
    parser = ArgumentParser(description="Serve Polaris static files and Flask API endpoints.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    print(f"Serving Polaris at http://{args.host}:{args.port}/polaris-web/index.html")
    print(f"Inventory API: http://{args.host}:{args.port}/api/inventory")
    print(f"Endpoint API: http://{args.host}:{args.port}/api/enterprise_endpoints")
    print(f"Endpoint VLANs: http://{args.host}:{args.port}/vlans")
    print(f"Endpoint Feed API: http://{args.host}:{args.port}/api/endpoint_feed")
    print(f"Endpoint Feed: http://{args.host}:{args.port}/endpoint_feed")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
