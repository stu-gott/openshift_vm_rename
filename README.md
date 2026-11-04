# Renaming a KubeVirt VirtualMachine on OpenShift (keeping all disks and state)

This repo provides an Ansible playbook that **renames** a KubeVirt `VirtualMachine` on an
OpenShift cluster — e.g. from `vader.syangsao.net` to `vader` — without losing any of its
disks or state.

Kubernetes objects do not allow `metadata.name` to be changed in place, so a rename must
create a new VM object and carry the disks across. The playbook does this by
**cloning every disk, verifying the replacement VM actually runs, and only then deleting
the original** ("clone-then-swap"):

1. pre-flight guards (required args, DNS-1123 name, target-name and clone-name
   collisions), manifest backup, disk discovery;
2. stop the source VM;
3. clone every disk with a CDI DataVolume (`spec.source.pvc`) and wait for completion —
   failing hard, and aborting with the original restarted and intact if a clone stalls;
4. transform the manifest (rename, remap volumes to the cloned PVCs) and validate it
   with a server-side dry run;
5. apply, start and **verify** the renamed VM (VMI Running, volume mapping correct,
   hotplug clones attached);
6. only then delete the source VM and release the now-obsolete original disks (plain
   PVCs and user-created DataVolumes). If the renamed VM was deliberately never started,
   this step is refused — the original is kept until you verify the new VM by hand.

## Why clone-then-swap

An earlier revision of this playbook renamed by delete-and-recreate: patch PVs to
`Retain`, detach DataVolumes, delete the old VM, re-create/re-bind the PVCs, apply the
renamed manifest. Live-cluster QE testing found critical defects in that approach, all
of which trace back to a single structural requirement — *the original PVCs must survive
the deletion of their own VM* — which several controllers violate:

- **DataVolume-backed disks**: virt-controller re-owns templated DataVolumes while the
  VM exists, so null-patching `ownerReferences` races reconciliation; the DVs (and their
  PVCs) were garbage-collected with the old VM anyway, and recreated disks came back with
  a flipped `volumeMode` so the VM could not start.
- **Persistent TPM/EFI state**: the `persistent-state-for-<vm>` PVC is created by
  virt-controller and does not appear in the VM spec, so it was destroyed with the old
  VM and silently replaced with a blank one.
- **MTv/forklift interference**: plan cleanup sets `deletionTimestamp` on PVCs when the
  old VM goes away, forcing finalizer fights, PVC re-creation and manual `claimRef`
  re-binds — each one a race that only matters because the PVC must outlive its VM.

Cloning removes the requirement instead of fighting the controllers: every disk is fully
copied **before** anything is destroyed, so MTv cleanup, DV cascades and finalizer timing
at old-VM deletion are irrelevant. It also means the playbook performs **only
namespaced operations** — no PV patching, no `claimRef` re-binds, no finalizer stripping —
so namespace-level `edit` access is sufficient, and no cluster-scoped state can be left
stranded.

> **Note:** this playbook does **not** use `virtctl clone` (the VirtualMachineClone CRD).
> That API refuses source VMs with persistent TPM/EFI state
> ("Clone of source with backendstorage is not supported") because it cannot remap the
> name-derived state PVC. Here each PVC is cloned individually with a CDI DataVolume,
> which has no such restriction, and the state PVC is explicitly cloned and relabeled
> for the new name.

## What gets carried across

| Disk class | How it is carried |
|---|---|
| Plain PVC volumes (`persistentVolumeClaim`) | CDI clone to `<new-name>-<volume>`, volume remapped in the manifest |
| DataVolume volumes (`dataVolume` / `dataVolumeTemplates`) | underlying PVC cloned, volume flattened to a direct PVC reference, `dataVolumeTemplates` removed |
| Persistent TPM/EFI state (`persistent-state-for-<vm>`, 10Mi, not in the VM spec) | discovered by label, cloned to `persistent-state-for-<new-name>` and labeled — virt-controller adopts it instead of creating a blank one |
| Hotplugged volumes (attached to the running VMI, absent from the VM spec) | detected (PVC- **and** DataVolume-typed), cloned, and re-attached with `virtctl addvolume` after the renamed VM is Running. On virtctl ≥ 1.10, `--disk-name` keeps the original volume name; from virtctl 1.9 the volume is always persisted to the new VM's spec (`--persist` is a deprecated no-op); on older virtctl the volume is re-attached runtime-only and named after the clone PVC |

