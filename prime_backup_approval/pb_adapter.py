import functools
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, cast

from mcdreforged.api.all import AbstractNode, CommandContext, CommandSource, Literal, PlayerCommandSource, PluginServerInterface, RColor
from mcdreforged.command.builder.nodes.basic import RUNS_CALLBACK, _Requirement

from prime_backup_approval.models import BackupDescription, BackupTarget
from prime_backup_approval.requests import BackRequest
from prime_backup_approval.text import reply


class PermissionSettings(Protocol):
	def get(self, literal: str) -> int: ...


class PBCommandSettings(Protocol):
	prefix: str
	permission: PermissionSettings


class PBConfig(Protocol):
	command: PBCommandSettings


class PBCommandManager(Protocol):
	config: PBConfig
	def cmd_back(self, source: CommandSource, context: CommandContext) -> None: ...


class PBEntry(Protocol):
	init_ok: bool | None
	command_manager: PBCommandManager | None


@dataclass(frozen=True)
class CallbackPatch:
	node: AbstractNode
	original: RUNS_CALLBACK
	wrapper: Callable[[CommandSource, CommandContext], None]


@dataclass(frozen=True)
class HookInstallation:
	manager: PBCommandManager
	back_node: Literal
	original_requirement: _Requirement
	replacement_requirement: _Requirement
	callbacks: tuple[CallbackPatch, ...]
	prefix: str
	back_permission: int


class PBAdapterError(Exception):
	pass


class PBTargetError(PBAdapterError):
	"""A normal user-facing failure while selecting a backup or checking permission."""
	pass


