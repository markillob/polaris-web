#!/usr/bin/env python3
import re
import json
import sqlite3
from uuid import uuid4
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "polaris-web"
DATA_ROOT = ROOT / "polaris" / "data"
DB_PATH = DATA_ROOT / "enterprise_endpoints.db"
INVENTORY_DB_PATH = DATA_ROOT / "inventory.db"
ROUTING_DB_PATH = DATA_ROOT / "enterprise_routing.db"
IFSTATE_COUNTER_DB_PATH = DATA_ROOT / "IFSTATE_status.db"
IFSTATE_STATUS_DB_PATH = DATA_ROOT / "IFSTATE_status.db"
CONFIG_PATH = DATA_ROOT / "config.yaml"
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


@app.after_request
def no_store_app_files(response):
    if request.path.startswith(("/polaris-web", "/api")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
    tag,
    tags,
    device_model,
    os_version,
    device_uptime,
    port_available,
    port_in_use,
    env_type,
    isp_provider,
    isp_type,
    isp_speed,
    isp_circuit_id,
    ha_status,
    ha,
    ha_enable,
    l3_gateway,
    physical_address,
    comment_01
"""

ROUTING_COLUMNS = """
    site_id,
    fqdn,
    vlan_id,
    subnet,
    routable
"""


def column_names(column_list):
    return [
        column.strip()
        for column in column_list.strip().split(",")
        if column.strip()
    ]


def parse_yaml_scalar(value):
    value = value.strip()
    if value in {"", "null", "Null", "NULL", "~"}:
        return ""
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def parse_simple_yaml(text):
    root = {}
    stack = [(-1, root)]

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1] if stack else root
        if value.strip() == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_yaml_scalar(value)

    return root


def read_config():
    default_config = {"site_name": {"main_site": "polaris"}}
    if not CONFIG_PATH.exists():
        return default_config

    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        import yaml
        loaded = yaml.safe_load(text) or {}
    except ImportError:
        loaded = parse_simple_yaml(text)

    if not isinstance(loaded, dict):
        loaded = {}

    site_name = loaded.setdefault("site_name", {})
    if not isinstance(site_name, dict):
        site_name = {}
        loaded["site_name"] = site_name
    site_name.setdefault("main_site", "polaris")
    return loaded


def main_site_name():
    config = read_config()
    return str(config.get("site_name", {}).get("main_site") or "polaris").strip() or "polaris"


def read_yaml_document(path):
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        loaded = yaml.safe_load(text) or {}
    except ImportError:
        loaded = parse_simple_yaml(text)

    return loaded if isinstance(loaded, dict) else {}


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
        endpoint_columns = {row["name"] for row in conn.execute("PRAGMA table_info(endpoints)")}
        arp_source_column = 'arp_source' if 'arp_source' in endpoint_columns else "'' AS arp_source"
        rows = conn.execute(f"""
            SELECT {ENDPOINT_COLUMNS}, {arp_source_column}
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

    with sqlite3.connect(INVENTORY_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        available_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(devices)").fetchall()
        }
        expected_columns = column_names(INVENTORY_COLUMNS)
        selected_columns = [
            column
            for column in expected_columns
            if column in available_columns
        ]

        if not selected_columns:
            return [], ""

        where_clause = ""
        params = []
        if site_filter:
            if "site" not in available_columns:
                return [], ""
            where_clause = "WHERE site = ?"
            params.append(site_filter)

        order_columns = [
            column
            for column in ("site", "fqdn", "ip_address")
            if column in available_columns
        ]
        order_clause = f"ORDER BY {', '.join(order_columns)}" if order_columns else ""

        rows = conn.execute(f"""
            SELECT {", ".join(selected_columns)}
            FROM devices
            {where_clause}
            {order_clause}
        """, params).fetchall()

        if "imported_at" in available_columns:
            imported_at = conn.execute(f"""
                SELECT COALESCE(MAX(NULLIF(imported_at, '')), '') AS imported_at
                FROM devices
                {where_clause}
            """, params).fetchone()["imported_at"]
        else:
            imported_at = ""

    normalized_rows = []
    for row in rows:
        normalized_row = dict(row)
        for column in expected_columns:
            normalized_row.setdefault(column, "")
        normalized_row["backup_config"] = backup_config_info(
            normalized_row.get("site", ""),
            normalized_row.get("fqdn", ""),
        )
        normalized_row["backup_config_exists"] = normalized_row["backup_config"]["exists"]
        normalized_row["backup_config_date"] = normalized_row["backup_config"]["date"]
        normalized_row["backup_config_file"] = normalized_row["backup_config"]["file"]
        normalized_rows.append(normalized_row)

    return normalized_rows, imported_at or ""


def read_routing_rows(site_filter=""):
    if not ROUTING_DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {ROUTING_DB_PATH}")

    where_clause = ""
    params = []
    if site_filter:
        where_clause = "WHERE site_id = ?"
        params.append(site_filter)

    with sqlite3.connect(ROUTING_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        available_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(routes)").fetchall()
        }
        expected_columns = column_names(ROUTING_COLUMNS)
        missing_columns = [column for column in expected_columns if column not in available_columns]
        if missing_columns:
            raise sqlite3.Error(f"missing required columns: {', '.join(missing_columns)}")

        rows = conn.execute(f"""
            SELECT {ROUTING_COLUMNS}
            FROM routes
            {where_clause}
            ORDER BY site_id, fqdn, CAST(vlan_id AS INTEGER), subnet
        """, params).fetchall()

        if "updated_at" in available_columns:
            last_updated = conn.execute(f"""
                SELECT COALESCE(MAX(NULLIF(updated_at, '')), '') AS last_updated
                FROM routes
                {where_clause}
            """, params).fetchone()["last_updated"]
        else:
            last_updated = ""

    normalized_rows = []
    for row in rows:
        normalized_rows.append({
            "site": row["site_id"] or "",
            "fqdn": row["fqdn"] or "",
            "vlan_id": row["vlan_id"] or "",
            "subnet": row["subnet"] or "",
            "routable": row["routable"] or "",
        })

    return normalized_rows, last_updated or ""


def parse_ifstate_filename(filename):
    path = Path(filename)
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return None

    stem = path.stem
    if not stem.startswith("IFSTATE_"):
        return None

    body = stem[len("IFSTATE_"):]
    match = re.match(r"^(.+?)_(\d{8}(?:[_-]?\d{6})?)$", body)
    if match:
        device_id = match.group(1)
        stamp = match.group(2)
    else:
        device_id = body
        stamp = ""

    if not device_id:
        return None

    parsed_datetime = None
    normalized_stamp = ""
    if stamp:
        parsed_datetime, normalized_stamp = parse_backup_datetime(stamp)

    return {
        "device": device_id,
        "stamp": normalized_stamp or stamp,
        "datetime": parsed_datetime,
    }


def interface_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}:{interface_value(nested)}" for key, nested in value.items())
    return str(value)


