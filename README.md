# MutaBasic

**Консольный BASIC на Python с возможностью изменять собственный листинг во время исполнения.**

MutaBasic — экспериментальный интерпретатор подмножества QBasic. Главная особенность — инструкции `SOURCE`, позволяющие программе инспектировать и изменять свой исходный текст по вычисляемым условиям, а затем отменять изменения.

Текущая версия кода: **1.0.0**. Python **3.10+**, только стандартная библиотека.

> Это не полная реализация Microsoft QBasic и не песочница. Код требует дальнейшего тестирования. Встроенные smoke-тесты доступны через `--self-test`; их наличие не означает полной проверки совместимости.

## Быстрый старт

Поместите `mutabasic.py` в рабочую папку:

```bash
python mutabasic.py
```

Без аргументов открывается REPL и создаётся пустой проект **в памяти**. Файл проекта автоматически не создаётся.

```text
muta> 10 PRINT "Hello from MutaBasic"
muta> 20 END
muta> RUN
muta> PROJECT SAVE hello.mbp
muta> SAVE hello.bas
```

Команды и имена переменных регистронезависимы. Содержимое строк сохраняет регистр; правила путей и переменных окружения определяются ОС.

## Установка и структура проекта

Для запуска из клонированного репозитория достаточно Python 3.10 или новее:

```bash
python mutabasic.py --self-test
```

Проект также содержит `pyproject.toml`, поэтому его можно установить в
окружение в режиме editable для встраивания:

```bash
python -m pip install -e .
```

Основные файлы:

| Путь | Назначение |
|---|---|
| `mutabasic.py` | Совместимый CLI-фасад |
| `mutabasic_pkg/core.py` | VM, компиляция листинга и REPL |
| `mutabasic_pkg/api.py` | API для Python-приложений |
| `mutabasic_pkg/parser.py` | Текущая parsing-абстракция |
| `mutabasic_pkg/registry.py` | Реестр пользовательских функций и команд |
| `mutabasic_pkg/security.py` | Политика выполнения shell-команд |
| `tests/` | Регрессионные тесты на `unittest` |

Публичные имена экспортируются из `mutabasic_pkg`, поэтому приложениям не
нужно импортировать внутренний модуль `core`.

## Зачем нужен MutaBasic

- Эксперименты с самомодифицирующимися программами.
- Учебные демонстрации интерпретации и управления исполнением.
- Интерактивное исследование алгоритмов с редактированием листинга.
- Сохранение экспериментов в проектах и снимках исполнения.

Это исследовательский инструмент, а не рекомендация заменять самомодификацией обычные функции и структуры данных в производственных приложениях.

## Основные возможности

| Область | Возможности |
|---|---|
| Исполнение | REPL, CLI, нумерованные строки, метки, непосредственные инструкции |
| Управление | IF, FOR, WHILE, DO, GOTO, GOSUB, ON … GOTO/GOSUB |
| Данные | Числа, строки, многомерные массивы, DATA/READ/RESTORE |
| Самоинспекция | LINE$, LINEEXISTS, FINDLINE, PROGRAM$ |
| Изменение кода | SOURCE SET, INSERT, DELETE, REPLACE, UNDO, REDO |
| Отладка | STEP, BREAK, CONT, TRACE, ограничение инструкций |
| Состояние | Системные показатели, счётчики, именованные таймеры |
| Хранение | Листинги, JSON-проекты, переменные, снимки |
| Файлы | Последовательные текстовые файлы UTF-8, операции с путями |
| ОС | Команды оболочки при явном разрешении --allow-shell |

## Пример: программа изменяет себя

```basic
10 PRINT "MutaBasic "; VERSION$
20 mode = 1
30 IF mode = 1 THEN SOURCE SET 100, "PRINT ""Alternative algorithm"""
40 PRINT "Line 100: "; LINE$(100)
50 GOTO 100
100 PRINT "Original algorithm"
110 STOP
120 END
```

После остановки можно исследовать изменение:

