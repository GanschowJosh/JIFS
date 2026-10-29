# JIFS — Josh & Iurii File System

JIFS is a small, persistent filesystem implemented in Python on top of a
block-addressed virtual disk. It was created by me and Iurii
Chmykhun as a data-structures project exploring how filesystems organize
metadata, directories, free space, links, and multi-block files.

The project includes an interactive shell and a generator that creates a fresh
sample disk image. It uses only the Python standard library.

## Features

- Persistent 512-byte block storage in `disk.img`
- Superblock containing filesystem layout information
- Bitmap-based free-block tracking
- Separate metadata blocks for files and directories
- Hierarchical absolute and relative paths
- Hash-table directory lookup with linear probing and tombstones
- Linked data blocks for files larger than one block
- File and directory creation and deletion
- File copying and hard links with reference counts
- Creation and modification timestamps
- Block read/write statistics when the shell exits

## Filesystem layout

| Blocks | Purpose |
|---|---|
| 0 | Superblock |
| 1 | Allocation bitmap |
| 2–33 | File and directory metadata |
| 34 onward | Directory tables and file data |

Each file-data block holds a two-byte pointer to the next block and up to 510
bytes of payload. Each directory occupies a fixed 16-entry hash table. Names
are ASCII, case-sensitive after lookup, and limited to 24 bytes. See
[`filesystem_spec.md`](filesystem_spec.md) for the complete binary format.

## Requirements

- Python 3.10 or newer

No third-party packages are required.

## Quick start

Create a fresh sample filesystem:

```bash
python3 generate_example.py
```

Start the interactive shell:

```bash
python3 main.py
```

The generator replaces `disk.img`, so copy an existing image first if you want
to preserve it.


## Commands

| Command | Description |
|---|---|
| `ls [path]` | List a directory |
| `cd [path]` | Change the current directory |
| `pwd` | Print the current absolute path |
| `mkdir <path>` | Create a directory |
| `touch <path>` | Create an empty file |
| `write <path> <text>` | Create or overwrite a file |
| `read <path>` / `cat <path>` | Print a file's contents |
| `cp <source> <destination>` | Copy a file |
| `ln <source> <destination>` | Create a hard link |
| `rm <path>` | Remove a file or hard link |
| `rmdir <path>` | Remove an empty directory |
| `stat <path>` | Display filesystem metadata |
| `help` | Show the command reference |
| `exit` | Exit and print disk I/O statistics |

Paths may be absolute or relative and can contain `.` and `..` components.

## Project structure

- `main.py` — filesystem implementation and interactive shell
- `Disk.py` — block-oriented virtual disk interface
- `generate_example.py` — sample image builder
- `filesystem_spec.md` — on-disk format specification

## Current limitations

- A directory can contain at most 16 entries.
- Filenames are limited to 24 ASCII bytes.
- The sample image contains 256 blocks and 32 metadata blocks.
- There are no permissions, journaling, snapshots, or concurrent access.
- The shell writes text data; it does not provide a binary-file import command.

## Authors

- Joshua Ganschow
- Iurii Chmykhun

