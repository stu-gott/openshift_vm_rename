#!/usr/bin/env python3
"""
rename_transform.py — turn an original KubeVirt VM manifest into a re-applicable one.

Reads the original manifest from stdin, writes the renamed manifest to stdout.

Changes:
  * metadata.name                      -> NEW_NAME
  * metadata.labels identity keys (app, kubevirt.io/domain, kubevirt.io/vm)
                                       -> NEW_NAME (if present)
  * spec.template.metadata.labels.app  -> NEW_NAME (if present)
  * spec.template.metadata.labels["kubevirt.io/domain"] -> NEW_NAME (if present)
  * spec.template.metadata.labels["kubevirt.io/vm"]     -> NEW_NAME (if present)
  * DataVolume-backed volumes are flattened to plain persistentVolumeClaim
    references (using DV_MAP, a JSON dict dv-name -> pvc-name). This is
    essential: dataVolumeTemplates are owned by the VM, so deleting the old
    VM garbage-collects the DataVolumes and their PVCs, and the renamed VM
    would get fresh BLANK disks. Flattening keeps the existing data PVCs.
  * Plain PVC-backed volumes have their claimName remapped through PVC_MAP
    (a JSON dict source-pvc -> clone-pvc) to point the renamed VM at the
    cloned disks.
  * spec.dataVolumeTemplates is removed (see above)
  * removed: metadata.uid, resourceVersion, creationTimestamp, generation,
    managedFields, finalizers, and the whole status block
  * annotations are KEPT except known stale server bookkeeping
    (kubemacpool timestamp, kubevirt.io/*-observed-api-version)

Keeps: namespace, labels, preference, runStrategy, template spec.

Usage:
    oc get vm <name> -n <ns> -o yaml \
      | NEW_NAME=<new> DV_MAP='{"dv-root":"pvc-root"}' PVC_MAP='{"pvc-old":"pvc-new"}' \
        python3 rename_transform.py > new.yaml
"""
import json
import os
import re
import sys

import yaml

DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?"
                     r"(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")

# Stale server bookkeeping — safe to drop. All other annotations are kept,
# because other operators/integrations may key off them.
STRIP_ANNOTATIONS = (
    "kubemacpool.io/transaction-timestamp",
    "kubevirt.io/latest-observed-api-version",
    "kubevirt.io/storage-observed-api-version",
)


def transform(doc, new_name, dv_map, pvc_map=None):
    pvc_map = pvc_map or {}
    meta = doc.setdefault("metadata", {})

    # Rename
    meta["name"] = new_name

    # Strip server-managed metadata fields
    for key in ("uid", "resourceVersion", "creationTimestamp", "generation",
                "managedFields", "finalizers"):
        meta.pop(key, None)

    # Drop the status block entirely
    doc.pop("status", None)

    # Keep annotations, but drop known stale server bookkeeping
    ann = meta.get("annotations")
    if ann:
        for key in STRIP_ANNOTATIONS:
            ann.pop(key, None)
        if not ann:
            meta.pop("annotations", None)

    # Rename the identity labels on the VM object itself (QE finding #6: the
    # kubevirt.io/vm label can live on metadata.labels as well as on the
    # template, and a stale old name there breaks VMI<->VM selection)
    mlabels = meta.get("labels") or {}
    for key in ("app", "kubevirt.io/domain", "kubevirt.io/vm"):
        if key in mlabels:
            mlabels[key] = new_name

    spec = doc.setdefault("spec", {})
    volumes = ((spec.get("template") or {}).get("spec") or {}).get("volumes") or []
    for vol in volumes:
        dv = (vol.get("dataVolume") or {}).get("name")
        if dv:
            # Flatten: reference the DataVolume's PVC directly, so the disk
            # survives the VM deletion. Re-applying dataVolumeTemplates with
            # the renamed VM would create fresh BLANK disks instead.
            vol.pop("dataVolume", None)
            vol["persistentVolumeClaim"] = {"claimName": dv_map.get(dv, dv)}
        elif vol.get("persistentVolumeClaim", {}).get("claimName"):
            claim = vol["persistentVolumeClaim"]["claimName"]
            vol["persistentVolumeClaim"]["claimName"] = pvc_map.get(claim, claim)

    # Never re-create DataVolumes with the renamed VM (see above)
    spec.pop("dataVolumeTemplates", None)

    # Rename the template labels that encode the VM identity
    tlabels = (((spec.get("template") or {}).get("metadata") or {})
               .get("labels") or {})
    for key in ("app", "kubevirt.io/domain", "kubevirt.io/vm"):
        if key in tlabels:
            tlabels[key] = new_name

    return doc


def main():
    new_name = os.environ.get("NEW_NAME")
    if not new_name:
        sys.stderr.write("ERROR: NEW_NAME env var is required\n")
        return 2
    if not DNS1123.match(new_name):
        sys.stderr.write("ERROR: NEW_NAME %r is not a valid DNS-1123 subdomain "
                         "(lowercase alphanumerics, '-' and '.')\n" % new_name)
        return 3
    try:
        dv_map = json.loads(os.environ.get("DV_MAP") or "{}")
    except ValueError:
        sys.stderr.write("ERROR: DV_MAP must be a JSON dict "
                         "{dv-name: pvc-name}\n")
        return 2
    try:
        pvc_map = json.loads(os.environ.get("PVC_MAP") or "{}")
    except ValueError:
        sys.stderr.write("ERROR: PVC_MAP must be a JSON dict "
                         "{source-pvc: clone-pvc}\n")
        return 2

    doc = yaml.safe_load(sys.stdin.read())
    if not isinstance(doc, dict):
        sys.stderr.write("ERROR: input is not a single YAML mapping "
                         "(is `oc get vm` output empty?)\n")
        return 2
    out = transform(doc, new_name, dv_map, pvc_map)
    sys.stdout.write(yaml.safe_dump(out, sort_keys=False,
                                    default_flow_style=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
