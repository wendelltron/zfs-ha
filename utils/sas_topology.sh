#!/bin/sh
# zfs-ha: discover SAS topology and map disks to expander phys.
#
# Usage: sas_topology.sh
#
# Outputs a table: bay, disk WWN, expander bsg node, phy id, current affiliation.
# Works with any SAS shelf (expander-based); no vendor-specific assumptions.
#
# Requires: smp_utils (smp_discover, smp_rep_phy_sata), sg3_utils (sg_ses, lsscsi)
set -u

SMP_BIN="${SMP_BIN:-/usr/local/bin}"
export PATH="$SMP_BIN:$PATH"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-/usr/local/lib}"

echo "=== SAS hosts ==="
for h in /sys/class/sas_host/*; do
    printf "  %s\n" "$(basename "$h")"
done

echo "=== Expanders ==="
for e in /sys/class/sas_expander/*; do
    name=$(basename "$e")
    addr=$(cat "$e/sas_address" 2>/dev/null)
    bsg="/dev/bsg/$name"
    [ -e "$bsg" ] || continue
    vendor=$(cat "$e/vendor_id" 2>/dev/null | tr -s ' ')
    product=$(cat "$e/product_id" 2>/dev/null | tr -s ' ')
    printf "  %-16s addr=%s %s %s\n" "$name" "$addr" "$vendor" "$product"
done

echo "=== Disks (bay / WWN / phy / affiliation) ==="
# Map a SAS address to its kernel block device. The sas_device class
# "device" link does not resolve to the block device on current kernels
# (it points back at the end_device), so match the SCSI device instead.
sas_addr_to_sd() {
    _want=$(echo "$1" | tr 'A-F' 'a-f')
    for _b in /sys/block/sd*; do
        _have=$(cat "$_b/device/sas_address" 2>/dev/null) || continue
        [ "$(echo "$_have" | tr 'A-F' 'a-f')" = "$_want" ] && {
            echo "${_b##*/}"; return 0; }
    done
    return 1
}
# For each SAS end device that is a disk, find its expander + phy, then query affiliation.
for d in /sys/class/sas_device/end_device-*; do
    name=$(basename "$d")
    addr=$(cat "$d/sas_address" 2>/dev/null)
    bay=$(cat "$d/bay_identifier" 2>/dev/null)
    enc=$(cat "$d/enclosure_identifier" 2>/dev/null)
    # skip enclosure services devices (no bay)
    [ -z "$bay" ] && continue
    # find the scsi disk node
    sd=$(sas_addr_to_sd "$addr")
    [ -z "$sd" ] && continue
    wwn=$(lsblk -dn -o WWN "/dev/$sd" 2>/dev/null)
    # find which expander hosts this device: parent end_device-<h>:<expander>:<phy>
    exp=$(echo "$name" | cut -d: -f2)
    phy=$(echo "$name" | cut -d: -f3)
    bsg="/dev/bsg/expander-${h:-?}:${exp}"
    # host number: derive from the end_device name's first field
    hostnum=$(echo "$name" | cut -d: -f1 | sed 's/end_device-//')
    bsg="/dev/bsg/expander-${hostnum}:${exp}"
    aff="?"
    if [ -e "$bsg" ] && command -v smp_rep_phy_sata >/dev/null 2>&1; then
        aff=$(smp_rep_phy_sata --phy="$phy" "$bsg" 2>/dev/null \
              | grep -E "affiliated STP initiator" | grep -o '0x[0-9a-f]*' | head -1)
        valid=$(smp_rep_phy_sata --phy="$phy" "$bsg" 2>/dev/null \
              | grep "affiliation valid" | awk '{print $3}')
        aff="${valid:-?}/${aff:-?}"
    fi
    printf "  bay=%-3s %-16s wwn=%s phy=%-3s aff(valid/initiator)=%s\n" \
           "$bay" "$sd" "${wwn:-?}" "$phy" "$aff"
done