Each clone copies the source PVC's `accessModes`, `volumeMode`, `storageClassName` and
requested size verbatim, so a `Block` disk stays `Block` and the storage class never
changes. Note that CDI provisions `Filesystem` clones ~6% larger than the request
(1Gi → ~1.06Gi) for filesystem overhead; `Block` clones are exact.

---

## Step-by-step manual procedure

The playbook automates exactly the procedure below. Doing it by hand at least once is
the best way to understand what the automation is doing — and what its failure
postures protect you from. The running example renames `vader.syangsao.net` → `vader`
in namespace `virt-linux`.

> **Rule of order:** nothing original is deleted until **step 6**, after the renamed VM
> is verified Running on its cloned disks. Every earlier step is recoverable by simply
> walking away (delete any half-finished clone DataVolumes and start the VM again).

### 1. Identify the VM, its disks, and its hidden state

```bash
NS=virt-linux; VM=vader.syangsao.net; NEW=vader

# PVC-backed volumes
oc get vm $VM -n $NS -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}={.persistentVolumeClaim.claimName}{"\n"}{end}'

# DataVolume-backed volumes, and the PVC behind each DataVolume
oc get vm $VM -n $NS -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}={.dataVolume.name}{"\n"}{end}'
oc get dv <dv-name> -n $NS -o jsonpath='{.status.claimName}{"\n"}'

# Persistent TPM/EFI state PVC — created by virt-controller, NOT in the VM spec
oc get pvc -n $NS -l persistent-state-for=$VM -o name
# fallback if unlabeled (legacy naming): oc get pvc persistent-state-for-$VM -n $NS -o name

# Hotplugged volumes — attached to the running VMI but absent from the lists above.
# A hotplug is PVC-typed OR DataVolume-typed (virtctl addvolume resolves the source
# as a DataVolume first), so read both fields; for a DataVolume-typed entry, resolve
# the underlying PVC with: oc get dv <name> -o jsonpath={.status.claimName}
oc get vmi $VM -n $NS -o jsonpath='{range .spec.volumes[*]}{.name}={.persistentVolumeClaim.claimName}{.dataVolume.name}{"\n"}{end}'

# Capture each source PVC's settings — the clone must match them exactly
oc get pvc <pvc-name> -n $NS -o jsonpath='{.spec.accessModes}{" "}{.spec.volumeMode}{" "}{.spec.storageClassName}{" "}{.spec.resources.requests.storage}{"\n"}'
```

Write the full inventory down: volume name → source PVC → planned clone PVC
(`<new-name>-<volume-name>` for spec volumes, `<new-name>-hotplug-<volume-name>` for
hotplugged ones, `persistent-state-for-<new-name>` for the state PVC).

### 2. Stop the VM

```bash
virtctl stop $VM -n $NS
oc get vmi $VM -n $NS    # repeat until NotFound — Windows guests can take minutes
```

Do **not** proceed while the VMI still exists — the clones must be taken from
quiesced disks.

### 3. Clone every disk

For each PVC in the inventory, create a CDI DataVolume that clones it. The `storage`
block must mirror the captured source settings:

```bash
oc apply -f - <<EOF
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: ${NEW}-data              # the planned clone PVC name
  namespace: $NS
spec:
  source:
    pvc:
      name: vader-rename-vm-10001-tmmt5   # the SOURCE PVC
      namespace: $NS
  storage:
    accessModes: [ReadWriteMany]           # from the source PVC
    volumeMode: Filesystem                 # from the source PVC — Block stays Block
    storageClassName: nfs-csi              # from the source PVC
    resources:
      requests:
        storage: 106Gi                     # same as the source request
EOF

oc get dv ${NEW}-data -n $NS -w    # wait until PHASE=Succeeded, then Ctrl-C
```

