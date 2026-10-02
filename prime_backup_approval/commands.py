from mcdreforged.api.all import CommandContext, CommandSource, GreedyText, Integer, Literal, PlayerCommandSource, Text

from prime_backup_approval.requests import ApplyRequest, CancelRequest, ListRequest, ShowRequest, StatusRequest
from prime_backup_approval.runtime import Runtime


class Commands:
	def __init__(self, runtime: Runtime):
		self.runtime = runtime
		self.prefix = runtime.config.command.prefix

	def register(self) -> None:
		root = Literal(self.prefix).runs(self.help)
		root.then(Literal('help').runs(self.help))
		root.then(Literal('status').runs(self.status))
		def permitted(source: CommandSource) -> bool:
			return isinstance(source, PlayerCommandSource) and source.has_permission(self.runtime.config.approval.request_permission)

		def player_command(name: str) -> Literal:
			return Literal(name).requires(permitted, lambda: '该命令供具有申请资格的玩家使用。')

		root.then(player_command('apply').then(Text('backup').then(GreedyText('reason').runs(self.apply))))
		root.then(player_command('show').then(Integer('approval_id').at_min(1).runs(self.show)))
		root.then(player_command('cancel').then(Integer('approval_id').at_min(1).runs(self.cancel)))
		listing = player_command('list').runs(self.list)
		listing.then(Integer('page').at_min(1).runs(self.list))
		root.then(listing)
		self.runtime.server.register_command(root)
		self.runtime.server.register_help_message(self.prefix, 'PrimeBackup 回档审批')

	def help(self, source: CommandSource) -> None:
		source.reply('\n'.join((
			'PrimeBackupApproval：',
			f'{self.prefix} apply <备份> <理由>：申请默认回档',
			f'{self.prefix} show <单号>：查看自己的审批单',
			f'{self.prefix} list [页码]：列出自己的审批单',
			f'{self.prefix} status：运行状态与活动审批',
			f'{self.prefix} cancel <单号>：取消自己的待审批单',
		)))

	def apply(self, source: CommandSource, context: CommandContext) -> None:
		assert isinstance(source, PlayerCommandSource)
		self.runtime.submit(ApplyRequest(source, source.player, context['backup'], context['reason']))

	def show(self, source: CommandSource, context: CommandContext) -> None:
		assert isinstance(source, PlayerCommandSource)
		self.runtime.submit(ShowRequest(source, source.player, context['approval_id']))

	def cancel(self, source: CommandSource, context: CommandContext) -> None:
		assert isinstance(source, PlayerCommandSource)
		self.runtime.submit(CancelRequest(source, source.player, context['approval_id']))

	def list(self, source: CommandSource, context: CommandContext) -> None:
		assert isinstance(source, PlayerCommandSource)
		self.runtime.submit(ListRequest(source, source.player, context.get('page', 1)))

	def status(self, source: CommandSource) -> None:
		source.reply(self.runtime.status_text())
		if isinstance(source, PlayerCommandSource) and source.has_permission(self.runtime.config.approval.request_permission):
			self.runtime.submit(StatusRequest(source, source.player))