def counter_value(counters, key):
    value = counters.get(key, {}) if isinstance(counters, dict) else {}
    if isinstance(value, dict):
        rx = value.get("rx", "")
        tx = value.get("tx", "")
        if rx != "" or tx != "":
            return f"rx:{rx} tx:{tx}"
    return interface_value(value)


def normalize_ifstate_interfaces(interfaces):
    if not isinstance(interfaces, dict):
        return []

    rows = []
    for name, details in interfaces.items():
        details = details if isinstance(details, dict) else {}
        counters = details.get("counters", {})
        rows.append({
            "name": str(name),
            "status": interface_value(details.get("status")),
            "admin_status": interface_value(details.get("admin_status")),
            "description": interface_value(details.get("description")),
            "mode": interface_value(details.get("mode")),
            "vlans": interface_value(details.get("vlans")),
            "native_vlan": interface_value(details.get("native_vlan")),
            "speed": interface_value(details.get("speed")),
            "errors": counter_value(counters, "errors"),
            "dropped": counter_value(counters, "dropped"),
            "crc_fcs": counter_value(counters, "crc_fcs"),
        })

    return sorted(rows, key=lambda row: row["name"])


def numeric_delta_value(value):
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return 0


def read_ifstate_rows(db_path, table_name, site_id):
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        available_columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        required_columns = {"site", "device", "interface"}
        missing_columns = sorted(required_columns - set(available_columns))
        if missing_columns:
            raise sqlite3.Error(f"{table_name} missing required columns: {', '.join(missing_columns)}")

        quoted_columns = ", ".join(f'"{column}"' for column in available_columns)
        order_columns = [
            column
            for column in ("device", "interface", "timestamp", "id")
            if column in available_columns
        ]
        order_clause = ", ".join(f'"{column}"' for column in order_columns) or '"device", "interface"'
        rows = conn.execute(f"""
            SELECT {quoted_columns}
            FROM {table_name}
            WHERE site = ?
            ORDER BY {order_clause}
        """, [site_id]).fetchall()

        if "timestamp" in available_columns:
            last_updated = conn.execute(f"""
                SELECT COALESCE(MAX(NULLIF(timestamp, '')), '') AS last_updated
                FROM {table_name}
                WHERE site = ?
            """, [site_id]).fetchone()["last_updated"]
        else:
            last_updated = ""

    return available_columns, rows, last_updated or ""


def row_timestamp_key(row, columns):
    return (
        "" if "timestamp" not in columns or row["timestamp"] is None else str(row["timestamp"]),
        row["id"] if "id" in columns and row["id"] is not None else 0,
    )


def latest_ifstate_rows(rows, columns):
    rows_by_interface = {}
    for row in rows:
        key = (
            "" if row["site"] is None else str(row["site"]),
            "" if row["device"] is None else str(row["device"]),
            "" if row["interface"] is None else str(row["interface"]),
        )
        rows_by_interface.setdefault(key, []).append(row)

    return [
        sorted(grouped_rows, key=lambda row: row_timestamp_key(row, columns), reverse=True)[0]
        for grouped_rows in rows_by_interface.values()
    ]


def interface_status_summary(rows):
    summary = {"up": 0, "down": 0, "other": 0}
    for row in rows:
        status = str(row.get("status", "")).strip().lower()
        if status == "up":
            summary["up"] += 1
        elif status == "down":
            summary["down"] += 1
        else:
            summary["other"] += 1
    return summary


def list_site_interface_status(site):
    site_id = safe_site_id(site)
    columns, rows, last_updated = read_ifstate_rows(IFSTATE_STATUS_DB_PATH, "ifstate_status", site_id)
    latest_rows = latest_ifstate_rows(rows, columns)
    display_columns = [
        column
        for column in columns
        if column not in {"id", "site", "device"}
    ]

    devices_by_name = {}
    for row in latest_rows:
        row_data = {
            column: "" if row[column] is None else str(row[column])
            for column in columns
        }
        device_name = row_data.get("device", "")
        row_data["device_id"] = device_name
        devices_by_name.setdefault(device_name, []).append(row_data)

    devices = []
    for device_name in sorted(devices_by_name, key=lambda value: value.lower()):
        interfaces = sorted(
            devices_by_name[device_name],
            key=lambda row: row.get("interface", ""),
        )
        devices.append({
            "device": device_name,
            "device_id": device_name,
            "file": "IFSTATE_status.db",
            "path": "/polaris/data/IFSTATE_status.db",
            "generated_at": last_updated,
            "interface_count": len(interfaces),
            "status_summary": interface_status_summary(interfaces),
            "interfaces": interfaces,
        })

    return {
        "path": "/polaris/data/IFSTATE_status.db",
        "columns": display_columns,
        "last_updated": last_updated,
        "devices": devices,
        "total_devices": len(devices),
        "total_interfaces": sum(device["interface_count"] for device in devices),
    }


