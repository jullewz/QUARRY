# File format

A Quarry database is one file of fixed-size pages, plus a write-ahead log next to it while the database is open.
All integers are big-endian unless noted.

## Pages

The page size is set when the file is created: a power of two from 512 to 65,536 bytes, 4,096 by default. Page
*n* starts at byte `n × page_size`. The first byte of every page says what it is.

### Page 0: the header

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 1 | `0x51` (`'Q'`) |
| 1 | 18 | magic: `QuarryDB format 1\0` |
| 19 | 4 | page size |
| 23 | 4 | page count |
| 27 | 4 | first free page (0 if none) |
| 31 | 4 | number of free pages |
| 35 | 4 | root page of the schema tree (always 1) |
| 39 | 4 | schema cookie, incremented by every schema change |
| 43 | 4 | user version (`PRAGMA user_version`) |
| 47 | 8 | commit counter |

The rest of the page is zero.

### Leaf page (type 1)

```text
type (1) · cell count (u16) · cells...
cell = key length (varint) · key record · flag (1)
       flag 0: value length (varint) · value bytes
       flag 1: first overflow page (u32) · total value length (varint)
```

### Interior page (type 2)

```text
type (1) · key count n (u16) · child 0 (u32) · n × (key length (varint) · key record · child (u32))
```

Child *i* holds keys below separator *i*. The last child holds keys at or above the last separator.

### Overflow page (type 3)

```text
type (1) · next overflow page (u32, 0 at the end) · byte count (u32) · bytes
```

### Free page (type 4)

```text
type (1) · next free page (u32)
```

## Records

Rows and keys share one encoding: a varint count, then each value as a tag byte plus payload.

| Tag | Value | Payload |
| ---: | --- | --- |
| 0 | NULL | none |
| 1 | INTEGER | zigzag varint |
| 2 | REAL | 8-byte IEEE 754 double |
| 3 | TEXT | varint length + UTF-8 |
| 4 | BLOB | varint length + bytes |
| 5 | the integer 0 | none |
| 6 | the integer 1 | none |

Varints are unsigned LEB128: seven bits per byte, low bits first, high bit set on every byte but the last.

## Trees

| Tree | Key | Value |
| --- | --- | --- |
| schema (page 1) | `(id,)` | record `(type, name, table_name, root_page, sql)` |
| table | `(rowid,)` | record of the row's columns (an `INTEGER PRIMARY KEY` column is stored as NULL: the key holds it) |
| index | `(column values..., rowid)` | empty |

Schema rows have type `table`, `index`, `view` or `sequence`. A `sequence` row stores the largest rowid
an `AUTOINCREMENT` table has used, in the `root_page` field. Indexes made for `UNIQUE` and `PRIMARY KEY`
constraints have `sql` NULL and the name `quarry_autoindex_<table>_<n>`.

Rows written before `ALTER TABLE ... ADD COLUMN` have fewer values. Reading fills the missing ones with the new
column's default.

## The write-ahead log (`<database>-wal`)

```text
header (16 bytes): magic "QWAL" · page size (u32) · salt (u32) · checkpoint sequence (u32)
frame:             page number (u32) · db size in pages (u32, non-zero only on the last frame of a commit)
                   · salt (u32) · checksum (u32) · page bytes
```

`checksum = crc32(page bytes, crc32(first 12 bytes of the frame header, previous checksum))`. The chain starts
from the CRC-32 of the log header. A reader stops at the first frame with the wrong salt or checksum, and uses
frames only up to the last commit frame before that point.

A page's current content is its newest committed frame in the log, if there is one, and otherwise its place in
the main file. A checkpoint copies those frames into the main file, cuts the file to the committed size,
flushes it, and then resets the log with `salt + 1`. That turns any frames left behind into garbage.
