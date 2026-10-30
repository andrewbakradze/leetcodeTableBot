"""Консольный интерфейс на Rich."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .config import BotError, Config, column_letter
from .leetcode import Problem


class TerminalUI:
    def __init__(self) -> None:
        self.console = Console(theme=Theme({"accent": "bold cyan", "good": "green", "muted": "dim", "warn": "yellow"}))

    def banner(self) -> None:
        self.console.print(Panel(
            "[bold white]LeetCode[/bold white] [accent]→[/accent] [bold white]Google Sheets[/bold white]\n"
            "[muted]Задача решена. Опыт сохранён.[/muted]",
            border_style="cyan", padding=(1, 3), title="[accent]STUDY JOURNAL[/accent]",
        ))

    def ask(self, label: str, default: str | None = None, choices: list[str] | None = None) -> str:
        return Prompt.ask(f"[accent]{label}[/accent]", console=self.console, default=default, choices=choices).strip()

    def ask_secret(self, label: str) -> str:
        return Prompt.ask(f"[accent]{label}[/accent]", console=self.console, password=True).strip()

    def error(self, message: str) -> None:
        self.console.print(Panel(Text(message), title="[red]Не получилось[/red]", border_style="red"))

    def warning(self, message: str) -> None:
        self.console.print(Text(message, style="yellow"))

    def success(self, message: str) -> None:
        self.console.print(Text(f"✓ {message}", style="green"))

    def problem(self, problem: Problem) -> None:
        text = Text(f"#{problem.number}  {problem.title}", style="bold white")
        if problem.difficulty:
            color = {"Easy": "green", "Medium": "yellow", "Hard": "red"}.get(problem.difficulty, "white")
            text.append(f"   {problem.difficulty}", style=color)
        text.append(f"\n{problem.url}", style=f"cyan link {problem.url}")
        self.console.print(Panel(text, border_style="cyan", padding=(1, 2)))

    def notes(self) -> str | None:
        self.console.print(
            "[bold]Что вызвало сложности?[/bold]\n"
            "[muted]Можно несколько строк. Точка на отдельной строке — сохранить.\n"
            "/skip — без заметки, /cancel — отменить задачу.[/muted]"
        )
        lines: list[str] = []
        length = 0
        while True:
            line = self.console.input("[cyan]  │ [/cyan]")
            command = line.strip().lower()
            if command == "/cancel":
                return None
            if command == "/skip":
                return ""
            if command == ".":
                return "\n".join(lines).strip()
            # Google ограничивает содержимое одной ячейки 50 000 символами.
            added = len(line) + (1 if lines else 0)
            if length + added > 50000:
                self.warning("Лимит заметки — 50 000 символов. Завершите ввод точкой или /cancel.")
                continue
            lines.append(line)
            length += added

    def mapping(self, config: Config, headers: list[str]) -> None:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Данные")
        table.add_column("Столбец")
        table.add_column("Заголовок")
        labels = {"number": "Номер задачи", "title": "Название", "notes": "Мои сложности", "url": "Ссылка"}
        for field, index in config.columns.items():
            table.add_row(labels[field], column_letter(index), headers[index - 1] if index <= len(headers) else "—")
        self.console.print(table)

    def help(self) -> None:
        self.console.print(Panel(
            "[accent]228[/accent]       — добавить решённую задачу\n"
            "[accent]/help[/accent]     — команды\n"
            "[accent]/config[/accent]   — показать настройки\n"
            "[accent]/draft[/accent]    — последняя локальная копия заметки\n"
            "[accent]/exit[/accent]     — выход",
            title="[bold]Команды[/bold]", border_style="bright_black",
        ))

    def show_draft(self, problem: Problem, notes: str) -> None:
        self.problem(problem)
        self.console.print(Panel(Text(notes or "Без заметки"), title="Локальная копия"))

    def demo(self) -> None:
        self.banner()
        self.console.print("[muted]Демонстрация интерфейса • без сети и записи в Google[/muted]\n")
        self.console.print("[accent]Номер задачи[/accent]: 228")
        self.problem(Problem(228, "Summary Ranges", "summary-ranges", "Easy"))
        self.console.print("[bold]Что вызвало сложности?[/bold]")
        self.console.print(Text("  │ Граничные случаи: пустой массив и переполнение int.MAX.\n  │ .", style="cyan"))
        self.success("Так будет выглядеть результат сохранения задачи №228.")
        self.help()