def list_site_interfaces(site, counter_mode="delta"):
    site_id = safe_site_id(site)
    try:
        status_payload = list_site_interface_status(site_id)
    except (FileNotFoundError, sqlite3.Error):
        status_payload = {"path": "/polaris/data/IFSTATE_status.db", "columns": [],
                          "last_updated": "", "devices": [], "total_devices": 0, "total_interfaces": 0}
    if not IFSTATE_COUNTER_DB_PATH.exists():
        return {
            "exists": True,
            "path": status_payload["path"],
            "status_path": status_payload["path"],
            "status_columns": status_payload["columns"],
            "status_last_updated": status_payload["last_updated"],
            "status_devices": status_payload["devices"],
            "status_total_devices": status_payload["total_devices"],
            "status_total_interfaces": status_payload["total_interfaces"],
            "counter_path": "/polaris/data/IFSTATE_status.db",
            "columns": [],
            "last_updated": "",
            "devices": [],
            "total_devices": 0,
            "total_interfaces": 0,
        }

    with sqlite3.connect(IFSTATE_COUNTER_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        available_columns = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(ifstate_counters)").fetchall()
        ]
        required_columns = {"site", "device", "interface"}
        missing_columns = sorted(required_columns - set(available_columns))
        if missing_columns:
            raise sqlite3.Error(f"missing required columns: {', '.join(missing_columns)}")

        selected_columns = available_columns
        quoted_columns = ", ".join(f'"{column}"' for column in selected_columns)
        order_columns = [
            column
            for column in ("device", "interface", "timestamp", "id")
            if column in available_columns
        ]
        order_clause = ", ".join(f'"{column}"' for column in order_columns) or '"device", "interface"'

        rows = conn.execute(f"""
            SELECT {quoted_columns}
            FROM ifstate_counters
            WHERE site = ?
            ORDER BY {order_clause}
        """, [site_id]).fetchall()
        if "timestamp" in available_columns:
            last_updated = conn.execute("""
                SELECT COALESCE(MAX(NULLIF(timestamp, '')), '') AS last_updated
                FROM ifstate_counters
                WHERE site = ?
            """, [site_id]).fetchone()["last_updated"]
        else:
            last_updated = ""

    display_columns = [
        column
        for column in selected_columns
        if column not in {"id", "site", "device"}
    ]
    if "timestamp" in display_columns:
        display_columns.insert(display_columns.index("timestamp") + 1, "previous_timestamp")
    preferred_counter_column_order = ["timestamp", "previous_timestamp", "interface", "status"]
    hidden_counter_columns = {"jumbos_rx", "jumbos_tx", "giants_tx"}
    display_columns = [
        column
        for column in preferred_counter_column_order
        if column in display_columns
    ] + [
        column
        for column in display_columns
        if column not in preferred_counter_column_order and column not in hidden_counter_columns
    ]

    counter_columns = [
        column
        for column in selected_columns
        if column.endswith("_rx") or column.endswith("_tx")
    ]
    rows_by_interface = {}
    for row in rows:
        key = (
            "" if row["site"] is None else str(row["site"]),
            "" if row["device"] is None else str(row["device"]),
            "" if row["interface"] is None else str(row["interface"]),
        )
        rows_by_interface.setdefault(key, []).append(row)

    devices_by_name = {}
    latest_devices_by_name = {}
    for grouped_rows in rows_by_interface.values():
        sorted_rows = sorted(
            grouped_rows,
            key=lambda row: (
                "" if "timestamp" not in selected_columns or row["timestamp"] is None else str(row["timestamp"]),
                row["id"] if "id" in selected_columns and row["id"] is not None else 0,
            ),
            reverse=True,
        )
        row = sorted_rows[0]
        previous_row = next((candidate for candidate in sorted_rows[1:]
                             if "timestamp" not in selected_columns or candidate["timestamp"] != row["timestamp"]), None)
        row_data = {
            column: "" if row[column] is None else str(row[column])
            for column in selected_columns
        }
        row_data["previous_timestamp"] = (
            "" if not previous_row or "timestamp" not in selected_columns or previous_row["timestamp"] is None
            else str(previous_row["timestamp"])
        )
        latest_devices_by_name.setdefault(row_data.get("device", ""), []).append(dict(row_data))
        for column in counter_columns:
            if counter_mode == "latest":
                continue
            if not previous_row:
                row_data[column] = "0"
                continue

            current_value = numeric_delta_value(row[column])
            previous_value = numeric_delta_value(previous_row[column])
            row_data[column] = str(max(current_value - previous_value, 0))

        device_name = row_data.get("device", "")
        devices_by_name.setdefault(device_name, []).append(row_data)

    devices = []
    for device_name in sorted(devices_by_name, key=lambda value: value.lower()):
        interfaces = sorted(
            devices_by_name[device_name],
            key=lambda row: row.get("interface", ""),
        )
        devices.append({
            "device": device_name,
            "ip": "",
            "generated_at": last_updated or "",
            "file": "IFSTATE_status.db",
            "path": "/polaris/data/IFSTATE_status.db",
            "interface_count": len(interfaces),
            "interfaces": interfaces,
        })

    return {
        "exists": True,
        "path": status_payload["path"],
        "status_path": status_payload["path"],
        "status_columns": status_payload["columns"],
        "status_last_updated": status_payload["last_updated"],
        "status_devices": status_payload["devices"],
        "status_total_devices": status_payload["total_devices"],
        "status_total_interfaces": status_payload["total_interfaces"],
        "counter_path": "/polaris/data/IFSTATE_status.db",
        "latest_counter_devices": [
            {"device": name, "device_id": name, "file": "IFSTATE_status.db",
             "generated_at": last_updated, "interfaces": interfaces,
             "status_summary": interface_status_summary(interfaces)}
            for name, interfaces in sorted(latest_devices_by_name.items())
        ],
        "columns": display_columns,
        "last_updated": last_updated or "",
        "devices": devices,
        "total_devices": len(devices),
        "total_interfaces": sum(device["interface_count"] for device in devices),
    }