```text
muta> HISTORY
muta> UNDO
muta> LIST 100 120
muta> REDO
muta> SNAPSHOT experiment.json
muta> CONT
```

`UNDO` не возвращает исполнение назад и не отменяет значения переменных. Это отмена изменений листинга. Для восстановления исполнения используются снимки.

## Инструкции SOURCE

```basic
SOURCE SET number, text$
SOURCE INSERT number, text$
SOURCE DELETE first [, last]
SOURCE REPLACE old$, new$ [, first, last, case_sensitive]
SOURCE UNDO [count]
SOURCE REDO [count]
```

- `SET` добавляет или заменяет строку.
- `INSERT` требует свободный номер.
- `DELETE` удаляет строку или диапазон.
- `REPLACE` по умолчанию не учитывает регистр; ненулевой последний аргумент включает его учёт.
- `UNDO` и `REDO` поддерживают несколько шагов.
- История ограничена параметром `--history-limit` — по умолчанию 100.

### Правила изменения во время исполнения

1. Каждая инструкция `SOURCE` применяется как отдельное изменение с предварительной компиляцией структуры листинга.
2. В работающей или приостановленной программе блоки должны оставаться структурно завершёнными.
3. Заголовок активного `FOR` или `DO` нельзя удалить или изменить. Тело цикла менять можно.
4. Исполнение продолжается с прежнего следующего адреса либо ближайшего сохранившегося адреса после него.
5. Вставленные до точки продолжения строки не исполняются задним числом.
6. Указатель DATA сохраняет порядковую позицию с ограничением новой длиной данных.
7. Для предсказуемых экспериментов используйте одну инструкцию на строке.

Предварительная проверка структуры не является полной статической проверкой синтаксиса и поведения всех инструкций.

### Самоинспекция

```basic
PRINT LINE$(100)
PRINT LINEEXISTS(100)
PRINT FINDLINE("PRINT")
PRINT PROGRAM$
```

`FINDLINE(text$ [, start, case_sensitive])` возвращает номер строки или 0.

## Работа в оболочке

| Команда | Назначение |
|---|---|
| HELP [topic] | Справка |
| NEW [name] | Пустой проект |
| LOAD file.bas / SAVE file.bas | Листинг |
| PROJECT LOAD file.mbp / PROJECT SAVE file.mbp | JSON-проект |
| LIST [first [last]] | Просмотр строк |
| EDIT number text / INSERT number text | Редактирование |
| DELETE first [last] | Удаление |
| FIND text / REPLACE "old" "new" | Поиск и замена |
| UNDO [n] / REDO [n] / HISTORY | История |
| RUN [line_or_label] | Новый запуск с очисткой пользовательских переменных |
| CONT / STEP [n] | Продолжение и шаги |
| BREAK [numbers] / UNBREAK [numbers] | Точки останова |
| VARS / SYS / EVAL expression | Инспекция |
| SET variable=expression | Присваивание |
| CHECK | Проверка структуры блоков |
| PWD / CD path / FILES [pattern] | Рабочая папка |
| QUIT / EXIT | Выход |

Темы справки: `commands`, `language`, `source`, `system`, `files`, `snapshots`, `functions`.

Команды оболочки можно писать с префиксом `:`. Для принудительного исполнения BASIC используйте `BASIC`:

```text
muta> BASIC RESTORE
```

Это сбрасывает указатель DATA. Команда оболочки `RESTORE file.json` загружает снимок.

> NEW и LOAD не запрашивают подтверждение потери несохранённых изменений.

## Системные показатели

Динамические переменные доступны только для чтения:

- Идентификация: `VERSION$`, `PROJECT$`, `CWD$`, `STATE$`, `PID`.
- Время: `DATE$`, `TIME$`, `TIMER`, `UPTIME`, `ELAPSED`.
- Счётчики: `RUNCOUNT`, `LAUNCHCOUNT`, `STEPS`, `TOTALSTEPS`.
- Исполнение: `CURRENTLINE`, `NEXTLINE`, `CALLDEPTH`, `LOOPDEPTH`, `DATAPOS`.
- Листинг: `LINECOUNT`, `HISTORYCOUNT`, `REDOCOUNT`, `PROGRAM$`.
- Диск: `FREEDISK`, `TOTALDISK` — байты на диске рабочей папки.
- Диагностика: `LASTEXIT`, `LASTOUTPUT$`, `LASTERROR$`, `ERRORLINE`.

