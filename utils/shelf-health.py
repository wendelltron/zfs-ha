#!/usr/bin/env python3
"""zfs-ha: SAS shelf health/status checker.

Queries the enclosure services (SES) device of a SAS disk shelf and reports
health: power supplies, cooling, temperature sensors, voltage/current sensors,
device slots, and the expander/IOM topology. Works with any SES-compliant
shelf; the reference implementation is a NetApp DS460C (IOM12).

The tool is read-only. It never issues control operations.

Data sources (all standard, no vendor tools required):
  sg_ses   - enclosure status/control pages (sg3_utils)
  sg_inq   - device identification
  lsscsi   - device enumeration
  sysfs    - expander/vendor/product metadata

Usage:
  shelf-health.py                     # formatted console output
  shelf-health.py --json              # machine-readable (monitoring tools)
  shelf-health.py --json --compact    # single-line summary for alerting
  shelf-health.py --ses /dev/sg0      # explicit SES device
  shelf-health.py --watch 30          # refresh every 30s (console mode)

Exit codes (both modes): 0 = healthy, 1 = degraded (warnings), 2 = critical.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# helpers

def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"


def find_ses_devices():
    """Enumerate enclosure services devices via lsscsi."""
    rc, out, _ = run(["lsscsi", "-g"])
    ses = []
    if rc != 0:
        return ses
    for line in out.splitlines():
        # [2:0:1:0] enclosu sas:0x... - /dev/sg0
        if "enclosu" in line:
            m = re.search(r"(/dev/sg\d+)\s*$", line.strip())
            if m:
                ses.append(m.group(1))
    return ses


def shelf_identity(ses_dev):
    """Vendor/product/revision from the SES inquiry."""
    rc, out, _ = run(["sg_ses", "--page=cf", ses_dev])
    ident = {"vendor": "", "product": "", "revision": "", "serial": "",
             "logical_id": ""}
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"enclosure vendor:\s*(\S+)\s+product:\s*(\S+)\s+rev:\s*(\S+)", line)
            if m:
                ident["vendor"] = m.group(1).strip()
                ident["product"] = m.group(2).strip()
                ident["revision"] = m.group(3).strip()
            m = re.search(r"enclosure logical identifier \(hex\):\s*(\S+)", line)
            if m:
                ident["logical_id"] = m.group(1)
    # serial from the vendor-specific area of the config page
    rc, out, _ = run(["sg_ses", "--page=cf", ses_dev])
    if rc == 0:
        m = re.search(r"([A-Z0-9]{10,})\s*$", out, re.M)
        if m and "SPARE" not in m.group(1):
            ident["serial"] = m.group(1)
    return ident


# ---------------------------------------------------------------------------
# SES status parsing

# sg_ses -p ec output is verbose text. We parse the status descriptor list into
# structured elements. Element types of interest:
#   Device slot, Power supply, Cooling, Temperature sensor, Voltage sensor,
#   Current sensor, SAS connector, Enclosure services controller electronics

TYPE_MAP = {
    # sg_ses renamed this element type across versions ("Device slot" in
    # older sg3_utils, "Array device slot" in 1.48+); accept both.
    "Device slot": "device_slot",
    "Array device slot": "device_slot",
    "Power supply": "power_supply",
    "Cooling": "cooling",
    "Temperature sensor": "temp_sensor",
    "Voltage sensor": "voltage_sensor",
    "Current sensor": "current_sensor",
    "SAS connector": "sas_connector",
    "Enclosure services controller electronics": "controller",
    "Enclosure": "enclosure",
    "Display": "display",
}


def parse_enclosure_status(ses_dev):
    """Parse sg_ses -p ec (Enclosure Status) into a list of elements."""
    rc, out, err = run(["sg_ses", "--page=ec", ses_dev], timeout=60)
    elements = []
    if rc != 0:
        return elements, f"sg_ses failed: {err.strip()[:100]}"

    current_type = None
    current_idx = -1
    elem = None

    for line in out.splitlines():
        line = line.strip()
        # "Element type: Power supply, subenclosure id: 0 [ti=1]"
        m = re.match(r"Element type:\s*([^,]+),\s*subenclosure", line)
        if m:
            tname = m.group(1).strip()
            current_type = TYPE_MAP.get(tname, tname.lower().replace(" ", "_"))
            current_idx = -1
            continue
        # "Element 0 descriptor:" or "Overall descriptor:"
        m = re.match(r"Element (\d+) descriptor:", line)
        if m:
            current_idx = int(m.group(1))
            elem = {"type": current_type, "index": current_idx, "status": "unknown",
                    "flags": {}}
            elements.append(elem)
            continue
        if line.startswith("Overall descriptor:"):
            current_idx = -1
            elem = {"type": current_type, "index": -1, "status": "unknown",
                    "flags": {}, "overall": True}
            elements.append(elem)
            continue
        if elem is None:
            continue
        # status: OK / Critical / Noncritical / Unrecoverable / Not installed / Unsupported
        m = re.search(r"status:\s*(\S+)", line)
        if m and "status" in elem:
            elem["status"] = m.group(1)
        # flags: Predicted failure=0, Disabled=0, ...
        for flag, val in re.findall(r"(\w[\w \t]*?)=(\d)", line):
            flag = flag.strip().lower().replace(" ", "_")
            if flag not in elem["flags"]:
                elem["flags"][flag] = int(val)
        # slot address
        m = re.search(r"Slot address:\s*(\d+)", line)
        if m:
            elem["slot"] = int(m.group(1))
        # bypass flags of interest
        m = re.search(r"(Bypassed [AB])=(\d)", line)
        if m:
            elem["flags"][m.group(1).lower().replace(" ", "_")] = int(m.group(2))
    return elements, None


def parse_additional_element_status(ses_dev):
    """Parse sg_ses -p aes (Additional Element Status) for SAS addresses."""
    rc, out, _ = run(["sg_ses", "--page=aes", ses_dev], timeout=60)
    slots = {}
    if rc != 0:
        return slots
    idx = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"Element index:\s*(\d+)", line)
        if m:
            idx = int(m.group(1))
            continue
        if idx is None:
            continue
        m = re.search(r"SAS address:\s*(0x[0-9a-fA-F]+)", line)
        if m:
            slots.setdefault(idx, {})["sas_address"] = m.group(1)
        m = re.search(r"attached SAS address:\s*(0x[0-9a-fA-F]+)", line)
        if m:
            slots.setdefault(idx, {})["attached_sas_address"] = m.group(1)
        m = re.search(r"device slot number:\s*(\d+)", line)
        if m:
            slots.setdefault(idx, {})["slot_number"] = int(m.group(1))
    return slots


def parse_element_descriptors(ses_dev):
    """Parse sg_ses -p ed for connector/IOM descriptors (NetApp-specific text)."""
    rc, out, _ = run(["sg_ses", "--page=ed", ses_dev], timeout=60)
    info = {"connectors": [], "ioms": [], "frus": []}
    if rc != 0:
        return info
    current = None
    for line in out.splitlines():
        line = line.strip()
        if "SAS connector" in line and "Element type" in line:
            current = "connectors"
            continue
        if "Vendor specific [0x83]" in line and "Element type" in line:
            current = "ioms"
            continue
        if "Vendor specific [0x8f]" in line and "Element type" in line:
            current = "frus"
            continue
        if current is None:
            continue
        m = re.search(r"Element (\d+) descriptor:\s*(.*)", line)
        if m:
            desc = m.group(2).strip()
            if not desc or desc == "<empty>":
                continue
            if current == "connectors":
                info["connectors"].append({"index": int(m.group(1)), "text": desc})
            elif current == "ioms":
                info["ioms"].append({"index": int(m.group(1)), "text": desc})
            elif current == "frus":
                info["frus"].append({"index": int(m.group(1)), "text": desc})
    return info


# ---------------------------------------------------------------------------
# health assessment

# statuses that mean trouble
CRIT_STATUSES = {"critical", "unrecoverable"}
WARN_STATUSES = {"noncritical", "non-critical", "degraded"}
OK_STATUSES = {"ok", "not installed", "unsupported", "unknown"}

# element types whose "Critical" status is expected in a single-IOM cabling
# (bypassed B path) - these are informational, not failures
EXPECTED_BYPASS_TYPES = {"device_slot"}


def assess(elements, slots, desc):
    """Return (overall, warnings, criticals) from parsed elements."""
    criticals = []
    warnings = []
    for e in elements:
        status = e.get("status", "unknown").lower()
        etype = e.get("type", "")
        idx = e.get("index", -1)
        label = f"{etype}[{idx}]"
        if e.get("overall"):
            label = f"{etype} (overall)"
        if e.get("slot") is not None:
            label += f" slot={e['slot']}"

        flags = e.get("flags", {})
        # predicted failure is a warning regardless of status
        if flags.get("predicted_failure"):
            warnings.append(f"{label}: PREDICTED FAILURE")

        if status in CRIT_STATUSES:
            # device slots with bypassed-B are expected on single-IOM cabling
            if etype in EXPECTED_BYPASS_TYPES and flags.get("bypassed_b"):
                # informational: path B bypassed (single IOM cabling)
                continue
            if etype == "device_slot" and flags.get("bypassed_a") and flags.get("bypassed_b"):
                continue  # empty slot, both paths bypassed
            criticals.append(f"{label}: {status.upper()}")
            if flags.get("device_off"):
                criticals[-1] += " (device off)"
        elif status in WARN_STATUSES:
            warnings.append(f"{label}: {status.upper()}")
    return criticals, warnings


def overall_from(criticals, warnings):
    if criticals:
        return "critical"
    if warnings:
        return "degraded"
    return "healthy"


# ---------------------------------------------------------------------------
# output formatting

def fmt_console(data):
    lines = []
    ident = data["identity"]
    lines.append("=" * 62)
    lines.append(f" SAS Shelf Health: {ident['vendor']} {ident['product']}")
    lines.append("=" * 62)
    lines.append(f"  SES device:    {data['ses_device']}")
    lines.append(f"  Enclosure ID:  {ident['logical_id']}")
    if ident["serial"]:
        lines.append(f"  Serial:        {ident['serial']}")
    lines.append(f"  FW revision:   {ident['revision']}")
    lines.append(f"  Overall:       {data['overall'].upper()}")
    lines.append("")

    # group elements by type
    by_type = {}
    for e in data["elements"]:
        by_type.setdefault(e["type"], []).append(e)

    type_order = ["power_supply", "cooling", "temp_sensor", "voltage_sensor",
                  "current_sensor", "controller", "device_slot", "sas_connector",
                  "enclosure", "display"]

    for t in type_order:
        elems = by_type.get(t, [])
        if not elems:
            continue
        title = t.replace("_", " ").title()
        lines.append(f" {title}:")
        for e in elems:
            status = e.get("status", "?")
            marker = "  "
            s = status.lower()
            if s in CRIT_STATUSES:
                marker = "!!"
            elif s in WARN_STATUSES:
                marker = " ?"
            elif s == "ok":
                marker = "OK"
            label = f"  [{marker}] "
            if e.get("overall"):
                label += "overall: "
            else:
                label += f"#{e['index']}: "
            label += status
            flags = e.get("flags", {})
            extras = []
            if flags.get("predicted_failure"):
                extras.append("PREDICTED FAILURE")
            if flags.get("bypassed_a"):
                extras.append("bypassA")
            if flags.get("bypassed_b"):
                extras.append("bypassB")
            if flags.get("device_off"):
                extras.append("device-off")
            if e.get("slot") is not None:
                extras.append(f"slot {e['slot']}")
            if extras:
                label += "  [" + ", ".join(extras) + "]"
            lines.append(label)
        lines.append("")

    # IOM / connector details
    desc = data.get("descriptors", {})
    if desc.get("ioms"):
        lines.append(" IOM Modules:")
        for iom in desc["ioms"]:
            lines.append(f"  {iom['text']}")
        lines.append("")
    if desc.get("connectors"):
        lines.append(" SAS Connectors (cabling):")
        for c in desc["connectors"]:
            lines.append(f"  {c['text']}")
        lines.append("")

    # summary
    if data["criticals"]:
        lines.append(" CRITICAL:")
        for c in data["criticals"]:
            lines.append(f"  !! {c}")
        lines.append("")
    if data["warnings"]:
        lines.append(" WARNINGS:")
        for w in data["warnings"]:
            lines.append(f"  ?  {w}")
        lines.append("")

    lines.append(f" Overall status: {data['overall'].upper()}")
    return "\n".join(lines)


def fmt_compact(data):
    """Single-line summary for monitoring tools."""
    psu_ok = sum(1 for e in data["elements"]
                 if e["type"] == "power_supply" and not e.get("overall")
                 and e.get("status", "").lower() == "ok")
    psu_total = sum(1 for e in data["elements"]
                    if e["type"] == "power_supply" and not e.get("overall"))
    fans_ok = sum(1 for e in data["elements"]
                  if e["type"] == "cooling" and not e.get("overall")
                  and e.get("status", "").lower() == "ok")
    fans_total = sum(1 for e in data["elements"]
                     if e["type"] == "cooling" and not e.get("overall"))
    slots = sum(1 for e in data["elements"]
                if e["type"] == "device_slot" and not e.get("overall")
                and e.get("status", "").lower() not in ("not installed", "unsupported"))
    return (f"shelf={data['overall']} psu={psu_ok}/{psu_total} "
            f"fans={fans_ok}/{fans_total} slots={slots} "
            f"crit={len(data['criticals'])} warn={len(data['warnings'])}")


# ---------------------------------------------------------------------------
# main

def collect(ses_dev):
    data = {
        "ses_device": ses_dev,
        "identity": shelf_identity(ses_dev),
        "elements": [],
        "descriptors": {},
        "criticals": [],
        "warnings": [],
        "overall": "unknown",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    elements, err = parse_enclosure_status(ses_dev)
    if err:
        data["error"] = err
        data["overall"] = "error"
        return data
    data["elements"] = elements
    data["slots"] = parse_additional_element_status(ses_dev)
    data["descriptors"] = parse_element_descriptors(ses_dev)
    data["criticals"], data["warnings"] = assess(elements, data["slots"],
                                                  data["descriptors"])
    data["overall"] = overall_from(data["criticals"], data["warnings"])
    return data


def main():
    ap = argparse.ArgumentParser(
        description="SAS shelf health checker (SES-based, read-only)")
    ap.add_argument("--ses", help="SES device (default: auto-detect)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--compact", action="store_true",
                    help="single-line summary (with --json: compact JSON)")
    ap.add_argument("--watch", type=int, metavar="SEC",
                    help="refresh every SEC seconds (console mode)")
    args = ap.parse_args()

    ses = args.ses or (find_ses_devices() or [None])[0]
    if not ses:
        print("ERROR: no enclosure services device found (lsscsi 'enclosu')",
              file=sys.stderr)
        sys.exit(2)

    def emit(data):
        if args.json:
            if args.compact:
                print(json.dumps({"overall": data["overall"],
                                  "criticals": data["criticals"],
                                  "warnings": data["warnings"],
                                  "timestamp": data["timestamp"]}))
            else:
                print(json.dumps(data, indent=2))
        elif args.compact:
            print(fmt_compact(data))
        else:
            print(fmt_console(data))
        sys.exit({"healthy": 0, "degraded": 1, "critical": 2,
                  "error": 2}.get(data["overall"], 2))

    if args.watch and not args.json:
        while True:
            os.system("clear" if os.name != "nt" else "cls")
            data = collect(ses)
            print(fmt_console(data))
            time.sleep(args.watch)
    else:
        emit(collect(ses))


if __name__ == "__main__":
    main()