def parse_backup_datetime(value):
    match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{8})(?:[_-]?(\d{6}))?", value)
    if not match:
        return None, ""

    date_value = match.group(1)
    time_value = match.group(2) or ""
    try:
        if "-" in date_value:
            parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
            normalized_date = date_value
        else:
            parsed_date = datetime.strptime(date_value, "%Y%m%d")
            normalized_date = parsed_date.strftime("%Y-%m-%d")

        if time_value:
            parsed_time = datetime.strptime(time_value, "%H%M%S").time()
            parsed_date = datetime.combine(parsed_date.date(), parsed_time)
            normalized_date = f"{normalized_date} {time_value[0:2]}:{time_value[2:4]}:{time_value[4:6]}"
    except ValueError:
        return None, ""

    return parsed_date, normalized_date


def backup_config_info(site, device_name):
    site_id = secure_filename(str(site or "").strip())
    filename = str(device_name or "").strip()
    if not site_id or not filename or "/" in filename or "\\" in filename:
        return {"exists": False, "date": "", "file": "", "path": ""}

    site_dir = DATA_ROOT / site_id
    if not site_dir.exists() or not site_dir.is_dir():
        return {"exists": False, "date": "", "file": "", "path": ""}

    prefix = f"{filename}-"
    today = datetime.now()
    candidates = []
    for path in site_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".txt":
            continue

        if path.stem == filename:
            backup_datetime = datetime.fromtimestamp(path.stat().st_mtime)
            backup_date = backup_datetime.isoformat(timespec="seconds")
        elif path.name.startswith(prefix):
            backup_datetime, backup_date = parse_backup_datetime(path.stem[len(prefix):])
        else:
            continue
        if not backup_datetime:
            continue

        candidates.append((
            abs((backup_datetime - today).total_seconds()),
            path.name,
            backup_date,
            path,
        ))

    if not candidates:
        return {"exists": False, "date": "", "file": "", "path": ""}

    _, _, backup_date, path = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "exists": True,
        "date": backup_date,
        "file": path.name,
        "path": f"/polaris/data/{site_id}/{path.name}",
    }


def list_site_backup_contents(site):
    site_id = safe_site_id(site)
    directory = DATA_ROOT / site_id
    if not directory.exists():
        return {
            "exists": False,
            "path": f"/polaris/data/{site_id}",
            "items": [],
        }

    if not directory.is_dir():
        abort(400, f"{directory} is not a directory")

    items = []
    for path in directory.iterdir():
        if path.name.startswith(".") or not path.is_file() or path.suffix.lower() != ".txt":
            continue

        stat = path.stat()
        item = {
            "name": path.name,
            "type": "file",
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "url": f"/polaris/data/{site_id}/{quote(path.name)}",
        }
        items.append(item)

    return {
        "exists": True,
        "path": f"/polaris/data/{site_id}",
        "name_filter": "*.txt",
        "items": sorted(items, key=lambda item: (item["type"] != "folder", item["name"].lower())),
    }


def safe_site_id(site):
    cleaned = secure_filename(str(site or "").strip())
    if not cleaned:
        abort(400, "Missing site")
    return cleaned


def photo_directory(site):
    return PHOTO_ROOT / safe_site_id(site)


def photo_url(site, filename):
    quoted_filename = quote(str(filename), safe="/")
    return f"/polaris/data/site_photos/{safe_site_id(site)}/{quoted_filename}"


def safe_photo_filename(filename):
    cleaned = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(character for character in cleaned if ord(character) >= 32)
    if not cleaned or cleaned in {".", ".."}:
        return ""
    return cleaned


