### Схема v7: внешние репозитории сборки

Схема v7 добавляет `build_repositories`: команду собирают из отдельно
запиненного внешнего Git-репозитория, а не из корня сборки внутри скилла.
Пакет скилла объявляет каноничный сетевой источник, точный объект и
(опционально) точный тег; выбор кредов, тулчейна, вывода и подписи остаётся
за оператором и менеджером.

```json
{
  "schema_version": 7,
  "capabilities": {
    "network": ["gitlab.example.com"],
    "filesystem": "repo",
    "exec": "none",
    "secrets": "none",
    "env_read": [],
    "prompt_scope": "Read-only documentation lookups."
  },
  "build_repositories": {
    "tool-cli": {
      "git": "git@gitlab.example.com:group/tool-cli.git",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "0123456789abcdef0123456789abcdef01234567"
      },
      "tag": "v1.2.0"
    }
  },
  "commands": {
    "tool": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "tool-cli",
      "target": "tool"
    }
  }
}
```

Внешний репозиторий обязан содержать в корне закрытый дескриптор
`skill-build.json`:

```json
{
  "schema_version": 1,
  "targets": {
    "tool": {
      "driver": "go-repository-v1",
      "build_root": ".",
      "source_dir": "cmd/tool"
    }
  }
}
```

`build_root` содержит `go.mod`; модули вендорятся (`go mod vendor`), сборка
идёт без сети. Скомпилированный артефакт никогда не коммитится ни в скилл,
ни во внешний репозиторий. Приватные SSH-источники требуют явного выбора
кредов оператором; полный контракт, включая скоупы `build_ssh` в глобальном
конфиге, описан в `docs/external-build-repositories.md`. `go-repository-v1`
поддерживается только на macOS и Windows.

