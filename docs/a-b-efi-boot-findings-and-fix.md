# A/B EFI boot and recovery

## Summary

Reefy firmware updates use two EFI system partitions, `reefy-a` and
`reefy-b`. An update writes the inactive slot, schedules it once with standard
UEFI `BootNext`, and commits it only after the next boot reaches storage and
control-plane health.

Some AMI-based systems keep a private BBS or default boot priority in addition
to standard `BootOrder`. On those systems, changing only `BootOrder` appears
successful in Linux but firmware can restore its old preference on the next
plain reboot.

Reefy's implemented fix stays within standard UEFI operations: confirmation
creates fresh `reefy-a` and `reefy-b` `Boot####` entries, orders the new pair
with the active slot first, and deletes the old pair by exact boot number. The
new entry identities cause affected firmware to refresh its hidden/default
state without Reefy writing undocumented vendor variables.

The approach was validated in both directions on:

- GMKtec NucBox K13 with AMI Aptio firmware; and
- MSI PRO B550M-VC WIFI, board MS-7C95.

## Disk and boot-entry model

| Slot | Partition | Filesystem label | EFI executable |
|---|---:|---|---|
| A | 1 | `reefy-a` | `\EFI\Boot\bootx64.efi` |
| B | 2 | `reefy-b` | `\EFI\Boot\bootx64.efi` |

`Boot####` numbers are assigned by firmware and are not stable slot IDs.
Reefy identifies a slot by label and partition UUID, and it determines the
active slot from the ESP actually mounted at `/mnt/reefy`. It never assumes
that A is `Boot0000` or B is `Boot0001`.

Standard UEFI variables have distinct roles:

- `BootCurrent` identifies the entry that started this boot.
- `BootOrder` defines persistent preference.
- `BootNext` requests one entry for the next boot only and is consumed by
  firmware.
- `Boot####` entries associate a label with a device path and EFI executable.

## Entry repair

`reefy-efi fix` is idempotent and runs during storage boot and before an
update. It:

1. Finds the disk mounted at `/mnt/reefy`.
2. Reads partition UUIDs for slots A and B.
3. Temporarily remounts `efivarfs` read-write.
4. Removes auto-created non-Reefy entries that point at Reefy's partitions.
5. Removes stale `reefy-a` or `reefy-b` entries whose device path does not
   contain the expected partition UUID.
6. Creates a missing slot entry with `efibootmgr -c`.
7. If neither Reefy slot is first, sets the valid Reefy pair at the front of
   `BootOrder`.
8. Returns `efivarfs` to read-only.

Matching both label and partition UUID handles a replaced boot disk, mislabeled
entries, duplicate firmware-generated entries, and interrupted earlier repair.

## Update flow

`reefy-efi update <efi-file> [-r]` uses a non-blocking update lock and performs
the following sequence:

1. Resolve active and inactive slots from the mounted ESP.
2. Repair entries and re-read the inactive slot's assigned `Boot####` number.
3. Clear any stale mount of the inactive partition left by an interrupted
   update.
4. Format the inactive ESP as FAT32 with its slot label.
5. Mount it in a temporary directory.
6. Preserve `/mqtt` and the `/reefy` device namespace from the active ESP.
7. Copy the new image to `EFI/Boot/bootx64.efi`, sync, and unmount.
8. Set `BootNext` to the inactive slot and read the variable back.
9. Reboot when `-r` was requested.

The temporary mount has both an exit trap and a pre-update stale-mount sweep.
The trap handles normal failures; the sweep recovers from power loss, OOM, or
`SIGKILL`, which cannot execute a shell trap.

### `BootNext` compatibility fallback

If the read-back value is not the exact inactive entry, Reefy clears
`BootNext` and writes the new image to the active ESP too. That path loses the
one-shot rollback property, but it avoids reporting a safe A/B update when the
firmware did not accept the standard request.

## Trial boot and health confirmation

After firmware consumes `BootNext`, the new slot is active while the old slot
remains first in persistent `BootOrder`.

`reefy-boot-confirm` recognizes this from
`BootCurrent != BootOrder[0]`. It waits up to 300 seconds for:

- `reefy-storage.service`; and
- `reefy-control.service`.

If either unit fails, confirmation exits without changing the persistent
default. If both become active, the script runs `reefy-efi confirm` and then
stops the 360-second boot watchdog.