def photo_payload(site, path, filename=None, category="site photos"):
    relative_name = filename or path.name
    return {
        "filename": Path(relative_name).name,
        "path": str(relative_name),
        "url": photo_url(site, str(relative_name)),
        "category": category,
        "size": path.stat().st_size,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def list_site_photos(site):
    directory = photo_directory(site)
    if not directory.exists():
        return []

    photos = []
    for path in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS:
            category = "wireless floorplan" if path.stem == floorplan_stem(site) else "site photos"
            photos.append(photo_payload(site, path, path.name, category))
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


def floorplan_stem(site):
    return f"{safe_site_id(site)}-floorplan01"


def find_wireless_floorplan(site):
    directory = photo_directory(site)
    if not directory.exists():
        return None

    stem = floorplan_stem(site)
    matches = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.stem == stem
            and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def wireless_floorplan_payload(site):
    floorplan = find_wireless_floorplan(site)
    if not floorplan:
        return None

    return {
        "filename": floorplan.name,
        "url": photo_url(site, floorplan.name),
        "size": floorplan.stat().st_size,
        "modified": datetime.fromtimestamp(floorplan.stat().st_mtime).isoformat(timespec="seconds"),
    }


def access_point_photo_prefix(site):
    return f"{safe_site_id(site)}-access_point_"


def access_point_photo_directory(site):
    return photo_directory(site) / "access_point_photos"


def telco_room_photo_directory(site):
    return photo_directory(site) / "telco_rooms"


def access_point_photo_filename(site, original_name):
    filename = safe_photo_filename(original_name)
    if not filename:
        return ""

    return filename


def telco_room_photo_filename(site, original_name):
    filename = safe_photo_filename(original_name)
    if not filename:
        return ""

    return filename


def access_point_photo_path(site, filename):
    cleaned = safe_photo_filename(filename)
    if not cleaned:
        abort(400, "Missing filename")

    if Path(cleaned).suffix.lower() not in ALLOWED_PHOTO_EXTENSIONS:
        abort(400, "Unsupported image file")

    path = access_point_photo_directory(site) / cleaned
    if not path.exists() or not path.is_file():
        legacy_path = photo_directory(site) / cleaned
        prefix = access_point_photo_prefix(site).lower()
        if (
            legacy_path.exists()
            and legacy_path.is_file()
            and legacy_path.name.lower().startswith(prefix)
        ):
            return legacy_path
        abort(404, "Photo not found")

    return path


def telco_room_photo_path(site, filename):
    cleaned = safe_photo_filename(filename)
    if not cleaned:
        abort(400, "Missing filename")

    if Path(cleaned).suffix.lower() not in ALLOWED_PHOTO_EXTENSIONS:
        abort(400, "Unsupported image file")

    path = telco_room_photo_directory(site) / cleaned
    if not path.exists() or not path.is_file():
        abort(404, "Photo not found")

    return path


def list_access_point_photos(site):
    photos = []
    directory = access_point_photo_directory(site)
    if directory.exists():
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS:
                photos.append(photo_payload(site, path, Path("access_point_photos") / path.name, "access point photos"))

    legacy_directory = photo_directory(site)
    prefix = access_point_photo_prefix(site).lower()
    if legacy_directory.exists():
        for path in legacy_directory.iterdir():
            if (
                path.is_file()
                and path.name.lower().startswith(prefix)
                and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS
            ):
                photos.append(photo_payload(site, path, path.name, "access point photos"))

    return sorted(photos, key=lambda item: item["modified"], reverse=True)


def list_telco_room_photos(site):
    directory = telco_room_photo_directory(site)
    if not directory.exists():
        return []

    photos = []
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS:
            photos.append(photo_payload(site, path, Path("telco_rooms") / path.name, "telco rooms"))

    return sorted(photos, key=lambda item: item["modified"], reverse=True)


@app.get("/api/photo_sites")
def photo_sites():
    sites = []
    try:
        if PHOTO_ROOT.is_dir():
            for directory in PHOTO_ROOT.iterdir():
                if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                    continue
                if secure_filename(directory.name) != directory.name:
                    continue
                photos = list_all_site_photos(directory.name)
                if photos:
                    sites.append({"site": directory.name, "photo_count": len(photos)})
    except OSError:
        app.logger.exception("Unable to list sites with photos")
        abort(500, "Unable to list sites with photos")
    return jsonify({"sites": sorted(sites, key=lambda row: row["site"]), "total": len(sites)})


def list_all_site_photos(site):
    photos = []
    seen = set()
    for photo in [
        *list_site_photos(site),
        *list_access_point_photos(site),
        *list_telco_room_photos(site),
    ]:
        key = photo.get("url") or photo.get("path") or photo.get("filename")
        if key in seen:
            continue
        seen.add(key)
        photos.append(photo)

    return sorted(photos, key=lambda item: item["modified"], reverse=True)


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


@app.get("/favicon.ico")
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def browser_icon():
    # Browsers request these automatically even when no icon is configured.
    return "", 204


@app.get("/api/config")
def app_config():
    config = read_config()
    return jsonify({
        "source": "/polaris/data/config.yaml",
        "site_name": config.get("site_name", {}),
        "main_site": main_site_name(),
        "config": config,
    })


@app.get("/State")
@app.get("/state")
def state_page():
    return redirect("/feed")


@app.get("/vlans")
@app.get("/VLANs")
def vlans_page():
    return send_from_directory(WEB_ROOT, "vlans.html")


@app.get("/endpoint_feed")
@app.get("/endpoint_feeed")
@app.get("/feed")
def endpoint_feed_page():
    return send_from_directory(WEB_ROOT, "endpoint-feed.html")


@app.get("/polaris-web/endpoint-diff.html")
def legacy_endpoint_diff_page():
    return redirect("/feed")


@app.get("/polaris-web/state.html")
def legacy_state_page():
    return redirect("/feed")


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


@app.get("/api/enterprise_routing")
@app.get("/api/routes")
def enterprise_routing():
    site_filter = request.args.get("site", "").strip()
    try:
        rows, last_updated = read_routing_rows(site_filter)
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read enterprise_routing.db: {error}")

    return jsonify({
        "source": "/polaris/data/enterprise_routing.db",
        "site": site_filter,
        "last_updated": last_updated,
        "total": len(rows),
        "routes": rows,
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


@app.get("/api/vlan_mac_counts")
def vlan_mac_counts():
    site = request.args.get("site", "").strip()
    if not IFSTATE_STATUS_DB_PATH.is_file():
        abort(404, "IFSTATE_status.db not found")
    try:
        with sqlite3.connect(IFSTATE_STATUS_DB_PATH.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT id, site, device, vlan_id, vlan_name, entries, queried_at FROM vlan_mac_counts"
            params = []
            if site:
                query += " WHERE site = ?"
                params.append(site)
            query += " ORDER BY site, device, CAST(vlan_id AS INTEGER), vlan_id"
            rows = [dict(row) for row in conn.execute(query, params)]
    except sqlite3.Error as error:
        abort(500, f"Unable to read VLAN MAC counts: {error}")
    return jsonify({"source": "/polaris/data/IFSTATE_status.db", "site": site,
                    "total": len(rows), "counts": rows})


def interface_speed_mbps(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:b(?:it)?(?:/s|ps)?)?\s*", str(value or ""), re.I)
    if not match:
        return None
    return float(match[1]) * {"": 1, "k": 0.001, "m": 1, "g": 1000}[match[2].lower()]


@app.get("/api/interface_outliers")
def interface_outliers():
    if not IFSTATE_COUNTER_DB_PATH.is_file():
        abort(404, "IFSTATE_status.db not found")
    try:
        with sqlite3.connect(IFSTATE_COUNTER_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(ifstate_counters)")]
            if not {"site", "device", "interface", "timestamp", "status", "speed"}.issubset(columns):
                abort(500, "ifstate_counters is missing required interface columns")
            rows = latest_ifstate_rows(conn.execute("SELECT * FROM ifstate_counters").fetchall(), columns)
    except sqlite3.Error as error:
        abort(500, f"Unable to read interface outliers: {error}")
    error_fields = [name for name in columns if name in {
        "errors_rx", "errors_tx", "crc_fcs_rx", "crc_fcs_tx", "runts_rx", "runts_tx",
        "giants_rx", "giants_tx", "collision_rx", "collision_tx"}]
    sites = {}
    for row in rows:
        if str(row["status"] or "").strip().lower() != "up":
            continue
        errors = {name: row[name] for name in error_fields if numeric_delta_value(row[name]) > 0}
        speed = interface_speed_mbps(row["speed"])
        slow = speed is not None and 0 < speed <= 100
        if not errors and not slow:
            continue
        site = str(row["site"] or "").strip()
        if not site:
            continue
        item = sites.setdefault(site, {"site": site, "error_interfaces": 0, "slow_interfaces": 0, "interfaces": []})
        item["error_interfaces"] += bool(errors)
        item["slow_interfaces"] += slow
        item["interfaces"].append({"device": row["device"], "interface": row["interface"],
            "speed": row["speed"], "slow": slow, "errors": errors, "timestamp": row["timestamp"],
            "description": row["description"] if "description" in columns else ""})
    for item in sites.values():
        item["affected_interfaces"] = len(item["interfaces"])
        item["interfaces"].sort(key=lambda row: (str(row["device"]), str(row["interface"])))
    result = sorted(sites.values(), key=lambda row: (-row["error_interfaces"], -row["slow_interfaces"], row["site"]))
    return jsonify({"sites": result, "source": "/polaris/data/IFSTATE_status.db",
                    "latest": max((str(row["timestamp"] or "") for row in rows), default="")})


@app.get("/api/site_kpis")
def site_kpis():
    site = request.args.get("site", "").strip()
    if not site:
        abort(400, "Missing site")
    if not IFSTATE_STATUS_DB_PATH.is_file():
        abort(404, "IFSTATE_status.db not found")
    definitions = {
        "kpi_vlan_utilization": "queried_at, site, vlan_id, subnet, max_hosts, ep_with_ip, ep_no_ip, available, util_pct, has_ep_data, device_types",
        "kpi_100mbps_interfaces": "queried_at, site, device, interface, description, vlan",
        "kpi_paloalto_ha": "queried_at, site, fqdn, ip_address, ha_enable, ha_status, model, os_version, serial, arp_ip_cnt",
        "kpi_rogue_port": "queried_at, site, device_fqdn, device_port, entry_vlan, mac_count, description",
        "kpi_port_capacity": "queried_at, site, device_fqdn, device_type, device_model, port_in_use, port_available, total_ports, used_pct",
    }
    try:
        with sqlite3.connect(IFSTATE_STATUS_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [table for table in definitions if table not in tables]
            timestamps = set()
            for table in definitions:
                if table in tables:
                    timestamps.update(row[0] for row in conn.execute(
                        f"SELECT DISTINCT queried_at FROM {table} WHERE site = ? AND queried_at IS NOT NULL AND queried_at != ''", (site,)))
            snapshots = sorted(timestamps, reverse=True)
            selected = request.args.get("queried_at", "").strip() or (snapshots[0] if snapshots else "")
            if selected and selected not in timestamps:
                abort(404, "KPI snapshot not found for this site")
            results = {}
            for table, columns in definitions.items():
                results[table] = [dict(row) for row in conn.execute(
                    f"SELECT {columns} FROM {table} WHERE site = ? AND queried_at = ?", (site, selected)
                )] if table in tables and selected else []
    except sqlite3.Error as error:
        abort(500, f"Unable to read KPI data: {error}")
    return jsonify({"site": site, "source": "/polaris/data/IFSTATE_status.db",
                    "snapshots": snapshots, "queried_at": selected, "missing_tables": missing,
                    "vlans": results["kpi_vlan_utilization"],
                    "interfaces": results["kpi_100mbps_interfaces"],
                    "paloalto_ha": results["kpi_paloalto_ha"],
                    "rogue_ports": results["kpi_rogue_port"],
                    "port_capacity": results["kpi_port_capacity"]})


@app.route("/api/site_comments", methods=["GET", "POST"])
def site_comments():
    site = request.args.get("site", "").strip()
    site_id = safe_site_id(site)
    if site_id != site:
        abort(400, "Invalid site ID")
    directory = DATA_ROOT / site_id / "comments"
    if not directory.resolve().is_relative_to(DATA_ROOT.resolve()):
        abort(400, "Invalid comments directory")
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True)
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                abort(400, "Comment cannot be empty")
            if len(text) > 10000:
                abort(400, "Comments must be 10,000 characters or fewer")
            comment = {"id": uuid4().hex, "site": site_id, "text": text.strip(),
                       "created_at": datetime.now(timezone.utc).isoformat()}
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (comment["id"] + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(comment, handle, ensure_ascii=True)
                temporary.replace(directory / (comment["id"] + ".json"))
            finally:
                temporary.unlink(missing_ok=True)
            return jsonify(comment), 201
        comments = []
        if directory.is_dir():
            for path in directory.glob("*.json"):
                if path.is_symlink():
                    continue
                with path.open(encoding="utf-8") as handle:
                    comments.append(json.load(handle))
        comments.sort(key=lambda item: item["created_at"], reverse=True)
        return jsonify({"site": site_id, "comments": comments})
    except (OSError, ValueError, KeyError) as error:
        app.logger.exception("Unable to access site comments")
        abort(500, "Unable to read or save site comments")


@app.get("/api/site_backups")
@app.get("/api/backups")
def site_backups():
    site = request.args.get("site", "").strip()
    return jsonify({
        "source": "/polaris/data",
        "site": site,
        **list_site_backup_contents(site),
    })


@app.get("/api/interfaces")
@app.get("/api/site_interfaces")
def site_interfaces():
    site = request.args.get("site", "").strip()
    counter_mode = request.args.get("counter_mode", "delta")
    if counter_mode not in {"latest", "delta"}:
        abort(400, "counter_mode must be latest or delta")
    try:
        payload = list_site_interfaces(site, counter_mode=counter_mode)
    except FileNotFoundError as error:
        abort(404, str(error))
    except sqlite3.Error as error:
        abort(500, f"Unable to read interface state database: {error}")

    return jsonify({
        "source": "/polaris/data/IFSTATE_status.db",
        "site": site,
        "counter_mode": counter_mode,
        **payload,
    })


@app.get("/api/<site_id>/devices")
def site_devices(site_id):
    site_filter = str(site_id or "").strip()
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
        "photos": list_all_site_photos(site),
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
        "photos": list_all_site_photos(site),
    })


@app.get("/api/wireless_floorplan")
@app.get("/api/wireless-floorplan")
def wireless_floorplan():
    site = request.args.get("site", "").strip()
    safe_site_id(site)
    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "floorplan": wireless_floorplan_payload(site),
    })


@app.post("/api/wireless_floorplan")
@app.post("/api/wireless-floorplan")
def upload_wireless_floorplan():
    site = request.form.get("site", "").strip()
    site_dir = photo_directory(site)
    file_storage = request.files.get("floorplan")
    if not file_storage:
        abort(400, "No floorplan uploaded")

    filename = secure_filename(file_storage.filename or "")
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_PHOTO_EXTENSIONS:
        abort(400, "No supported image file uploaded")

    site_dir.mkdir(parents=True, exist_ok=True)
    target_stem = floorplan_stem(site)
    for existing in site_dir.iterdir():
        if (
            existing.is_file()
            and existing.stem == target_stem
            and existing.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS
        ):
            existing.unlink()

    target = site_dir / f"{target_stem}{suffix}"
    file_storage.save(target)

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "floorplan": wireless_floorplan_payload(site),
    })


