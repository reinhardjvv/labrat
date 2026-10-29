# Security policy

LABRAT controls physical equipment, so a security bug can be a safety bug. Please report it privately.

## Reporting

Use [private vulnerability reporting](https://github.com/reinhardjvv/labrat/security/advisories/new) on
GitHub. Do not open a public issue. Include the version, what an attacker can do, and how to reproduce it.

You will get an acknowledgement within 7 days and an assessment within 30 days. Fixes are released as soon
as they are ready, with credit to you unless you prefer otherwise.

## Scope

In scope: anything that lets a request bypass a limit, a lock, a role or authentication, prevents a safe
state, leaks a token or key, or crashes a nest or rat from the network.

Out of scope: attacks that need an admin token or root on the nest or rat, and denial of service from inside
a lab network that LABRAT documents as trusted. The [safety and security guide](docs/safety.md) describes the
model.

## Supported versions

The latest minor release receives security fixes.
