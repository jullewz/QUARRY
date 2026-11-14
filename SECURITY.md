# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Please report security problems privately through
[GitHub private vulnerability reporting](https://github.com/jullewz/quarry/security/advisories/new), not in a public
issue.

Relevant problems include:

- a crafted database or log file that makes Quarry crash with an exception other than `CorruptDatabaseError`,
  loop forever, or use unbounded memory
- SQL that escapes into Python: Quarry never calls `eval` or `exec`, and SQL must not reach either
- data loss or corruption after a crash, against what [docs/architecture.md](docs/architecture.md) promises
- the shell writing outside the paths you give `.dump`, `.export` or `.import`

## Notes for users

- Always pass values as parameters (`?` or `:name`). Never build SQL strings from user input.
- `PRAGMA synchronous = 0` turns off `fsync`. A power failure can then lose recent commits.
- Quarry does not encrypt data. Protect database files with file permissions.
- Opening an untrusted database file is safe from code execution, but a hostile file can make queries slow.
