import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import httpx
from pydantic import ValidationError

from prime_backup_approval.approval_center_sdk import ApprovalCenterAPIError


@dataclass(frozen=True)
class FailureContext:
	phase: str
	player: str | None = None
	approval_id: int | None = None
	backup: str | None = None
	method: str | None = None
	path: str | None = None
	offset: int | None = None

	def describe(self) -> str:
		parts = [f'phase={self.phase}']
		if self.player is not None:
			parts.append(f'player={self.player}')
		if self.approval_id is not None:
			parts.append(f'approval_id={self.approval_id}')
		if self.backup is not None:
			parts.append(f'backup={self.backup}')
		if self.method is not None:
			parts.append(f'method={self.method}')
		if self.path is not None:
			parts.append(f'path={self.path}')
		if self.offset is not None:
			parts.append(f'offset={self.offset}')
		return ' '.join(parts)


@contextmanager
def failure_context(context: FailureContext) -> Iterator[None]:
	try:
		yield
	except Exception as error:
		# Keep the original exception type for the runtime's recovery policy.
		error.add_note(context.describe())
		raise


def format_failure(error: Exception, context: FailureContext, *, secret: str = '') -> str:
	lines = [f'Approval processing failed: {context.describe()}']
	current: BaseException | None = error
	while current is not None:
		lines.extend(getattr(current, '__notes__', ()))
		lines.append(f'{type(current).__name__}:')
		if isinstance(current, ValidationError):
			lines.append(f'  model={current.title}')
			for item in current.errors(include_input=False, include_context=False, include_url=False):
				location = '.'.join(str(part) for part in item['loc']) or '<model>'
				lines.append(f"  field={location} type={item['type']} message={item['msg']}")
		elif isinstance(current, ApprovalCenterAPIError):
			lines.append(f'  status={current.status_code} code={current.code} message={current.message}')
		elif isinstance(current, httpx.HTTPStatusError):
			lines.append(f'  status={current.response.status_code}')
		else:
			lines.append(f'  {current}')
		if isinstance(current, httpx.HTTPError):
			try:
				request = current.request
			except RuntimeError:
				pass
			else:
				lines.append(f'  method={request.method} path={request.url.path}')
		lines.append('Traceback (most recent call last):')
		for frame in traceback.extract_tb(current.__traceback__):
			# File, line and function identify the failure without logging locals or payloads.
			lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
		current = current.__cause__
		if current is not None:
			lines.append('Caused by:')
	text = '\n'.join(lines)
	if secret:
		text = text.replace(secret, '<redacted>')
	return text