Repeat for every disk, including hotplugged ones. On clone-capable storage (ODF/Ceph)
this is near-instant; on NFS-CSI it is a host-assisted copy of the full disk and needs
CDI scratch space — budget time and transient storage accordingly.

If the VM has persistent TPM/EFI state, clone the state PVC too, then relabel the clone
so the renamed VM adopts it instead of getting a blank one:

```bash
# ... clone persistent-state-for-$VM to persistent-state-for-$NEW as above ...
oc label pvc persistent-state-for-${NEW} -n $NS persistent-state-for=${NEW}
# verify
oc get pvc persistent-state-for-${NEW} -n $NS -o jsonpath='{.metadata.labels.persistent-state-for}{"\n"}'
# -> vader
```

### 4. Back up and transform the manifest

```bash
oc get vm $VM -n $NS -o yaml > ${VM}_${NS}.yaml.bkp    # pristine backup, do not touch
```

Either use the helper (recommended — it applies every edit below consistently):

```bash
oc get vm $VM -n $NS -o yaml \
  | NEW_NAME=$NEW \
    PVC_MAP='{"vader-rename-vm-10001-tmmt5":"vader-data"}' \
    DV_MAP='{"dv-root":"vader-rootdisk"}' \
    python3 rename_transform.py > ${VM}_${NS}.yaml
```

…or edit a copy of the backup by hand:

**Change:**
- `metadata.name` → new name
- `metadata.labels` and `spec.template.metadata.labels`: the `app`,
  `kubevirt.io/vm` and `kubevirt.io/domain` labels → new name (both levels)

**Remap the disks to the clones:**
- `persistentVolumeClaim.claimName` → the clone PVC name (via `PVC_MAP` above)
- `dataVolume: {name: …}` volumes → replace with
  `persistentVolumeClaim: {claimName: <the DataVolume's PVC clone>}`
- delete `spec.dataVolumeTemplates` entirely — re-applying it would create fresh
  **blank** disks

**Remove** (server-managed fields that break a re-apply):
- `metadata.uid`, `resourceVersion`, `creationTimestamp`, `generation`,
  `managedFields`, `finalizers`, the whole `status:` block
- stale server annotations (`kubemacpool.io/transaction-timestamp`,
  `kubevirt.io/latest-observed-api-version`, `kubevirt.io/storage-observed-api-version`)

Validate **before** applying:

```bash
oc apply -f ${VM}_${NS}.yaml --dry-run=server -n $NS
```

### 5. Apply, start, and verify the renamed VM

```bash
oc apply -f ${VM}_${NS}.yaml
virtctl start $NEW -n $NS

oc get vmi $NEW -n $NS -o jsonpath='{.status.phase}{"\n"}'    # must be Running
# every volume must map to its CLONE, not the original PVC:
oc get vmi $NEW -n $NS -o jsonpath='{range .spec.volumes[*]}{.name}={.persistentVolumeClaim.claimName}{"\n"}{end}'
```

Re-attach any hotplugged volumes. Every recent virtctl takes exactly ONE positional
argument (the VM) plus a required `--volume-name` (the DataVolume/PVC source); newer
virtctl additionally accepts `--disk-name` to keep the original volume name — and on
that newer virtctl the volume is always persisted to the VM spec (`--persist` is a
deprecated no-op):

```bash
virtctl addvolume $NEW -n $NS --volume-name <clone-pvc>              # older virtctl
virtctl addvolume $NEW -n $NS --volume-name <clone-pvc> --disk-name <volume-name>
```

Then verify the attachment (both volume types, one per line):

```bash
oc get vmi $NEW -n $NS -o jsonpath='{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{.dataVolume.name}{"\n"}{end}'
```

Verify the data is intact — console into the guest and check a known marker, or
compare the disk contents/size against the source.

