from mcdreforged.api.all import Info, PluginServerInterface

from prime_backup_approval.commands import Commands
from prime_backup_approval.config import load_config
from prime_backup_approval.runtime import Runtime

runtime: Runtime | None = None


def on_load(server: PluginServerInterface, old: object) -> None:
	global runtime
	previous = getattr(old, 'runtime', None)
	config = load_config(server)
	instance = Runtime(server, config)
	if previous is not None:
		for player in previous.online_snapshot():
			instance.player_seen(player)
	try:
		Commands(instance).register()
		instance.start()
	except BaseException:
		instance.close()
		raise
	runtime = instance


def on_unload(server: PluginServerInterface) -> None:
	if runtime is not None:
		runtime.close()
		# Keep the closed instance available to on_load(old) for online player recovery.


def on_player_joined(server: PluginServerInterface, player: str, info: Info) -> None:
	if runtime is not None:
		runtime.player_joined(player)


def on_player_left(server: PluginServerInterface, player: str) -> None:
	if runtime is not None:
		runtime.player_left(player)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
	if runtime is not None:
		runtime.server_stopped()