@app.get("/api/access_point_photos")
@app.get("/api/access-point-photos")
def access_point_photos():
    site = request.args.get("site", "").strip()
    safe_site_id(site)
    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "photos": list_access_point_photos(site),
    })


@app.post("/api/access_point_photos")
@app.post("/api/access-point-photos")
def upload_access_point_photos():
    site = request.form.get("site", "").strip()
    site_dir = access_point_photo_directory(site)
    files = request.files.getlist("photos")
    if not files:
        abort(400, "No access point photos uploaded")

    site_dir.mkdir(parents=True, exist_ok=True)
    uploaded = []
    skipped = []
    for file_storage in files:
        source_name = file_storage.filename or ""
        original_name = access_point_photo_filename(site, source_name)
        suffix = Path(original_name).suffix.lower()
        if not original_name or suffix not in ALLOWED_PHOTO_EXTENSIONS:
            skipped.append(file_storage.filename or "unknown")
            continue

        target = unique_photo_path(site_dir, original_name)
        file_storage.save(target)
        uploaded.append({
            "original_filename": source_name,
            "filename": target.name,
            "path": str(Path("access_point_photos") / target.name),
            "url": photo_url(site, str(Path("access_point_photos") / target.name)),
            "size": target.stat().st_size,
        })

    if not uploaded:
        abort(400, "No supported image files uploaded")

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "uploaded": uploaded,
        "skipped": skipped,
        "photos": list_access_point_photos(site),
    })


