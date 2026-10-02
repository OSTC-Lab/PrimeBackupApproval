import re
from datetime import datetime

from mcdreforged.api.all import CommandSource, RAction, RColor, RText, RTextBase, RTextList

_MARKDOWN_CHARACTERS = '\\`*_~|[]()<>#+-'


def escape_markdown(text: str) -> str:
	return ''.join('\\' + char if char in _MARKDOWN_CHARACTERS else char for char in text)


def unescape_markdown(text: str) -> str:
	return re.sub(r'\\([' + re.escape(_MARKDOWN_CHARACTERS) + r'])', r'\1', text)


def message(text: str | RTextBase, color: RColor = RColor.white) -> RTextBase:
	content = RText(text, color) if isinstance(text, str) else text
	return RTextList(RText('[PBA] ', RColor.dark_aqua).h('PrimeBackup 回档审批'), content)


def reply(source: CommandSource, text: str | RTextBase, color: RColor = RColor.white) -> None:
	source.reply(message(text, color))


def command(text: str, *, label: str | None = None, run: bool = False) -> RTextBase:
	return RText(label or text, RColor.aqua).c(
		RAction.run_command if run else RAction.suggest_command, text,
	).h(f'{"点击执行" if run else "点击预填"}：{text}')


def button(label: str, text: str, *, run: bool = False, danger: bool = False) -> RTextBase:
	return command(text, label=f'[{label}]', run=run).set_color(RColor.red if danger else RColor.aqua)


def approval_link(approval_id: int, prefix: str) -> RTextBase:
	return command(f'{prefix} show {approval_id}', label=f'#{approval_id}', run=True)


def time_text(timestamp: int) -> str:
	return datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def duration_text(seconds: int) -> str:
	minutes, seconds = divmod(max(0, seconds), 60)
	hours, minutes = divmod(minutes, 60)
	parts: list[str] = []
	if hours:
		parts.append(f'{hours} 小时')
	if minutes:
		parts.append(f'{minutes} 分钟')
	if seconds or not parts:
		parts.append(f'{seconds} 秒')
	return ' '.join(parts)
