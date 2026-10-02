import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol, cast

from pydantic import ValidationError

from prime_backup_approval.approval_center_sdk import (
	ApprovalCenterAPIError, ApprovalCenterClient, ApprovalContent, ApprovalInfo, ApprovalStatus,
	CancelApprovalRequest, CreateApprovalRequest, DisplayField, GetApprovalRequest, ListApprovalsRequest, SetApprovalDataRequest,
)
from prime_backup_approval.config import Config
from prime_backup_approval.models import (
	BackupDescription, GrantPolicy, ManagedApproval, ProcessingState, RestoreApprovalData, TerminalStatus, reference_key,
)
from prime_backup_approval.requests import (
	ApplyRequest, BackRequest, CancelRequest, ListRequest, NotifyRequest, ShowRequest, StatusRequest, WorkRequest,
)

STATUS_TEXT = {
	ApprovalStatus.PENDING: '待审批', ApprovalStatus.APPROVED: '已同意',
	ApprovalStatus.REJECTED: '已拒绝', ApprovalStatus.TIMED_OUT: '已超时', ApprovalStatus.CANCELLED: '已取消',
}


class BackupProvider(Protocol):
	@property
	def prefix(self) -> str: ...
	def resolve(self, raw: str | None) -> BackupDescription: ...


class OperationError(Exception):
	pass


class ProcessingStopped(Exception):
	pass


@dataclass(frozen=True)
class TrackedApproval:
	record: ManagedApproval
	next_poll: float


