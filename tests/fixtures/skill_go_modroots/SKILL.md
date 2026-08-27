# go module roots E2E fixture

A schema-8 skill whose `go-v1` command declares one first-party module root.
The build root replaces `example.test/board` with the declared directory, the
vendor tree carries the compiled copy, and the manager checks the declaration
against `vendor/modules.txt` one to one.

## Commands

- `modroot-tool` prints the label the declared module provides and its argv.