```basic
PRINT SYS$("VERSION")
PRINT FREEDISK
PRINT ENVIRON$("PATH")
TIMER START "work"
SLEEP 1
TIMER STOP "work"
PRINT TIMERGET("work")
```

`RUNCOUNT` хранится в проекте и снимках. `LAUNCHCOUNT` хранится в отдельном файле состояния пользователя; `--no-state` отключает его сохранение. Обновление счётчика не синхронизировано между одновременно запускаемыми процессами.

`ELAPSED` учитывает время исполнения, включая INPUT/SLEEP, но не паузы REPL. Именованные таймеры считают монотонное реальное время, включая паузы, пока не остановлены.

## Встроенные функции

Интерпретатор предоставляет следующие группы функций:

| Группа | Функции |
|---|---|
| Математика | `ABS`, `ATN`, `COS`, `SIN`, `TAN`, `SQR`, `EXP`, `LOG`, `INT`, `FIX`, `CINT`, `CLNG`, `CSNG`, `CDBL`, `SGN`, `MIN`, `MAX`, `ROUND`, `FLOOR`, `CEIL`, `RND` |
| Строки | `LEN`, `ASC`, `CHR$`, `STR$`, `VAL`, `HEX$`, `OCT$`, `LEFT$`, `RIGHT$`, `MID$`, `LCASE$`, `UCASE$`, `LTRIM$`, `RTRIM$`, `SPACE$`, `STRING$`, `INSTR`, `TAB`, `SPC`, `INKEY$` |
| Массивы и переменные | `LBOUND`, `UBOUND`, `VAREXISTS` |
| Листинг и система | `LINE$`, `LINEEXISTS`, `FINDLINE`, `TIMERGET`, `SYS`, `SYS$`, `ENVIRON$` |
| Файлы | `FILEEXISTS`, `DIREXISTS`, `FILESIZE`, `READFILE$`, `EOF`, `LOF`, `LOC`, `SEEK` |
| Оболочка | `SHELL`, `SHELL$` |

Номера измерений массивов начинаются с 1. `LBOUND` и `UBOUND` принимают
необязательный номер измерения; при первом обращении массив автоматически
создаётся до индекса 10. Неинициализированные числовые переменные равны `0`,
строковые — `""`.

`SHELL(command$)` возвращает код завершения команды, а `SHELL$(command$)` —
перехваченный stdout. Обе функции подчиняются текущей `ShellPolicy`; stderr
не добавляется к возвращаемому stdout.

## Файлы и команды ОС

```basic
OPEN "result.txt" FOR OUTPUT AS #1
PRINT #1, "Result: "; 42
CLOSE #1

WRITEFILE "note.txt", "Hello"
PRINT READFILE$("note.txt")
```

Доступны `INPUT`, `OUTPUT`, `APPEND`, CSV-подобный `WRITE`/`INPUT #`, `LINE INPUT #`, `EOF`, `LOF`, `LOC`, `SEEK`, а также `COPYFILE`, `APPENDFILE`, `CHDIR`, `MKDIR`, `RMDIR`, `KILL`, `NAME`.

Команды оболочки требуют явного разрешения:

```bash
python mutabasic.py --allow-shell
```

```basic
SHELL "echo Hello"
output$ = SHELL$("echo Captured output")
PRINT output$
PRINT LASTEXIT
```

`SHELL$` захватывает stdout, но не объединяет его со stderr. Синтаксис команды зависит от ОС и её оболочки.

## Проекты и снимки

