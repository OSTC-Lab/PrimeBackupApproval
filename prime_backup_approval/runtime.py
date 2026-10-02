import queue
import threading
import time

import httpx
from mcdreforged.api.all import PluginServerInterface, RColor, RText, RTextBase, RTextList

from prime_backup_approval.approval_center_sdk import ApprovalCenterAPIError, ApprovalCenterClient
from prime_backup_approval.approval_manager import ApprovalManager, OperationError, ProcessingStopped
from prime_backup_approval.config import Config
from prime_backup_approval.diagnostics import FailureContext, failure_context, format_failure
from prime_backup_approval.pb_adapter import PBAdapter, PBTargetError
from prime_backup_approval.requests import ApplyRequest, BackRequest, CancelRequest, NotifyRequest, PlayerRequest, ShowRequest, WorkRequest
from prime_backup_approval.text import button, message, reply


class Runtime:
	def __init__(self, server: PluginServerInterface, config: Config, *, client: ApprovalCenterClient | None = None):
		self.server = server
		self.config = config
		self._stop = threading.Event()
		self._closed = False
		self._jobs: queue.Queue[WorkRequest | None] = queue.Queue()
		self._online_lock = threading.Lock()
		self._online: set[str] = set()
		self.ready = False
		self._pb_identity: object | None = None
		self._last_error: str | None = None
		self._next_recovery = 0.0
		self._next_maintenance = 0.0
		self.adapter = PBAdapter(server, config.approval.request_permission, self.submit, config.command.prefix)
		center = config.approval_center
		self.client = client or ApprovalCenterClient(center.base_url, center.client_id, center.client_secret.get_secret_value(), timeout=center.request_timeout_seconds)
		self.manager = ApprovalManager(config, self.client, self.adapter, server.logger, self._notify, stopping=self._stop.is_set)
		self.thread = threading.Thread(target=self._run, name='prime_backup_approval-worker', daemon=True)

	def start(self) -> None:
		if self.config.enabled and self.config.approval_center.configured:
			self.thread.start()
			self.server.logger.info('PrimeBackupApproval started')
		elif self.config.enabled:
			self.server.logger.info('请填写 config/prime_backup_approval/config.json 中的 client_id 和 client_secret，然后重载插件。')

	def submit(self, request: WorkRequest) -> None:
		first_seen = False
		if isinstance(request, PlayerRequest):
			first_seen = self.player_seen(request.player)
		if not self.config.enabled or self._stop.is_set() or not self.ready:
			if isinstance(request, PlayerRequest):
				reply(request.source, self.status_text())
			return
		if first_seen and isinstance(request, PlayerRequest):
			self._jobs.put(NotifyRequest(request.player))
		self._jobs.put(request)

	def player_seen(self, player: str) -> bool:
		with self._online_lock:
			first_seen = player not in self._online
			self._online.add(player)
			return first_seen

	def player_joined(self, player: str) -> None:
		self.player_seen(player)
		if self.config.enabled and self.config.approval_center.configured and not self._stop.is_set():
			self._jobs.put(NotifyRequest(player))

	def player_left(self, player: str) -> None:
		with self._online_lock:
			self._online.discard(player)

	def server_stopped(self) -> None:
		with self._online_lock:
			self._online.clear()

	def online_snapshot(self) -> set[str]:
		with self._online_lock:
			return set(self._online)

	def _notify(self, player: str, text: RTextBase) -> bool:
		with self._online_lock:
			if player not in self._online or self._stop.is_set() or not self.server.is_server_running():
				return False
			self.server.tell(player, message(text))
			return True

	def status_text(self) -> RTextBase:
		if not self.config.enabled or self._stop.is_set():
			return RText('审批服务：已停用', RColor.gray)
		if not self.config.approval_center.configured:
			return RText('审批服务：等待配置，请填写 client_id 和 client_secret，填写后重载插件。', RColor.yellow)
		state = '就绪' if self.ready else ('恢复中' if self._last_error is not None else '初始化中')
		return RTextList(
			'审批服务：', RText(state, RColor.green if self.ready else RColor.yellow),
			' · PB 连接：', RText('已就绪' if self.adapter.available else '等待就绪', RColor.green if self.adapter.available else RColor.yellow),
		)

	def _log_error(self, error: Exception, context: FailureContext) -> None:
		description = format_failure(error, context, secret=self.config.approval_center.client_secret.get_secret_value())
		if description != self._last_error:
			self.server.logger.warning(description)
			self._last_error = description

	def _maintenance(self) -> None:
		entry = self.server.get_plugin_instance('prime_backup')
		identity = getattr(entry, 'command_manager', None)
		if identity is not self._pb_identity:
			self.ready = False
			self._pb_identity = identity
			self._next_recovery = 0
		with failure_context(FailureContext('pb.refresh')):
			available = self.adapter.refresh()
		if not available:
			self.ready = False
			return
		if not self.ready:
			if time.monotonic() < self._next_recovery:
				return
			with failure_context(FailureContext('recovery')):
				with self._online_lock:
					self._online.update(self.adapter.online_players())
				self.manager.recover()
			self.ready = True
			self._last_error = None
			self.server.logger.info('Approval center available; processing ready')
		with failure_context(FailureContext('poll')):
			self.manager.poll()

	def _report_failure(self, request: WorkRequest, error: Exception) -> None:
		feedback: str | None = None
		if isinstance(error, (OperationError, PBTargetError)):
			feedback = str(error)
		elif isinstance(error, ApprovalCenterAPIError) and error.code == 'approval_not_found':
			feedback = '审批单不存在。'
		elif isinstance(error, httpx.RequestError):
			creating = isinstance(request, ApplyRequest) and any(
				note.startswith('phase=create.approval ') for note in getattr(error, '__notes__', ())
			)
			feedback = '申请提交结果待确认，请查询已有单据或重新按正常流程申请。' if creating else '暂时无法与审批中心通信，请稍后重试。'
		if isinstance(request, PlayerRequest):
			if feedback is not None:
				reply(request.source, RTextList(
					RText(feedback, RColor.red), ' ', button('查看单据', f'{self.config.command.prefix} list', run=True),
				))
		if not isinstance(error, (OperationError, PBTargetError)):
			self._log_error(error, FailureContext(
				phase=type(request).__name__, player=request.player,
				approval_id=request.approval_id if isinstance(request, (ShowRequest, CancelRequest)) else None,
				backup=request.backup_raw if isinstance(request, (ApplyRequest, BackRequest)) else None,
			))
		if isinstance(error, httpx.HTTPError) and not (
			isinstance(error, ApprovalCenterAPIError) and error.status_code in (404, 409, 422)
		):
			self.ready = False
			self._next_recovery = time.monotonic() + self.config.polling.retry_interval_seconds

	def _run(self) -> None:
		try:
			while not self._stop.is_set():
				now = time.monotonic()
				if now >= self._next_maintenance:
					try:
						self._maintenance()
					except ProcessingStopped:
						break
					except Exception as error:
						self.ready = False
						self._log_error(error, FailureContext('maintenance'))
						self._next_recovery = time.monotonic() + self.config.polling.retry_interval_seconds
						self._next_maintenance = self._next_recovery
					else:
						self._next_maintenance = time.monotonic() + self.config.polling.interval_seconds
				try:
					request = self._jobs.get(timeout=max(0.01, min(0.5, self._next_maintenance - time.monotonic())))
				except queue.Empty:
					continue
				if request is None or self._stop.is_set():
					break
				if not self.ready:
					if isinstance(request, PlayerRequest):
						reply(request.source, self.status_text())
					continue
				try:
					if isinstance(request, PlayerRequest):
						if not request.source.has_permission(self.config.approval.request_permission):
							raise OperationError('权限不足。')
					self.manager.handle(request)
				except ProcessingStopped:
					break
				except Exception as error:
					self._report_failure(request, error)
		finally:
			self.ready = False

	def close(self) -> None:
		if self._closed:
			return
		self._closed = True
		self.ready = False
		self._stop.set()
		self._jobs.put(None)
		if self.thread.is_alive():
			self.thread.join()
		self.adapter.close()
		self.client.close()
		self.server.logger.info('PrimeBackupApproval stopped')