class PBAdapter:
	def __init__(self, server: PluginServerInterface, request_permission: int, submit: Callable[[BackRequest], None], companion_prefix: str = '!!pba'):
		self.server = server
		self.request_permission = request_permission
		self.submit = submit
		self.companion_prefix = companion_prefix
		self._installation: HookInstallation | None = None
		self._lock = threading.RLock()
		self._closed = False

	@property
	def available(self) -> bool:
		with self._lock:
			return self._installation is not None and not self._closed

	@property
	def prefix(self) -> str:
		with self._lock:
			return self._installation.prefix if self._installation else '!!pb'

	def refresh(self) -> bool:
		with self._lock:
			if self._closed:
				return False
			entry = cast(PBEntry | None, self.server.get_plugin_instance('prime_backup'))
			manager = entry.command_manager if entry is not None and entry.init_ok is True else None
			if self._installation is not None and self._installation.manager is manager:
				return True
			self._restore()
			if manager is None:
				return False
			self._install(manager)
			return True

	def _install(self, manager: PBCommandManager) -> None:
		if manager.config.command.prefix == self.companion_prefix:
			raise PBAdapterError('Companion command prefix must differ from PB prefix')
		root = getattr(manager, '_CommandManager__root_node', None)
		if not isinstance(root, Literal):
			raise PBAdapterError('PB root node is incompatible')
		permissions = manager.config.command.permission
		if self.request_permission < max(permissions.get('root'), permissions.get('confirm')):
			raise PBAdapterError('request_permission must satisfy PB root and confirm permissions')
		back_nodes = [node for node in root.get_children() if isinstance(node, Literal) and 'back' in node.literals]
		if len(back_nodes) != 1:
			raise PBAdapterError('Expected exactly one PB back node')
		back = back_nodes[0]
		arguments = [node for node in back.get_children() if getattr(node, 'get_name', lambda: None)() == 'backup_id']
		if len(arguments) != 1 or len(back._requirements) != 1:
			raise PBAdapterError('PB back arguments or requirements are incompatible')
		requirement = back._requirements[0]
		checker = requirement.requirement
		level = permissions.get('back')
		if not (isinstance(checker, functools.partial) and checker.func is CommandSource.has_permission and checker.keywords == {'level': level}):
			raise PBAdapterError('PB back permission checker is incompatible')
		patches: list[CallbackPatch] = []
		for node in (back, arguments[0]):
			original = node._callback
			if original != manager.cmd_back:
				raise PBAdapterError('PB back callback is incompatible')
			patches.append(CallbackPatch(node, original, self._make_wrapper(manager, original, level)))

		def check_permission(source: CommandSource, context: CommandContext) -> bool:
			if checker(source):
				return True
			if not isinstance(source, PlayerCommandSource) or not source.has_permission(self.request_permission):
				return False
			if any(option in context.command_remaining.split() for option in ('--confirm', '--fail-soft', '--no-verify')):
				reply(source, '注意：只有默认回档命令才可申请审批。', RColor.yellow)
				# The original PB failure callback supplies the native permission error.
				return False
			return True

		replacement = _Requirement(check_permission, requirement.failure_message_getter)
		# Both execution callbacks are guarded before the entry permission is widened.
		for patch in patches:
			patch.node.runs(patch.wrapper)
		back._requirements[0] = replacement
		self._installation = HookInstallation(manager, back, requirement, replacement, tuple(patches), manager.config.command.prefix, level)
		self.server.logger.info('PB back hook installed')

	def _make_wrapper(self, manager: PBCommandManager, original: RUNS_CALLBACK, level: int) -> Callable[[CommandSource, CommandContext], None]:
		callback = cast(Callable[[CommandSource, CommandContext], None], original)

		def wrapper(source: CommandSource, context: CommandContext) -> None:
			if source.has_permission(level):
				callback(source, context)
				return
			if not isinstance(source, PlayerCommandSource) or not source.has_permission(self.request_permission):
				self.server.execute_command(context.command, source)
				return
			if any(context.get(key, 0) > 0 for key in ('confirm', 'fail_soft', 'no_verify')):
				self.server.execute_command(context.command, source)
				return
			copied = context.copy()

			def execute(backup_id: int) -> None:
				with self._lock:
					if self._closed or self._installation is None or self._installation.manager is not manager:
						raise PBAdapterError('PB instance changed before execution')
					entry = cast(PBEntry | None, self.server.get_plugin_instance('prime_backup'))
					if entry is None or entry.init_ok is not True or entry.command_manager is not manager:
						raise PBAdapterError('PB is no longer ready')
					if not source.has_permission(self.request_permission):
						raise PBTargetError('权限不足。')
					copied['backup_id'] = str(backup_id)
					callback(source, copied)

			self.submit(BackRequest(source, source.player, context.get('backup_id'), execute))

		return wrapper

	def _restore(self) -> None:
		installation = self._installation
		if installation is None:
			return
		for index, requirement in enumerate(installation.back_node._requirements):
			if requirement is installation.replacement_requirement:
				installation.back_node._requirements[index] = installation.original_requirement
		for patch in installation.callbacks:
			if patch.node._callback is patch.wrapper:
				patch.node.runs(patch.original)
		self._installation = None
		self.server.logger.info('PB back hook restored')

	def close(self) -> None:
		with self._lock:
			self._closed = True
			self._restore()

	def resolve(self, raw: str | None) -> BackupDescription:
		if not self.refresh():
			raise PBAdapterError('PB is initializing or disabled')
		from prime_backup.action.get_backup_action import GetBackupAction
		from prime_backup.action.list_backup_action import ListBackupAction
		from prime_backup.types.backup_filter import BackupFilter
		from prime_backup.utils.backup_id_parser import BackupIdParser
		from prime_backup.exceptions import BackupNotFound

		if raw is None:
			backups = ListBackupAction(backup_filter=BackupFilter().requires_non_temporary_backup(), limit=1).run()
			if not backups:
				raise PBTargetError('没有可用的备份。')
			backup = backups[0]
		else:
			try:
				backup_id = BackupIdParser(allow_db_access=True).parse(raw)
			except BackupIdParser.OffsetBackupNotFound:
				raise PBTargetError('没有找到匹配的备份。') from None
			except ValueError:
				raise PBTargetError('备份参数无效，请使用备份编号、latest 或 ~N。') from None
			try:
				backup = GetBackupAction(backup_id).run()
			except BackupNotFound:
				raise PBTargetError(f'备份 #{backup_id} 不存在。') from None
		return BackupDescription(
			BackupTarget(backup_id=backup.id, fileset_id_base=backup.fileset_id_base, fileset_id_delta=backup.fileset_id_delta),
			backup.date_str, backup.comment,
		)

	def online_players(self) -> set[str]:
		entry = self.server.get_plugin_instance('prime_backup')
		counter = getattr(entry, 'online_player_counter', None)
		if counter is None:
			return set()
		with counter.data_lock:
			return {record.name for record in counter.player_records.get_records() if record.online}