If confirmation never completes, `reefy-boot-watchdog` forces a sysrq reboot.
`BootNext` has already been consumed, so the previous first `BootOrder` entry
is selected again.

## Why plain `BootOrder` was insufficient

The original confirmation path put `BootCurrent` first with
`efibootmgr -o`. Linux immediately showed the requested order, but later
normal reboots on the affected systems returned to the old slot.

On the K13, BIOS setup exposed the effective preference under its UEFI USB BBS
priorities. Manual changes updated standard `BootOrder` and AMI variables such
as `OldBootOrder` and `UefiDevOrder`. Writing standard `BootOrder` alone left
that private priority unchanged.

The MSI system exposed a different shape. Manual changes moved both
`BootOrder` and `DefaultBootOrder`, but directly writing those variables still
did not override the unseen BBS source. Variables used by the K13 were not
present.

These observations rule out a portable implementation based on a specific
AMI private variable. Linux also marks many non-standard EFI variables
immutable to reduce the risk of firmware corruption.

## Persistent confirmation with fresh entries

`reefy-efi confirm` does nothing on a normal default-slot boot. For a trial
boot it calls the fresh-entry commit path:

1. Repair the current Reefy entries.
2. Verify exactly one valid A entry and one valid B entry by label and
   partition UUID.
3. Create a new A entry and discover its firmware-assigned number by set
   difference.
4. Create a new B entry and discover its assigned number the same way.
5. Put the new active entry first and the new inactive entry second in
   `BootOrder`.
6. Delete the old A and B entries by their exact numbers.
7. Sync and verify that exactly one valid entry per slot remains and that the
   new pair leads `BootOrder`.

If creation or ordering fails, the code removes newly created entries where
possible and returns `efivarfs` to read-only. Deletion of old entries happens
only after the new pair is first, so a deletion failure leaves a bootable
standard order.

On the validated K13, firmware refreshed `OldBootOrder` and `UefiDevOrder`
during the following boot. On the MSI board, firmware refreshed
`DefaultBootOrder`. Reefy did not write any of those variables directly.

## Validation results

The auto-numbered flow was exercised without assuming fixed boot numbers:

```text
K13:    A -> B, then B -> A
MSI:    A -> B, then B -> A
```

In each direction:

- the inactive slot booted once through `BootNext`;
- health confirmation created a fresh pair;
- a later plain reboot stayed on the newly confirmed slot; and
- firmware's private/default state followed the fresh standard entries.

The update tests also cover digit-ending boot devices such as NVMe, stale
mount cleanup, active or inactive entry rendering, and confirmation rollback
behavior.

## Operational commands

```bash
# Show active and inactive slots plus UEFI variables
reefy-efi status

# Repair stale, duplicate, or missing standard entries
sudo reefy-efi fix

# Schedule slot A or B once, without changing the persistent default
sudo reefy-efi set-next a
sudo reefy-efi set-next b

# Write the inactive slot and schedule a trial reboot
sudo reefy-efi update /path/to/bootx64.efi -r

# Normally called by reefy-boot-confirm after health checks
sudo reefy-efi confirm
```

Changing EFI state or forcing a trial boot can make a machine temporarily
unreachable. Manual use should include console or physical recovery access.

## Rejected approach: vendor-variable writes

Direct writes to the K13's observed AMI variables proved that its BBS list was
the effective selector, but this is not Reefy's production mechanism.

Vendor-variable formats differ between boards, may be immutable, and are not
part of the standard UEFI boot-manager contract. A guessed write can corrupt
firmware configuration. Reefy therefore uses only standard `Boot####`,
`BootOrder`, and `BootNext` operations and lets firmware update its private
state.

## Implementation files

| File | Responsibility |
|---|---|
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-efi` | Entry repair, status, one-shot selection, image update, and fresh-entry confirmation. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-boot-confirm` | Trial-slot health checks and confirmation. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/reefy-boot-watchdog` | Trial timeout and rollback reboot. |
| `board/reefy/reefy/rootfs-overlay/usr/bin/boot-reefy-storage.sh` | Active ESP detection, mount, and boot-entry repair. |
| `board/reefy/reefy/tests/test_reefy_efi.py` | Source and loop-device coverage for update and confirmation behavior. |
