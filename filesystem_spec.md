# JIFS Filesystem Spec

## Disk Layout

| Block(s) | Use |
|---|---|
| 0 | Superblock |
| 1 | Allocation bitmap |
| 2 through metadata end | Metadata blocks |
| Remaining blocks | Directory tables and file data |

## Superblock

Stored in block 0.

Fields:

- magic: filesystem name/version
- block_size: 512
- total_blocks
- bitmap_start
- meta_start
- meta_block_count
- data_start
- root_meta_block
- flags
- mount_count
- checksum

## Allocation Bitmap

- Stored in block 1.
- 1 bit per block.
- `1` means allocated; `0` means free.

## Metadata Blocks

Each file or directory has one metadata block.

### File Metadata

Fields:

- type: file
- links: hard link count
- size: file size in bytes
- first_data_block
- last_data_block
- ctime
- mtime

Empty files use `65535` for `first_data_block` and `last_data_block`.

### Directory Metadata

Fields:

- type: directory
- links
- parent_meta_block
- table_block
- num_entries
- flags
- ctime
- mtime

The root directory points to itself as parent.

## File Data Blocks

Each file data block stores:

- next block pointer: 2 bytes
- payload: 510 bytes

`65535` means end of file chain.

Large files are stored as linked data blocks.

## Directory Tables

Each directory has one directory table block.

- 16 entries per directory
- 32 bytes per entry
- Max filename length: 24 ASCII bytes

Directory entry fields:

- state: empty, used, or deleted
- entry_type: file or directory
- name_hash
- target_meta_block
- name_len
- name

Deleted entries are tombstones so probing still works.

## Name Lookup

Hash:

```
h = (h * 31 + byte) % 65536
```

- Hash uses lowercase ASCII name bytes.
- Start slot is `hash % 16`.
- Collisions use linear probing.
- Lookup stops at an empty slot.