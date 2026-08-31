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
./polaris/data/config.yaml
```

Displayed site branding is read from:

```yaml
site_name:
  main_site: polaris
```

Inventory data is served from:

```text
/api/inventory
/api/<site_id>/devices
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

## Required Data Fields

These are the fields currently used by the web app. Keep them present when regenerating the data sources.

### `inventory.db`

Table: `devices`

Required columns:

```text
site
fqdn
ip_address
serial_number
device_type
device_role
location
access
tag
tags
physical_address
comment_01
imported_at
```

Usage:

```text
site             main site list, filters, per-site pages, inventory counts
fqdn             device name display and search
ip_address       device IP display and search
serial_number    device serial display, search, physical device counts
device_type      device type display, search, summary counts
device_role      device role display, search, L3 gateway and fleet summaries
location         wireless device filtering and display
access           wireless device filtering and display
tag              wireless device filtering for access_point
tags             wireless device filtering for access_point
physical_address site location display and Google Maps links
comment_01       continuation field for split physical_address values
imported_at      inventory metadata timestamp
```

Wireless devices are shown when `access_point` or `access point` appears in one of these inventory fields:

```text
device_role
device_type
location
access
tag
tags
```

### `enterprise_endpoints.db`

Table: `endpoints`

Required columns:

```text
device_fqdn
device_ip
device_port
entry_ip
entry_mac
entry_vlan
entry_port
hostname
site
description
active
status
error
date
first_seen
last_seen
```

Usage:

```text
site        endpoint filtering, status, VLANs, feed
device_fqdn network device display and endpoint grouping
device_ip   endpoint API output
device_port device port display and search
entry_ip    endpoint IP display and search
entry_mac   endpoint MAC display, counts, diff keys
entry_vlan  VLAN display, sorting, VLAN summaries
entry_port  fallback when device_port is empty
hostname    endpoint hostname display and search
active      endpoint status display and sorting
status      fallback endpoint status
date        fallback last-updated metadata
first_seen  feed, new endpoint detection, home tile metadata coloring
last_seen   feed, disappeared endpoint detection, current snapshot
```

### `site_operations.json`

Records should include these fields, or one of the supported aliases:

```text
site: Site, site
device: FQDN, fqdn, Device, device
tech room: Tech_Room, tech_room, Tech Room, techRoom
ip address: IP_Address, ip_address
member: Member, member
serial number: Serial_Number, Member_Serial, serial_number
uptime: Uptime, Member_Uptime, uptime
code version: Code_Version, code_version, Code Version, codeVersion
```

### `enterprise_routing.json`

Route records should include these fields, or one of the supported aliases:

```text
site: site, Site
device: fqdn, FQDN, device, Device
vlan: vlan_id, VLAN_ID, vlanId, VLAN
subnet: subnet, Subnet, network, Network
```

### SSH Status CSV

The home status panel expects:

```text
Device
IP_Address
Site
Device_Type
SSH_Port_22
Status
```
