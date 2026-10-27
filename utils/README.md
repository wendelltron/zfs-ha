# utils/ - shelf diagnostics

Two read-only tools for looking at the disk shelf itself. Neither one writes
anything, resets anything, or changes any state. They are the first thing I
run when a disk "disappears" or a bay looks wrong, and they are what I use to
answer "is this a disk problem or a shelf problem".

Both need `sg3_utils` and `lsscsi` installed, plus `smp_utils` for the
topology tool. Root (or membership in a group that can read the sg devices)
is required to read SES device nodes.

```sh
dnf install -y sg3_utils lsscsi
# smp_utils is not packaged for EL10/Rocky 10 - build it:
dnf install -y gcc make
curl -skL https://sg.danny.cz/sg/p/smp_utils-0.99.tgz | tar xz -C /tmp
cd /tmp/smp_utils-0.99 && ./configure --prefix=/usr/local && make -j4 && make install
# Debian/Ubuntu package all three:
# apt install -y sg3-utils lsscsi smp-utils
```

---

## shelf-health.py

Reads the enclosure services (SES) device on the shelf and reports the health
of everything that is not a disk: power supplies, fans, temperature sensors,
voltage/current sensors, the slot population, the IOM modules, and the SAS
connector cabling map. It is SES-standard, so it works with any shelf that
exposes an enclosure services device - the reference shelf is a NetApp DS460C
(IOM12), but nothing in the tool is NetApp-specific.

### Usage

`make install` puts the tools on your PATH as `shelf-health` and
`sas_topology` (in `/usr/local/bin`); the examples below use `python3
shelf-health.py` so they work straight from a checkout.

```sh
# formatted console output (default)
sudo python3 shelf-health.py

# machine-readable, full detail (for monitoring tools)
sudo python3 shelf-health.py --json

# single-line summary (for alerting checks / cron)
sudo python3 shelf-health.py --compact
sudo python3 shelf-health.py --json --compact

# explicit SES device (skip auto-detection)
sudo python3 shelf-health.py --ses /dev/sg0

# live view, refresh every 30s
sudo python3 shelf-health.py --watch 30
```

Exit codes: 0 = healthy, 1 = degraded (warnings present), 2 = critical (or
no SES device found). The exit code is what you hang alerting off of.

### Sample output (real, from the reference shelf)

Console, abridged:

```
==============================================================
 SAS Shelf Health: NETAPP DS46012IOM12A
==============================================================
  SES device:    /dev/sg0
  Enclosure ID:  500a09800b1db854
  Serial:        SHJGD1904000
  FW revision:   0311
  Overall:       CRITICAL

 Power Supply:
  [OK] #0: OK
  [!!] #1: Critical

 Cooling:
  [OK] #0: OK
  [OK] #1: OK
  ... (8 fans, all OK)

 Temp Sensor:
  [OK] #0: OK
  ... (29 sensors, all OK)

 Device Slot:
  [OK] #0: OK  [slot 0]
  [!!] #45: Critical  [bypassA, bypassB, slot 9]
  ... (60 slots; bypass flags are expected in single-IOM cabling)

 Sas Connector:
  [OK] #0: OK
  [OK] #2: OK
  [OK] #4: OK

 IOM Modules:
  FI=00;FM=10;SA=500A0980082F1569;FPI=IOM12A  ;
  FI=00;FM=10;SA=500A0980082F14E1;FPI=IOM12A  ;

 SAS Connectors (cabling):
  SN=XYooLinK01222779;VN=XIANGYU;PN=HD8644-8644-1M;AA=500605B00AC9B870;AP=04;
  SN=XYooLinK01222801;VN=XIANGYU;PN=HD8644-8644-1M;AA=500605B00B28ED40;AP=00;

 CRITICAL:
  !! power_supply[1]: CRITICAL
  !! voltage_sensor[2]: UNRECOVERABLE
  !! voltage_sensor[3]: UNRECOVERABLE

 Overall status: CRITICAL
```

Compact (what a cron job or monitoring check would consume):

```
shelf=critical psu=1/2 fans=8/8 slots=60 crit=3 warn=0
```

JSON compact:

```json
{"overall": "critical",
 "criticals": ["power_supply[1]: CRITICAL", "voltage_sensor[2]: UNRECOVERABLE", "voltage_sensor[3]: UNRECOVERABLE"],
 "warnings": [],
 "timestamp": "2026-10-23T16:40:12Z"}
```