@app.patch("/api/access_point_photos/<path:filename>")
@app.patch("/api/access-point-photos/<path:filename>")
def rename_access_point_photo(filename):
    site = request.form.get("site", "").strip()
    if request.is_json:
        site = str((request.get_json(silent=True) or {}).get("site") or site).strip()
    source = access_point_photo_path(site, filename)

    data = request.get_json(silent=True) or {}
    requested_name = str(data.get("filename") or request.form.get("filename") or "").strip()
    if not requested_name:
        abort(400, "Missing new filename")

    requested_path = Path(requested_name)
    if not requested_path.suffix:
        requested_name = f"{requested_name}{source.suffix}"

    target_name = access_point_photo_filename(site, requested_name)
    target_suffix = Path(target_name).suffix.lower()
    if not target_name or target_suffix not in ALLOWED_PHOTO_EXTENSIONS:
        abort(400, "Unsupported image filename")

    target_dir = access_point_photo_directory(site)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / target_name
    if target != source:
        target = unique_photo_path(target_dir, target_name)
        source.rename(target)

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "renamed": {
            "original_filename": filename,
            "filename": target.name,
            "path": str(target.relative_to(photo_directory(site))),
            "url": photo_url(site, str(target.relative_to(photo_directory(site)))),
            "size": target.stat().st_size,
        },
        "photos": list_access_point_photos(site),
    })