class ApprovalManager:
	"""All methods run on the single runtime worker."""

	def __init__(
		self, config: Config, client: ApprovalCenterClient, backups: BackupProvider,
		logger: logging.Logger, notify: Callable[[str, str], bool], *,
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
		try:
			data = RestoreApprovalData.model_validate_json(approval.data)
			if approval.reference_key != reference_key(data.player_name):
				raise ValueError('Player reference mismatch')
			if approval.status == ApprovalStatus.APPROVED and approval.decision is None:
				raise ValueError('Missing decision')
		except (ValidationError, ValueError):
			self.logger.warning(f'Invalid approval data approval_id={approval.approval_id}')
			return None
		return ManagedApproval(approval, data)

	def _list(self, player: str | None = None) -> list[ManagedApproval]:
		items: list[ManagedApproval] = []
		offset = 0
		while True:
			self._check_running()
			page = self.client.list_approvals(ListApprovalsRequest(
				reference_key=None if player is None else reference_key(player), limit=100, offset=offset,
			))
			for approval in page.items:
				record = self._decode(approval)
				if record is not None and (player is None or record.data.player_name == player):
					items.append(record)
			if len(page.items) < page.limit:
				return items
			offset += page.limit

	def _owned(self, player: str, approval_id: int) -> ManagedApproval:
		self._check_running()
		record = self._decode(self.client.get_approval(GetApprovalRequest(approval_id=approval_id)))
		if record is None or record.data.player_name != player:
			raise OperationError('该单据属于其他玩家或数据格式无法识别。')
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
		data = record.data.model_copy(update={'processing': processing})
		data = RestoreApprovalData.model_validate_json(data.model_dump_json())
		payload = data.model_dump_json().encode('utf8')
		result = self.client.set_approval_data(SetApprovalDataRequest(approval_id=record.approval.approval_id, data=payload))
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
					self.tracked.pop(approval_id, None)
					self.logger.info(f'Approval removed approval_id={approval_id}')
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
					record = self._owned(request.player, tracked.record.approval.approval_id)
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
			request.source.reply(self.describe(record))
			request.source.reply(f'{record.approval.content.title}\n{record.approval.content.description}')
			for field in record.approval.content.fields:
				request.source.reply(f'{field.name}：{field.value}')
		elif isinstance(request, CancelRequest):
			self._cancel(request)
		elif isinstance(request, ListRequest):
			records = self._list(request.player)
			start = (request.page - 1) * 10
			request.source.reply(f'审批单列表，第 {request.page} 页，共 {len(records)} 单：')
			for record in records[start:start + 10]:
				request.source.reply(self.describe(record))
		elif isinstance(request, StatusRequest):
			records = [record for record in self._list(request.player) if record.is_active(int(self.clock()))]
			request.source.reply(f'活动单据 {len(records)}/{self.config.approval.max_active_per_player}：')
			for record in records:
				request.source.reply(self.describe(record))
		else:
			raise TypeError('Unknown work request')

	def _apply(self, request: ApplyRequest) -> None:
		if not request.reason.strip():
			raise OperationError('请填写申请理由。')
		backup = self.backups.resolve(request.backup_raw)
		records = self._list(request.player)
		for record in records:
			self._track(record)
		active = [record for record in records if record.is_active(int(self.clock()))]
		if len(active) >= self.config.approval.max_active_per_player:
			raise OperationError(f'活动单据已达到 {self.config.approval.max_active_per_player} 单，请等待结束或取消待审批单。')
		duplicates = [record.approval.approval_id for record in active if record.data.backup == backup.target]
		if duplicates:
			request.source.reply(f'该目标已有活动申请 {duplicates}，继续创建本次申请。')
		data = RestoreApprovalData(
			player_name=request.player, backup=backup.target,
			grant=GrantPolicy(validity_seconds=self.config.approval.grant_validity_seconds, max_uses=self.config.approval.max_uses),
		)
		content = ApprovalContent(title='PrimeBackup 回档申请', description=request.reason, fields=(
			DisplayField(name='玩家', value=request.player, inline=True),
			DisplayField(name='服务器', value=self.config.server_name, inline=True),
			DisplayField(name='操作', value='prime_backup / back（默认回档，保留确认、严格恢复、内容校验）'),
			DisplayField(name='目标备份', value=f'#{backup.target.backup_id}\n{backup.date}\n{backup.comment}'),
			DisplayField(name='批准使用规则', value=f'决定后 {data.grant.validity_seconds} 秒内，最多 {data.grant.max_uses} 次放行'),
		))
		self._check_running()
		result = self.client.create_approval(CreateApprovalRequest(
			content=content, expires_at=int(self.clock()) + self.config.approval.decision_timeout_seconds,
			reference_key=reference_key(request.player), data=data.model_dump_json().encode('utf8'),
		))
		approval = ApprovalInfo(
			**result.model_dump(), client_id=self.config.approval_center.client_id, content=content,
			decision=None, data=data.model_dump_json().encode('utf8'), data_version=0,
		)
		self._track(ManagedApproval(approval, data))
		request.source.reply(f'已创建审批单 #{result.approval_id}，目标备份 #{backup.target.backup_id}，审批截止 {result.expires_at}。')
		request.source.reply(f'通过后使用：{self.backups.prefix} back {backup.target.backup_id}')
		self.logger.info(f'Approval created approval_id={result.approval_id} player={request.player} backup_id={backup.target.backup_id}')

	def _back(self, request: BackRequest) -> None:
		backup = self.backups.resolve(request.backup_raw)
		records = self._list(request.player)
		for record in records:
			self._track(record)
		candidates = [record for record in records if record.data.backup == backup.target and record.approval.status == ApprovalStatus.APPROVED and record.is_active(int(self.clock()))]
		if not candidates:
			request.source.reply(f'目标备份 #{backup.target.backup_id} 当前需要审批。申请命令：{self.config.command.prefix} apply {backup.target.backup_id} <理由>')
			for record in records:
				if record.data.backup.backup_id == backup.target.backup_id:
					request.source.reply(self.describe(record))
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
		request.execute(record.data.backup.backup_id)
		request.source.reply(f'审批单 #{record.approval.approval_id} 已用于本次放行，剩余 {record.remaining_uses} 次。')
		self.logger.info(f'Approval used approval_id={record.approval.approval_id} player={request.player} backup_id={record.data.backup.backup_id} use_count={record.data.processing.use_count}')

	def _cancel(self, request: CancelRequest) -> None:
		self._owned(request.player, request.approval_id)
		self._check_running()
		try:
			result = self.client.cancel_approval(CancelApprovalRequest(approval_id=request.approval_id))
		except ApprovalCenterAPIError as error:
			if error.code != 'approval_not_pending':
				raise
			record = self._owned(request.player, request.approval_id)
			request.source.reply(f'审批单已进入终态：{STATUS_TEXT[record.approval.status]}。')
			self._track(record)
			return
		cancelled = self._decode(result)
		if cancelled is None:
			raise OperationError('中心返回的单据格式无法识别。')
		record = cancelled
		self._track(record)
		request.source.reply(self.describe(record))
		self._store(record, record.data.processing.model_copy(update={'notified_status': 'cancelled'}))
		self.logger.info(f'Approval cancelled approval_id={request.approval_id} player={request.player}')

	def describe(self, record: ManagedApproval) -> str:
		approval = record.approval
		text = f'#{approval.approval_id} → 备份 #{record.data.backup.backup_id}：{STATUS_TEXT[approval.status]}'
		if approval.status == ApprovalStatus.PENDING:
			return f'{text}，审批截止 {approval.expires_at}'
		if approval.status == ApprovalStatus.APPROVED:
			remaining_seconds = max(0, record.grant_deadline - int(self.clock()))
			return f'{text}，剩余 {record.remaining_uses}/{record.data.grant.max_uses} 次，剩余 {remaining_seconds} 秒'
		return text
