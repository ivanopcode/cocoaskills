---
name: fixture-skill
description: fixture for lint verification
---

# fixture-skill

Скилл экспортирует команду `mytool`.

Резолвь экспортированное имя `mytool` один раз до первого вызова:

1. Вверх от текущей директории, затем от физического пути `SKILL.md`: ближайший `mytool` в `.agents/bin` (управляемые шимы на Windows несут суффикс `.cmd`: `mytool.cmd`).
2. Иначе `mytool` в `global/bin` под домом CocoaSkills (родитель `CSK_CONFIG`, по умолчанию `~/.cocoaskills`), с тем же `.cmd` на Windows.
3. Иначе голое имя `mytool`: только после валидации `command -v mytool` в POSIX-шеллах, `Get-Command mytool` в PowerShell.

Если лаунчер не найден, установка неполна: сообщи об этом и остановись; не выкачивай и не собирай CLI ад-хок.