| Формат | Содержимое |
|---|---|
| `.bas` | Текстовый листинг |
| `.mbp` | JSON: листинг, имя, история изменений, RUNCOUNT |
| variables JSON | Переменные, массивы, OPTION BASE |
| snapshot JSON | Проект, переменные, позиция, стеки, DATA, RNG, таймеры, точки останова |

```text
muta> VARSAVE variables.json
muta> VARLOAD variables.json
muta> SNAPSHOT snapshot.json
muta> RESTORE snapshot.json
muta> CONT
```

Из листинга доступны `VARSAVE "file"`, `VARLOAD "file"`, `SNAPSHOT "file"`.

Снимок из BASIC сохраняет продолжение после инструкции SNAPSHOT. При открытых BASIC-файлах создание снимка запрещено. Файлы, рабочая папка, окружение ОС и эффекты внешних команд не восстанавливаются. Право запуска SHELL задаётся текущим CLI, а не снимком.

## CLI

```bash
python mutabasic.py --help
python mutabasic.py --version
python mutabasic.py --self-test
python mutabasic.py example.bas
python mutabasic.py example.bas --no-run
python mutabasic.py example.bas --trace -i
python mutabasic.py --project example.mbp --no-run
python mutabasic.py --restore snapshot.json -i
python mutabasic.py -e "EVAL 2^10"
```

| Ключ | Назначение |
|---|---|
| `-i`, `--interactive` | REPL после исполнения |
| `--no-run` | Загрузка без запуска с открытием REPL |
| `-e`, `--execute` | Команда оболочки; можно повторять |
| `--project`, `--restore` | Загрузка проекта или снимка |
| `--name`, `--cwd` | Имя проекта и рабочая папка |
| `--allow-shell` | Разрешить команды ОС |
| `--allow-shell-command` | Повторяемый allow-list executable после `--allow-shell` |
| `--max-steps` | Лимит инструкций на RUN/CONT; 0 — без лимита |
| `--history-limit` | Глубина истории |
| `--trace` | Трассировка в stderr |
| `-q`, `--quiet` | Убрать приветствие и часть служебного вывода |
| `--check` | Только проверка структуры блоков |
| `--state-file`, `--no-state` | Постоянный счётчик запусков |
| `--project-out`, `--vars-out`, `--snapshot-out` | Сохранение перед выходом |
| `--self-test` | Встроенные smoke-тесты |

## Совместимость и ограничения

- Нет графики, SUB/FUNCTION, TYPE, SELECT CASE, ELSEIF, ON ERROR, PRINT USING.
- Нет двоичных и RANDOM-файлов.
- Нет вложенного однострочного IF и NEXT с несколькими счётчиками.
- Численная модель Python не эмулирует переполнения и точные типы Microsoft QBasic.
- PRINT форматируется упрощённо; запятая выводит табуляцию, TAB/SPC возвращают пробелы.
- INPUT # читает одну CSV-строку за вызов.
- SEEK использует позиции текстового потока Python, а не произвольные байтовые смещения QBasic.
- Произвольные переходы через границы активных циклов не очищают их стеки.
- Снимки и история листинга не являются откатом внешнего мира.

## Проверка и сообщения об ошибках

```bash
python mutabasic.py --self-test
python mutabasic.py example.bas --check
```

Smoke-тесты покрывают несколько сценариев выражений, массивов, циклов, самомодификации, истории, файлов и сохранения состояния. Дополнительный регрессионный набор запускается без зависимостей:

```bash
python -m unittest discover -s tests -v
```

## Архитектура и встраивание

`mutabasic.py` теперь является совместимым CLI-фасадом. Реализация находится в
`mutabasic_pkg`: `core.py` содержит VM/REPL (поэтапно выделяемый legacy-слой),
`parser.py` предоставляет стабильную parsing-связь для будущего AST,
`security.py` — политики shell, `registry.py` — расширения, а `api.py` —
небольшой Python API:

```python
from mutabasic_pkg import MutaBasic, ShellPolicy
app = MutaBasic(shell_policy=ShellPolicy(
    enabled=True, commands=frozenset({"echo"})
))
app.load({10: 'PRINT "embedded"', 20: "END"}).run()
```