> **Do not continue past this point until the VM is Running and every volume maps to
> its clone.** If anything is wrong, delete the new VM (`oc delete vm $NEW -n $NS`) and
> the clone DataVolumes, fix the problem, and start over — the original VM and its
> disks are still intact.

### 6. Swap — delete the original

Only now is anything original destroyed — and **only if the renamed VM was started and
verified** in step 5. If you deliberately did not start it (`start_after=false`, or the
source was stopped), the playbook refuses the swap and keeps the original; doing it by
hand is your own risk:

```bash
oc delete vm $VM -n $NS
# Templated DataVolumes and the state PVC are garbage-collected with the VM.
# Plain PVCs are NOT owned by the VM — release them explicitly:
oc delete pvc <original-plain-pvc> -n $NS
# USER-created DataVolumes (referenced via dataVolume volumes, not templates) also
# survive the VM — release each, which cascades its PVC:
oc delete dv <original-user-dv> -n $NS
```

The rename is complete. The clone DataVolumes remain in the namespace and **own** the
live PVCs — leave them alone (deleting one deletes its PVC and the VM's disk).

### Cheat sheet

```bash
NS=virt-linux; VM=vader.syangsao.net; NEW=vader
virtctl stop $VM -n $NS                              # wait for VMI NotFound
# for each PVC (incl. hotplug + state PVC):
#   DataVolume spec.source.pvc=<pvc> storage=<same settings> name=<clone>
#   oc get dv <clone> -w                               # Succeeded
oc label pvc persistent-state-for-$NEW -n $NS persistent-state-for=$NEW   # if TPM/EFI
oc get vm $VM -n $NS -o yaml > ${VM}_${NS}.yaml.bkp
# transform: NEW_NAME/PVC_MAP/DV_MAP through rename_transform.py (see step 4)
oc apply -f ${VM}_${NS}.yaml --dry-run=server -n $NS
oc apply -f ${VM}_${NS}.yaml
virtctl start $NEW -n $NS                            # verify Running + clone mapping
# hotplug: virtctl addvolume $NEW -n $NS --volume-name <clone-pvc> [--disk-name <vol>]
oc delete vm $VM -n $NS
oc delete pvc <original-plain-pvcs> -n $NS
oc delete dv <original-user-dvs> -n $NS              # each DV owns its PVC
```

## Usage

```bash
ansible-playbook rename_vm.yml \
  -e vm_name=vader.syangsao.net \
  -e new_name=vader \
  -e vm_namespace=virt-linux \
  [-e start_after=auto] \
  [-e dry_run=false] \
  [-e keep_old=false]
```

| Extra var | Default | Meaning |
|-----------|---------|---------|
| `vm_name` | — | current VirtualMachine name (required) |
| `new_name` | — | desired new name (required, must differ, DNS-1123) |
| `vm_namespace` | — | namespace the VM lives in (required) |
| `start_after` | `auto` | start the renamed VM iff it was running before (`true`/`false` to force) |
| `dry_run` | `false` | if `true`: discovery + plan only; nothing is stopped, cloned or applied — and nothing is written to disk |
| `keep_old` | `false` | if `true`: keep the original VM (stopped) and its PVCs as a rollback window |
| `swap_unverified` | `false` | if `true`: delete the original even when the renamed VM was never started (boot verification skipped). Default `false` — an unverified swap is refused and the original kept |
| `clone_wait_retries` × `clone_wait_delay` | 120 × 10s | per-disk clone wait budget (20 min) |
| `vmi_wait_retries` × `vmi_wait_delay` | 60 × 10s | renamed-VM startup wait budget |
| `vm_gone_retries` × `vm_gone_delay` | 60 × 5s | old-VM deletion wait budget |

Requirements: `oc` and `virtctl` on `$PATH`, a healthy CDI (part of any CNV
installation), Python 3 + PyYAML on the Ansible controller, and **namespace-level edit
access** — nothing cluster-scoped is touched.

## Failure behavior

Nothing original is destroyed before the replacement is verified, so every failure has a
safe posture:

- **During cloning** (clone stalls or the budget expires): the clone DataVolumes are
  removed and the original VM is restarted, fully intact. Re-running is just a run —
  there is deliberately no recovery mode because nothing needs recovering.
- **At the verification gate** (renamed VM never reaches Running, or a volume maps
  wrong, or a hotplug re-attach fails): the play fails hard and **the original VM is not
  deleted**. The new VM and its clones are left in place for investigation; the failure
  message includes the manual rollback commands (`oc delete vm <new>`, delete the clone
  DataVolumes, restart the original).
- **Reporting is honest**: every wait uses `--ignore-not-found` with output-based retry
  conditions, so a wait can only fail by exhausting its retries — failures can never be
  reported as success.
- **A VM that was never started is never swapped**: when boot verification was skipped
  (`start_after=false` or the source was stopped), the original VM and its disks are
  kept intact and the play reports the manual finishing steps. Opt into the risk
  explicitly with `-e swap_unverified=true`.

## Costs and operational notes

- **Full copy per disk.** On clone-capable storage (e.g. ODF/Ceph CSI) CDI uses
  smart/CSI clones and completes near-instantly; on NFS-CSI it is a host-assisted
  network copy — plan for the VM to be down for the copy window and for roughly **2× the
  disk size** in transient storage. CDI host-assisted clones also need scratch space
  (about the target size for `Filesystem` volumes); a clone stuck in
  `CloneScheduled`/`ScratchMediated` is a CDI configuration issue, not a playbook issue.
  The cloned `Filesystem` PVC is provisioned ~6% larger than the source request
  (CDI filesystem overhead: 1Gi → ~1.06Gi); `Block` volumes are copied exactly.
- **MTV/Forklift-managed VMs:** if the VMs were migrated with MTV (or are otherwise
  tracked by a Forklift plan/mapping), update the plan and mapping after the rename —
  the new DataVolume/PVC names are unknown to MTV, and a later plan cleanup or re-run
  may target stale objects.
- **The clone DataVolumes remain and OWN the live PVCs.** Deleting a clone DataVolume
  deletes its PVC and the VM's disk. This is the same steady state as a
  `dataVolumeTemplates` VM, but do not "clean up" leftover DVs.
- **Hotplug detection needs a running VM.** Hotplugged volumes are runtime-only state;
  when the source VM is stopped they cannot be detected (an unattached hotplug PVC
  simply survives in the namespace untouched). Note that on recent virtctl,
  `addvolume` always persists the volume into the VM spec (`--persist` is a deprecated
  no-op), so volumes hotplugged there are ordinary spec disks, detected by the normal
  discovery — the runtime-only path matters for older clusters or volumes added through
  the API without persistence.
- **Windows guests shut down slowly.** `virtctl stop` sends a graceful ACPI shutdown;
  Windows can take minutes. If a Windows VM hangs on stop and its launcher pod sticks
  in `Terminating`, force-delete it to unblock:
  `oc delete pod virt-launcher-<vm> -n <ns> --grace-period=0 --force`.

## Files in this repo

| File | Purpose |
|------|---------|
| `README.md` | This document. |
| `rename_vm.yml` | The clone-then-swap rename playbook. |
| `rename_transform.py` | Python helper used by the playbook to rename the VM manifest, flatten DataVolume references to cloned PVCs and strip server-managed fields. Standalone-usable: `oc get vm $VM -n $NS -o yaml \| NEW_NAME=$NEW_NAME DV_MAP='{...}' PVC_MAP='{...}' python3 rename_transform.py > new.yaml` |
| `vader.syangsao.net_virt-linux.yaml` / `.bkp` | Example manifest from the original live test of the manual procedure (original repo content). |
| `windows11.syangsao.net_virt-windows.yaml` / `.bkp` | Example manifest from the original live test of the manual procedure (original repo content). |

## History

The original manual delete-and-recreate procedure, its live-test results (including the
stuck-Terminating-PVC recovery), and the first automated playbook are preserved in git
history on the `main` branch The clone-then-swap workflow in this revision supersedes it.
