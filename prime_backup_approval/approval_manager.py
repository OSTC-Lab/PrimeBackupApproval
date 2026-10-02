import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol, cast

import httpx
from mcdreforged.api.all import RColor, RText, RTextBase, RTextList
from pydantic import ValidationError

from prime_backup_approval.approval_center_sdk import (
	ApprovalCenterAPIError, ApprovalCenterClient, ApprovalContent, ApprovalInfo, ApprovalStatus,
	CancelApprovalRequest, CreateApprovalRequest, DisplayField, GetApprovalRequest, ListApprovalsRequest, SetApprovalDataRequest,
)
from prime_backup_approval.config import Config
from prime_backup_approval.diagnostics import FailureContext, failure_context, format_failure
from prime_backup_approval.errors import OperationError, ProcessingStopped, WriteOutcomeUncertain
from prime_backup_approval.models import (
	BackupDescription, GrantPolicy, ManagedApproval, ProcessingState, RestoreApprovalData, TerminalStatus, reference_key,
)
from prime_backup_approval.requests import (
	ApplyRequest, BackRequest, CancelRequest, ListRequest, NotifyRequest, ShowRequest, StatusRequest, WorkRequest,
)
from prime_backup_approval.text import approval_link, button, command, duration_text, escape_markdown, reply, time_text, unescape_markdown

STATUS_TEXT = {
	ApprovalStatus.PENDING: '待审批', ApprovalStatus.APPROVED: '已同意',
	ApprovalStatus.REJECTED: '已拒绝', ApprovalStatus.TIMED_OUT: '已超时', ApprovalStatus.CANCELLED: '已取消',
}
STATUS_COLOR = {
	ApprovalStatus.PENDING: RColor.yellow, ApprovalStatus.APPROVED: RColor.green,
	ApprovalStatus.REJECTED: RColor.red, ApprovalStatus.TIMED_OUT: RColor.gray, ApprovalStatus.CANCELLED: RColor.gray,
}


class BackupProvider(Protocol):
	@property
	def prefix(self) -> str: ...
	def resolve(self, raw: str | None) -> BackupDescription: ...


@dataclass(frozen=True)
class TrackedApproval:
	record: ManagedApproval
	next_poll: float