Методы `load()` и `run()` возвращают объект `MutaBasic`, поэтому вызовы можно
связывать. Для выполнения непосредственной BASIC-инструкции используйте
`execute()`, а для чтения значения выражения — `evaluate()`:

```python
from mutabasic_pkg import MutaBasic

app = MutaBasic().load({
    10: 'message$ = "hello"',
    20: 'END',
})
app.run()
assert app.evaluate("message$") == "hello"
app.execute('PRINT message$')
app.close()
```

`create_vm()` создаёт низкоуровневый `VM`; параметр `allow_shell=True` включает
совместимый режим разрешения оболочки, а `shell_policy=ShellPolicy(...)`
позволяет задать явный allow-list исполняемых файлов. `MutaBasic.close()`
закрывает открытые BASIC-файлы и должен вызываться после завершения работы с
VM.

Функции и команды можно добавлять без изменения интерпретатора:
`register_function("NAME", callable)` и `register_command("NAME",
handler(shell, argument))`. `parse_source()` и `tokenize()` являются
текущей parsing-абстракцией; полноценный AST остаётся отдельным этапом.
Регистрация действует для VM, созданных после регистрации:

```python
from mutabasic_pkg import VM, register_function

register_function("TRIPLE", lambda value: value * 3)
vm = VM()
assert vm.evaluate("TRIPLE(7)") == 21
```

## Политика безопасности

`--allow-shell` сохраняет прежний явный opt-in. Для встраивания рекомендуется
`ShellPolicy(enabled=True, commands=frozenset({...}))`, ограничивающая
исполняемые команды на уровне проекта/VM. В CLI allow-list задаётся повторяемым
параметром:

```bash
python mutabasic.py --allow-shell \
  --allow-shell-command echo \
  --allow-shell-command printf
```

Если указан хотя бы один `--allow-shell-command`, только перечисленные имена
исполняемых файлов проходят проверку, а синтаксис цепочек, перенаправлений и
подстановок оболочки запрещается. Это не sandbox: BASIC по-прежнему
может читать и изменять доступные процессу файлы, поэтому используйте
отдельную ОС-пользовательскую учётную запись или контейнер для недоверенного
кода.

Политика применяется и к `SHELL`, и к `SHELL$`; она не восстанавливается из
снимка и не даёт изоляции файловой системы. Для более строгого контроля
запускайте интерпретатор в отдельном процессе с ограниченными правами.

## Тестирование

Быстрая встроенная проверка:

```bash
python mutabasic.py --self-test
```

Регрессионный набор:

```bash
python -m unittest discover -s tests -v
```

Набор проверяет API, выражения и исполнение программ, parsing-фасад, реестры
расширений и shell-политику. При добавлении новой инструкции или функции
добавляйте отдельный позитивный и негативный сценарий.

## Дорожная карта

- выделить компиляцию выражений и исполнения инструкций в отдельные модули;
- заменить текущую parsing-абстракцию формальной грамматикой и AST;
- расширить policy до allow-list путей и безопасного запуска без `shell=True`;
- наращивать совместимость BASIC и покрытие тестами.

При сообщении о проблеме приложите:

1. Версию Python и ОС.
2. Версию MutaBasic.
3. Минимальный листинг и команду запуска.
4. Ожидаемый и фактический результат.
5. Текст ошибки; при необходимости — трассировку `--trace`.

## Безопасность

MutaBasic **не является песочницей**. Даже без `--allow-shell` программа может изменять и удалять доступные ей файлы. Не запускайте недоверенный код с важными данными и повышенными правами. Для изоляции используйте отдельную среду с ограниченными правами.

JSON не использует pickle, однако загруженный проект или снимок содержит исполняемую BASIC-программу. Разрешение оболочки нельзя считать единственной границей безопасности.

## Лицензия

Проект распространяется по лицензии BSD 3-Clause. Полный текст находится в
файле [`LICENSE`](LICENSE).
