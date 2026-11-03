# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting on [ruizhengu/kiwi-dl](https://github.com/ruizhengu/kiwi-dl),
or contact [@ruizhengu](https://github.com/ruizhengu).

Include what the problem is and its impact, how to reproduce it, and a fix if
you have one. Remove link keys (the part after `#`), proxy credentials and
private paths from anything you paste.

## Supported versions

Only the latest release is supported.

## Scope

In scope: link and key handling, the network and proxy code, file integrity
checks, and anything that could overwrite or delete local files or leak a link
key or proxy password.

Out of scope: MEGA's own service, and problems in third-party dependencies
(report those upstream).
