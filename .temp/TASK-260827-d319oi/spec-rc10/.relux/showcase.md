---
title: Curator Specification
summary: The open protocol behind the Curator agent environment manager, implementable by any conforming manager.
category: Agent infrastructure
featured: true
site: https://github.com/relux-works/curator
---

## What it is

The Curator Specification defines an open protocol for managing AI agent
environments: skill package formats, project manifests, dependency closure
resolution, installation layouts, MCP server requirements, and a
cryptographically verifiable audit registry.

The protocol is independently implementable. The reference implementation is
Curator (Go); an independent Python implementation conforms to the same wire
formats, which is the working proof that the document alone is enough to
build an interoperable manager. That matters for organizations whose internal
security policies require an in-house build instead of adopting an external
binary.

## Highlights

- Byte-exact contracts: content hashes, install markers, and canonical
  signing bytes are specified to the byte, gated by golden fixtures.
- Deny-wins audit federation over Ed25519-signed records with rollback and
  freeze detection.
- Multi-agent delivery with managed adapters, three install scopes, and
  Windows as a first-class target.
