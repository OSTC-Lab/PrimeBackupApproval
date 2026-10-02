from dataclasses import dataclass
from typing import Callable

from mcdreforged.api.all import CommandSource


@dataclass(frozen=True)
class PlayerRequest:
	source: CommandSource
	player: str


@dataclass(frozen=True)
class ApplyRequest(PlayerRequest):
	backup_raw: str
	reason: str


@dataclass(frozen=True)
class ShowRequest(PlayerRequest):
	approval_id: int


@dataclass(frozen=True)
class CancelRequest(PlayerRequest):
	approval_id: int


@dataclass(frozen=True)
class ListRequest(PlayerRequest):
	page: int = 1


@dataclass(frozen=True)
class StatusRequest(PlayerRequest):
	pass


@dataclass(frozen=True)
class BackRequest(PlayerRequest):
	backup_raw: str | None
	execute: Callable[[int], None]


@dataclass(frozen=True)
class NotifyRequest:
	player: str


WorkRequest = ApplyRequest | ShowRequest | CancelRequest | ListRequest | StatusRequest | BackRequest | NotifyRequest