That output is from the actual reference shelf, and it is a good example of
why the tool exists: the second PSU has been reporting Critical since first
discovery, and the two voltage sensors reporting Unrecoverable are believed
to be related to running on one PSU. That is a physical problem to fix, not
a software one, and no pool-level monitoring would have surfaced it.

### Reading the output

- `Critical` / `Unrecoverable` element status is critical; `Noncritical` /
  `Degraded` is a warning; the `Predicted failure` flag is a warning.
- Device slots showing `Bypassed B=1` are **expected** when only one IOM is
  cabled (the B path is not connected) and are treated as informational, not
  failures. If you cable both IOMs, edit `EXPECTED_BYPASS_TYPES` in the
  script so bypassed paths get reported again.
- The `SAS Connectors (cabling)` section is the cabling map: `AA=` is the
  attached (host-side) SAS address, so you can tell exactly which host is
  plugged into which connector. On the reference shelf you can see both
  hosts' HBAs (`500605B00AC9B870` and `500605B00B28ED40`) landing on the
  same IOM.

### Monitoring integration

- Nagios/Icinga: `--json --compact` with exit-code checks.
- Prometheus: scrape `--json`, export `overall` as a gauge (0/1/2) plus the
  criticals/warnings lists.
- cron + mail: `--compact` hourly, alert on exit code != 0.

### Adapting to another shelf

Only two things are potentially vendor-specific:

1. The vendor element descriptor pages (0x83, 0x8f on NetApp) that carry
   IOM/FRU text. Run `sg_ses -p ed /dev/sgN` once on your shelf and adjust
   the grep patterns in `parse_element_descriptors()`. The core health
   assessment (the status page) needs no changes.
2. The expected-bypass exception described above, which depends on your
   cabling.
3. On LSI DE6600 rebadges (Dell MD3060e, NetApp DE6600, Veritas NetBackup
   5330 shelf), each EMM exposes its own SES node and the secondary EMM
   reports `Bypassed A` for slots it does not route. Pass `--ses` to pick the
   EMM that sees all slots; on a multi-shelf host, run once per enclosure.
   LSI DE6600 support added by @malventano.

---

## sas_topology.sh

Maps the shelf from the host side: SAS hosts, expanders, and every disk with
its bay, WWN, expander bsg node, phy id, and current STP affiliation. This is
the tool that answers "which phy is bay 6 on, and which host owns it right
now" - the question behind most handoff debugging.

Requires `smp_utils` (specifically `smp_rep_phy_sata`) in addition to
sg3_utils. Run it as root.

```sh
sudo sh sas_topology.sh
```

### Sample output (real, from the reference shelf)

```
=== SAS hosts ===
  host0

=== Expanders ===
  expander-0:0     addr= NETAPP  DS46012IOM12A
  expander-0:1     addr= NETAPP  Traf DCM
  expander-0:2     addr= NETAPP  Traf DCM
  expander-0:3     addr= NETAPP  Traf DCM
  expander-0:4     addr= NETAPP  Traf DCM
  expander-0:5     addr= NETAPP  Traf DCM

=== Disks (bay / WWN / phy / affiliation) ===
  bay=0   end_device-0:1:0   wwn=0x50014ee2b0xxxxxx phy=0   aff(valid/initiator)=1/0x500605b00b28ed40
  bay=1   end_device-0:1:1   wwn=0x50014ee2b0xxxxxx phy=1   aff(valid/initiator)=1/0x500605b00b28ed40
  ...
```

The affiliation column is `valid/initiator`: `1/0x500605b00b28ed40` means
the disk is currently affiliated to the host whose HBA has that SAS address
(a `0x500605b0...` address is an LSI/Broadcom HBA OUI). `0/0x0` means the
disk is idle and either host can take it.

### What I use it for

- Before a handoff: confirm which disks are still affiliated to the old
  owner (they should all show the old owner's HBA address, then all flip to
  `0/0x0` after release).
- After a crash: a disk still showing the dead host's address has a stale
  affiliation that needs a phy hard reset (see the main HOWTO, crash
  recovery section).
- Mapping bays to phys: the expander phy id matches the bay number on the
  reference shelf, but do not assume that on yours - this tool tells you the
  real mapping, which is what the pool.conf disk list and the fence agent
  need.

### Note on the expander list

The first expander is the IOM (edge expander); the `Traf DCM` entries are the
downstream traffic expanders behind it, one per 12-disk backplane group.
Empty bays show up as expanders with disabled phys and no disks underneath.
