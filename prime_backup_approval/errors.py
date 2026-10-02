from dataclasses import dataclass
from typing import Literal


class OperationError(Exception):
	"""An expected failure that can be explained to the player."""
	pass


class ProcessingStopped(Exception):
	pass


@dataclass
class WriteOutcomeUncertain(Exception):
	operation: Literal['create', 'data', 'cancel']
	error: Exception

	def __post_init__(self) -> None:
		super().__init__(f'Approval {self.operation} result could not be confirmed')
