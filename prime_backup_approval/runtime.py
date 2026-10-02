import queue
import threading
import time

import httpx
from mcdreforged.api.all import PluginServerInterface

from prime_backup_approval.approval_center_sdk import ApprovalCenterAPIError, ApprovalCenterClient
from prime_backup_approval.approval_manager import ApprovalManager, OperationError, ProcessingStopped
from prime_backup_approval.config import Config
from prime_backup_approval.pb_adapter import PBAdapter, PBAdapterError
from prime_backup_approval.requests import ApplyRequest, NotifyRequest, PlayerRequest, WorkRequest


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
				request.source.reply('审批扩展已停用或正在初始化／恢复，请稍后重试。')
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

	def _notify(self, player: str, text: str) -> bool:
		with self._online_lock:
			if player not in self._online or self._stop.is_set() or not self.server.is_server_running():
				return False
			self.server.tell(player, text)
			return True

	def status_text(self) -> str:
		if not self.config.approval_center.configured:
			return '审批扩展：等待填写 client_id 和 client_secret，填写后重载插件。'
		return f'审批扩展：{"就绪" if self.ready else "停用／恢复中"}；PB hook：{"已安装" if self.adapter.available else "等待就绪"}。'

	def _log_error(self, error: Exception) -> None:
		if isinstance(error, ApprovalCenterAPIError):
			description = f'{type(error).__name__} status={error.status_code} code={error.code}'
		elif isinstance(error, PBAdapterError):
			description = f'PBAdapterError: {error}'
		else:
			description = type(error).__name__
		if description != self._last_error:
			self.server.logger.warning(f'Approval processing failed: {description}')
			self._last_error = description

	def _maintenance(self) -> None:
		entry = self.server.get_plugin_instance('prime_backup')
		identity = getattr(entry, 'command_manager', None)
		if identity is not self._pb_identity:
			self.ready = False
			self._pb_identity = identity
			self._next_recovery = 0
		if not self.adapter.refresh():
			self.ready = False
			return
		if not self.ready:
			if time.monotonic() < self._next_recovery:
				return
			with self._online_lock:
				self._online.update(self.adapter.online_players())
			self.manager.recover()
			self.ready = True
			self._last_error = None
			self.server.logger.info('Approval center available; processing ready')
		self.manager.poll()

	def _report_failure(self, request: WorkRequest, error: Exception) -> None:
		if isinstance(request, PlayerRequest):
			if isinstance(error, OperationError):
				message = str(error)
			elif isinstance(error, ApprovalCenterAPIError):
				message = f'审批中心返回错误：{error.code}。请使用 show 或 list 查询当前结果。'
			elif isinstance(error, httpx.RequestError) and isinstance(request, ApplyRequest):
				message = '申请提交结果待确认，请查询已有单据或重新按正常流程申请。'
			else:
				message = '本次处理失败，请查询当前单据并稍后重试；详细信息已写入日志。'
			request.source.reply(message)
		if not isinstance(error, OperationError):
			self._log_error(error)
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
						self._log_error(error)
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
						request.source.reply('审批扩展正在恢复，请稍后重试。')
					continue
				try:
					if isinstance(request, PlayerRequest):
						if not request.source.has_permission(self.config.approval.request_permission):
							raise OperationError('当前权限低于申请权限要求。')
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