class ApprovalManager:
	"""All methods run on the single runtime worker."""

	def __init__(
		self, config: Config, client: ApprovalCenterClient, backups: BackupProvider,
		logger: logging.Logger, notify: Callable[[str, RTextBase], bool], *,
		clock: Callable[[], float] = time.time, stopping: Callable[[], bool] = lambda: False,
	):
		self.config = config
		self.client = client
		self.backups = backups
		self.logger = logger
		self.notify = notify
		self.clock = clock
		self.stopping = stopping
		self.tracked: dict[int, TrackedApproval] = {}

	def _check_running(self) -> None:
		if self.stopping():
			raise ProcessingStopped()

	def _decode(self, approval: ApprovalInfo) -> ManagedApproval | None:
		if approval.client_id != self.config.approval_center.client_id or not (approval.reference_key or '').startswith('pb.restore:'):
			return None
		with failure_context(FailureContext('decode.approval_data', approval_id=approval.approval_id)):
			data = RestoreApprovalData.model_validate_json(approval.data)
			if approval.reference_key != reference_key(data.player_name):
				raise ValueError('Player reference mismatch')
			if approval.status == ApprovalStatus.APPROVED and approval.decision is None:
				raise ValueError('Missing decision')
		return ManagedApproval(approval, data)

	def _list(self, player: str | None = None) -> list[ManagedApproval]:
		items: list[ManagedApproval] = []
		offset = 0
		while True:
			self._check_running()
			with failure_context(FailureContext('list.approvals', player=player, method='GET', path='/api/v1/approval', offset=offset)):
				page = self.client.list_approvals(ListApprovalsRequest(
					reference_key=None if player is None else reference_key(player), limit=100, offset=offset,
				))
			for approval in page.items:
				try:
					record = self._decode(approval)
				except (ValidationError, ValueError) as error:
					self.logger.warning(format_failure(
						error, FailureContext('list.approvals', approval_id=approval.approval_id),
						secret=self.config.approval_center.client_secret.get_secret_value(),
					))
					continue
				if record is not None and (player is None or record.data.player_name == player):
					items.append(record)
			if len(page.items) < page.limit:
				return items
			offset += page.limit

	def _owned(self, player: str, approval_id: int) -> ManagedApproval:
		self._check_running()
		with failure_context(FailureContext('get.approval', player=player, approval_id=approval_id, method='GET', path=f'/api/v1/approval/{approval_id}')):
			try:
				approval = self.client.get_approval(GetApprovalRequest(approval_id=approval_id))
			except ApprovalCenterAPIError as error:
				if error.code == 'approval_not_found':
					if self.tracked.pop(approval_id, None) is not None:
						self.logger.info(f'Approval removed approval_id={approval_id}')
				raise
			if approval.client_id != self.config.approval_center.client_id or approval.reference_key != reference_key(player):
				raise OperationError('权限不足。')
			record = self._decode(approval)
			if record is None:
				raise ValueError('Invalid restore approval data')
		return record

	def _track(self, record: ManagedApproval) -> None:
		now = self.clock()
		approval_id = record.approval.approval_id
		needs_notification = (
			record.approval.status != ApprovalStatus.PENDING
			and record.data.processing.notified_status != record.approval.status.value
		)
		if now < record.tracking_deadline and (
			record.approval.status == ApprovalStatus.PENDING or record.is_active(int(now)) or needs_notification
		):
			self.tracked[approval_id] = TrackedApproval(record, now + self.config.polling.interval_seconds)
		else:
			self.tracked.pop(approval_id, None)

	def _store(self, record: ManagedApproval, processing: ProcessingState) -> ManagedApproval:
		self._check_running()
		with failure_context(FailureContext('store.approval_data', player=record.data.player_name, approval_id=record.approval.approval_id)):
			data = record.data.model_copy(update={'processing': processing})
			data = RestoreApprovalData.model_validate_json(data.model_dump_json())
			payload = data.model_dump_json().encode('utf8')
			with failure_context(FailureContext('put.approval_data', method='PUT', path=f'/api/v1/approval-data/{record.approval.approval_id}')):
				request = SetApprovalDataRequest(approval_id=record.approval.approval_id, data=payload)
				try:
					result = self.client.set_approval_data(request)
				except (httpx.RequestError, ValidationError) as error:
					self.tracked.pop(record.approval.approval_id, None)
					raise WriteOutcomeUncertain('data', error) from error
				except httpx.HTTPStatusError:
					self.tracked.pop(record.approval.approval_id, None)
					raise
		updated = ManagedApproval(record.approval.model_copy(update={
			'data': payload, 'data_version': result.version, 'updated_at': result.updated_at,
		}), data)
		self._track(updated)
		return updated

	def recover(self) -> None:
		# Publish the recovered set only after the complete scan succeeds.
		records = self._list()
		self.tracked.clear()
		for record in records:
			self._track(record)
		for tracked in list(self.tracked.values()):
			self._notify_result(tracked.record)
		self.logger.info(f'Approval recovery completed active_records={len(self.tracked)}')

	def _notify_result(self, record: ManagedApproval) -> None:
		status = record.approval.status
		if status == ApprovalStatus.PENDING or self.clock() >= record.tracking_deadline:
			return
		if record.data.processing.notified_status == status.value:
			return
		self._check_running()
		with failure_context(FailureContext('notify.result', player=record.data.player_name, approval_id=record.approval.approval_id)):
			if self.notify(record.data.player_name, self.describe(record)):
				processing = record.data.processing.model_copy(update={'notified_status': cast(TerminalStatus, status.value)})
				self._store(record, processing)
				self.logger.info(f'Approval result delivered approval_id={record.approval.approval_id} player={record.data.player_name} status={status.value}')

	def poll(self) -> None:
		for approval_id, tracked in list(self.tracked.items()):
			self._check_running()
			now = self.clock()
			if now >= tracked.record.tracking_deadline:
				self.tracked.pop(approval_id, None)
				continue
			if tracked.record.approval.status != ApprovalStatus.PENDING:
				if not tracked.record.is_active(int(now)) and tracked.record.data.processing.notified_status == tracked.record.approval.status.value:
					self.tracked.pop(approval_id, None)
				else:
					self._notify_result(tracked.record)
				continue
			if now < tracked.next_poll:
				continue
			try:
				record = self._owned(tracked.record.data.player_name, approval_id)
			except ApprovalCenterAPIError as error:
				if error.code == 'approval_not_found':
					continue
				raise
			self._track(record)
			if record.approval.status != tracked.record.approval.status:
				self.logger.info(f'Approval state changed approval_id={approval_id} status={record.approval.status.value}')
			self._notify_result(record)

	def handle(self, request: WorkRequest) -> None:
		self._check_running()
		if isinstance(request, NotifyRequest):
			for tracked in list(self.tracked.values()):
				if tracked.record.data.player_name == request.player:
					try:
						record = self._owned(request.player, tracked.record.approval.approval_id)
					except ApprovalCenterAPIError as error:
						if error.code == 'approval_not_found':
							continue
						raise
					self._track(record)
					self._notify_result(record)
			return
		if isinstance(request, ApplyRequest):
			self._apply(request)
		elif isinstance(request, BackRequest):
			self._back(request)
		elif isinstance(request, ShowRequest):
			record = self._owned(request.player, request.approval_id)
			self._track(record)
			reply(request.source, self.describe(record, details=True))
			reply(request.source, RTextList(RText('申请理由：', RColor.gray), unescape_markdown(record.approval.content.description)))
			if record.approval.decision is not None:
				decision = record.approval.decision
				reply(request.source, RTextList(
					RText('决定时间：', RColor.gray), time_text(decision.decided_at),
					RText(' · 审批人：', RColor.gray), decision.reviewer_name or '系统',
				))
			for field in record.approval.content.fields:
				reply(request.source, RTextList(RText(f'{field.name}：', RColor.gray), unescape_markdown(field.value)))
		elif isinstance(request, CancelRequest):
			self._cancel(request)
		elif isinstance(request, ListRequest):
			records = self._list(request.player)
			start = (request.page - 1) * 10
			pages = max(1, (len(records) + 9) // 10)
			reply(request.source, RText(f'审批单 · 第 {request.page}/{pages} 页 · 共 {len(records)} 单', RColor.gold))
			for record in records[start:start + 10]:
				reply(request.source, self.describe(record))
			if not records[start:start + 10]:
				reply(request.source, '本页暂无审批单。', RColor.gray)
			navigation = RTextList()
			if request.page > 1:
				navigation.append(button('上一页', f'{self.config.command.prefix} list {request.page - 1}', run=True), ' ')
			if request.page < pages:
				navigation.append(button('下一页', f'{self.config.command.prefix} list {request.page + 1}', run=True))
			if navigation.to_plain_text():
				reply(request.source, navigation)
		elif isinstance(request, StatusRequest):
			records = [record for record in self._list(request.player) if record.is_active(int(self.clock()))]
			reply(request.source, RText(f'有效审批 {len(records)}/{self.config.approval.max_active_per_player}', RColor.gold))
			for record in records:
				reply(request.source, self.describe(record))
			if not records:
				reply(request.source, '当前暂无有效审批。', RColor.gray)
		else:
			raise TypeError('Unknown work request')

	def _apply(self, request: ApplyRequest) -> None:
		if not request.reason.strip():
			raise OperationError('请填写申请理由。')
		with failure_context(FailureContext('pb.resolve', player=request.player, backup=request.backup_raw)):
			backup = self.backups.resolve(request.backup_raw)
		records = self._list(request.player)
		for record in records:
			self._track(record)
		active = [record for record in records if record.is_active(int(self.clock()))]
		if len(active) >= self.config.approval.max_active_per_player:
			raise OperationError(f'有效审批已达到 {self.config.approval.max_active_per_player} 单，请等待审批结束或取消待审批单。')
		duplicates = [record.approval.approval_id for record in active if record.data.backup == backup.target]
		if duplicates:
			links = RTextList()
			for approval_id in duplicates:
				links.append(approval_link(approval_id, self.config.command.prefix), ' ')
			reply(request.source, RTextList(RText('注意：已存在回档至该备份的有效审批 ', RColor.yellow), links))
		data = RestoreApprovalData(
			player_name=request.player, backup=backup.target,
			grant=GrantPolicy(validity_seconds=self.config.approval.grant_validity_seconds, max_uses=self.config.approval.max_uses),
		)
		with failure_context(FailureContext('create.content', player=request.player, backup=str(backup.target.backup_id))):
			try:
				content = ApprovalContent(title='PrimeBackup 回档申请', description=escape_markdown(request.reason), fields=(
					DisplayField(name='玩家', value=escape_markdown(request.player), inline=True),
					DisplayField(name='服务器', value=escape_markdown(self.config.server_name), inline=True),
					DisplayField(name='操作', value=escape_markdown('prime_backup / back')),
					DisplayField(name='目标备份', value=escape_markdown(' · '.join(
						' '.join(part.split()) for part in (f'#{backup.target.backup_id}', backup.date, backup.comment) if part.strip()
					))),
					DisplayField(name='批准使用规则', value=escape_markdown(f'决定后 {duration_text(data.grant.validity_seconds)}内；执行次数上限：{data.grant.max_uses} 次')),
				))
			except ValidationError:
				raise OperationError('申请内容不符合展示要求，请调整申请理由或备份备注。') from None
		self._check_running()
		with failure_context(FailureContext('create.approval', player=request.player, backup=str(backup.target.backup_id), method='POST', path='/api/v1/approval')):
			create_request = CreateApprovalRequest(
				content=content, expires_at=int(self.clock()) + self.config.approval.decision_timeout_seconds,
				reference_key=reference_key(request.player), data=data.model_dump_json().encode('utf8'),
			)
			try:
				result = self.client.create_approval(create_request)
			except (httpx.RequestError, ValidationError) as error:
				raise WriteOutcomeUncertain('create', error) from error
		approval = ApprovalInfo(
			**result.model_dump(), client_id=self.config.approval_center.client_id, content=content,
			decision=None, data=data.model_dump_json().encode('utf8'), data_version=0,
		)
		self._track(ManagedApproval(approval, data))
		reply(request.source, RTextList(
			RText('已创建审批单 ', RColor.green), approval_link(result.approval_id, self.config.command.prefix),
			' · 目标备份 ', RText(f'#{backup.target.backup_id}', RColor.gold),
		))
		reply(request.source, RTextList(
			RText('审批截止：', RColor.gray), time_text(result.expires_at),
			f'（{duration_text(max(0, result.expires_at - int(self.clock())))}后）',
		))
		reply(request.source, RTextList(
			'通过后回档：', command(f'{self.backups.prefix} back {backup.target.backup_id}'),
			' ', button('取消申请', f'{self.config.command.prefix} cancel {result.approval_id}', danger=True),
		))
		self.logger.info(f'Approval created approval_id={result.approval_id} player={request.player} backup_id={backup.target.backup_id}')

	def _back(self, request: BackRequest) -> None:
		with failure_context(FailureContext('pb.resolve', player=request.player, backup=request.backup_raw)):
			backup = self.backups.resolve(request.backup_raw)
		records = self._list(request.player)
		for record in records:
			self._track(record)
		candidates = [record for record in records if record.data.backup == backup.target and record.approval.status == ApprovalStatus.APPROVED and record.is_active(int(self.clock()))]
		if not candidates:
			reply(request.source, RTextList(
				'回档至备份 ', RText(f'#{backup.target.backup_id}', RColor.gold), RText(' 需要审批。', RColor.yellow),
			))
			reply(request.source, RTextList('申请命令：', command(
				f'{self.config.command.prefix} apply {backup.target.backup_id} ',
				label=f'{self.config.command.prefix} apply {backup.target.backup_id} <理由>',
			).h('点击预填申请命令，然后填写理由')))
			for record in records:
				if record.data.backup.backup_id == backup.target.backup_id:
					reply(request.source, self.describe(record))
			return
		record = min(candidates, key=lambda item: (item.grant_deadline, item.approval.approval_id))
		# Recheck the selected record and PB target immediately before consuming it.
		record = self._owned(request.player, record.approval.approval_id)
		if record.approval.status != ApprovalStatus.APPROVED or not record.is_active(int(self.clock())) or record.data.backup != self.backups.resolve(str(backup.target.backup_id)).target:
			raise OperationError('批准已失效或备份目标已发生变化，请重新申请。')
		processing = record.data.processing.model_copy(update={
			'use_count': record.data.processing.use_count + 1, 'last_used_at': int(self.clock()),
		})
		record = self._store(record, processing)
		self._check_running()
		if self.clock() >= record.grant_deadline or record.data.backup != self.backups.resolve(str(record.data.backup.backup_id)).target:
			raise OperationError('使用窗口已结束或备份目标已发生变化，本次调用结束。')
		with failure_context(FailureContext('pb.execute', player=request.player, approval_id=record.approval.approval_id, backup=str(record.data.backup.backup_id))):
			request.execute(record.data.backup.backup_id)
		feedback = RTextList(
			RText('已通过审批单 ', RColor.green), approval_link(record.approval.approval_id, self.config.command.prefix),
			RText(' 放行本次回档操作。', RColor.green),
		)
		if record.remaining_uses <= 3:
			feedback.append(RText(f' 剩余执行次数：{record.remaining_uses} 次。', RColor.gold))
		reply(request.source, feedback)
		self.logger.info(f'Approval used approval_id={record.approval.approval_id} player={request.player} backup_id={record.data.backup.backup_id} use_count={record.data.processing.use_count}')

	def _cancel(self, request: CancelRequest) -> None:
		self._owned(request.player, request.approval_id)
		self._check_running()
		try:
			with failure_context(FailureContext('cancel.approval', player=request.player, approval_id=request.approval_id, method='POST', path=f'/api/v1/approval/{request.approval_id}/cancel')):
				cancel_request = CancelApprovalRequest(approval_id=request.approval_id)
				try:
					result = self.client.cancel_approval(cancel_request)
				except (httpx.RequestError, ValidationError) as error:
					self.tracked.pop(request.approval_id, None)
					raise WriteOutcomeUncertain('cancel', error) from error
		except ApprovalCenterAPIError as error:
			if error.code != 'approval_not_pending':
				raise
			record = self._owned(request.player, request.approval_id)
			reply(request.source, self.describe(record))
			self._track(record)
			return
		cancelled = self._decode(result)
		if cancelled is None:
			raise ValueError(f'Invalid cancelled approval response approval_id={request.approval_id}')
		record = cancelled
		self._track(record)
		reply(request.source, self.describe(record))
		self._store(record, record.data.processing.model_copy(update={'notified_status': 'cancelled'}))
		self.logger.info(f'Approval cancelled approval_id={request.approval_id} player={request.player}')

	def describe(self, record: ManagedApproval, *, details: bool = False) -> RTextBase:
		approval = record.approval
		text = RTextList(
			approval_link(approval.approval_id, self.config.command.prefix),
			RText(' → 备份 ', RColor.gray), RText(f'#{record.data.backup.backup_id}', RColor.gold),
			' · ', RText(STATUS_TEXT[approval.status], STATUS_COLOR[approval.status]),
		)
		if approval.status == ApprovalStatus.PENDING:
			text.append(RText(' · 审批截止 ', RColor.gray), RText(time_text(approval.expires_at)).h(f'Unix 时间戳：{approval.expires_at}'))
			text.append(' ', button('取消', f'{self.config.command.prefix} cancel {approval.approval_id}', danger=True))
		if approval.status == ApprovalStatus.APPROVED:
			remaining_seconds = max(0, record.grant_deadline - int(self.clock()))
			if details or record.remaining_uses <= 3:
				text.append(RText(f' · 执行次数：剩余 {record.remaining_uses}/{record.data.grant.max_uses} 次', RColor.gold))
			if remaining_seconds == 0:
				text.append(RText(' · 使用期已结束', RColor.gray))
			elif record.remaining_uses == 0:
				text.append(RText(' · 执行次数已用完', RColor.gray))
			else:
				text.append(RText(f' · 剩余 {duration_text(remaining_seconds)}', RColor.green).h(f'使用截止：{time_text(record.grant_deadline)}'))
				text.append(' ', button('回档', f'{self.backups.prefix} back {record.data.backup.backup_id}'))
		return text
