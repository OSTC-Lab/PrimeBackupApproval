from mcdreforged.api.all import CommandContext, CommandSource, GreedyText, Integer, Literal, PlayerCommandSource, RColor, RText, RTextList, Text

from prime_backup_approval.requests import ApplyRequest, CancelRequest, ListRequest, ShowRequest, StatusRequest
from prime_backup_approval.runtime import Runtime
from prime_backup_approval.text import command, message, reply


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
			return Literal(name).requires(permitted, lambda: message('该命令供具有申请资格的玩家使用。', RColor.red))

		root.then(player_command('apply').then(Text('backup').then(GreedyText('reason').runs(self.apply))))
		root.then(player_command('show').then(Integer('approval_id').at_min(1).runs(self.show)))
		root.then(player_command('cancel').then(Integer('approval_id').at_min(1).runs(self.cancel)))
		listing = player_command('list').runs(self.list)
		listing.then(Integer('page').at_min(1).runs(self.list))
		root.then(listing)
		self.runtime.server.register_command(root)
		self.runtime.server.register_help_message(self.prefix, 'PrimeBackup 回档审批')

	def help(self, source: CommandSource) -> None:
		reply(source, RText('PrimeBackup 回档审批', RColor.gold))
		reply(source, RTextList(command(f'{self.prefix} apply ', label=f'{self.prefix} apply <备份> <理由>'), RText('：申请回档', RColor.gray)))
		reply(source, RTextList(command(f'{self.prefix} show ', label=f'{self.prefix} show <单号>'), RText('：查看自己的审批单', RColor.gray)))
		reply(source, RTextList(command(f'{self.prefix} list', label=f'{self.prefix} list [页码]', run=True), RText('：列出自己的审批单', RColor.gray)))
		reply(source, RTextList(command(f'{self.prefix} status', run=True), RText('：运行状态与活动审批', RColor.gray)))
		reply(source, RTextList(command(f'{self.prefix} cancel ', label=f'{self.prefix} cancel <单号>'), RText('：取消自己的待审批单', RColor.gray)))

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
		reply(source, self.runtime.status_text())
		if isinstance(source, PlayerCommandSource) and source.has_permission(self.runtime.config.approval.request_permission):
			self.runtime.submit(StatusRequest(source, source.player))