@app.delete("/api/access_point_photos/<path:filename>")
@app.delete("/api/access-point-photos/<path:filename>")
def delete_access_point_photo(filename):
    site = request.args.get("site", "").strip()
    if request.is_json:
        site = str((request.get_json(silent=True) or {}).get("site") or site).strip()
    path = access_point_photo_path(site, filename)
    path.unlink()

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "deleted": filename,
        "photos": list_access_point_photos(site),
    })


@app.get("/api/telco_rooms")
@app.get("/api/telco-room-photos")
def telco_rooms():
    site = request.args.get("site", "").strip()
    safe_site_id(site)
    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "photos": list_telco_room_photos(site),
    })


@app.post("/api/telco_rooms")
@app.post("/api/telco-room-photos")
def upload_telco_room_photos():
    site = request.form.get("site", "").strip()
    site_dir = telco_room_photo_directory(site)
    files = request.files.getlist("photos")
    if not files:
        abort(400, "No telco room photos uploaded")

    site_dir.mkdir(parents=True, exist_ok=True)
    uploaded = []
    skipped = []
    for file_storage in files:
        source_name = file_storage.filename or ""
        original_name = telco_room_photo_filename(site, source_name)
        suffix = Path(original_name).suffix.lower()
        if not original_name or suffix not in ALLOWED_PHOTO_EXTENSIONS:
            skipped.append(file_storage.filename or "unknown")
            continue

        target = unique_photo_path(site_dir, original_name)
        file_storage.save(target)
        uploaded.append({
            "original_filename": source_name,
            "filename": target.name,
            "path": str(Path("telco_rooms") / target.name),
            "url": photo_url(site, str(Path("telco_rooms") / target.name)),
            "size": target.stat().st_size,
        })

    if not uploaded:
        abort(400, "No supported image files uploaded")

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "uploaded": uploaded,
        "skipped": skipped,
        "photos": list_telco_room_photos(site),
    })


@app.patch("/api/telco_rooms/<path:filename>")
@app.patch("/api/telco-room-photos/<path:filename>")
def rename_telco_room_photo(filename):
    site = request.form.get("site", "").strip()
    if request.is_json:
        site = str((request.get_json(silent=True) or {}).get("site") or site).strip()
    source = telco_room_photo_path(site, filename)

    data = request.get_json(silent=True) or {}
    requested_name = str(data.get("filename") or request.form.get("filename") or "").strip()
    if not requested_name:
        abort(400, "Missing new filename")

    if not Path(requested_name).suffix:
        requested_name = f"{requested_name}{source.suffix}"

    target_name = telco_room_photo_filename(site, requested_name)
    target_suffix = Path(target_name).suffix.lower()
    if not target_name or target_suffix not in ALLOWED_PHOTO_EXTENSIONS:
        abort(400, "Unsupported image filename")

    target_dir = telco_room_photo_directory(site)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / target_name
    if target != source:
        target = unique_photo_path(target_dir, target_name)
        source.rename(target)

    relative_path = target.relative_to(photo_directory(site))
    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "renamed": {
            "original_filename": filename,
            "filename": target.name,
            "path": str(relative_path),
            "url": photo_url(site, str(relative_path)),
            "size": target.stat().st_size,
        },
        "photos": list_telco_room_photos(site),
    })


@app.delete("/api/telco_rooms/<path:filename>")
@app.delete("/api/telco-room-photos/<path:filename>")
def delete_telco_room_photo(filename):
    site = request.args.get("site", "").strip()
    if request.is_json:
        site = str((request.get_json(silent=True) or {}).get("site") or site).strip()
    path = telco_room_photo_path(site, filename)
    path.unlink()

    return jsonify({
        "source": "/polaris/data/site_photos",
        "site": site,
        "deleted": filename,
        "photos": list_telco_room_photos(site),
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
@app.get("/api/feed")
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
    parser = ArgumentParser(description="Serve configured static files and Flask API endpoints.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    print(f"Serving {main_site_name()} at http://{args.host}:{args.port}/polaris-web/index.html")
    print(f"Inventory API: http://{args.host}:{args.port}/api/inventory")
    print(f"Endpoint API: http://{args.host}:{args.port}/api/enterprise_endpoints")
    print(f"Endpoint VLANs: http://{args.host}:{args.port}/vlans")
    print(f"Feed API: http://{args.host}:{args.port}/api/feed")
    print(f"Feed: http://{args.host}:{args.port}/feed")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
