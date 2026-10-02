from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prime_backup_approval.approval_center_sdk import ApprovalInfo, ApprovalStatus

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
UnixTime = NonNegativeInt
TerminalStatus = Literal['approved', 'rejected', 'timed_out', 'cancelled']


class StoredModel(BaseModel):
	model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class BackupTarget(StoredModel):
	backup_id: PositiveInt
	fileset_id_base: PositiveInt
	fileset_id_delta: PositiveInt


class GrantPolicy(StoredModel):
	validity_seconds: PositiveInt
	max_uses: PositiveInt


class ProcessingState(StoredModel):
	use_count: NonNegativeInt = 0
	last_used_at: UnixTime | None = None
	notified_status: TerminalStatus | None = None


class RestoreApprovalData(StoredModel):
	schema_version: Literal[1] = 1
	plugin_id: Literal['prime_backup_approval'] = 'prime_backup_approval'
	operation: Literal['back'] = 'back'
	player_name: str = Field(min_length=1)
	backup: BackupTarget
	grant: GrantPolicy
	processing: ProcessingState = Field(default_factory=ProcessingState)

	@model_validator(mode='after')
	def validate_usage(self) -> Self:
		if not self.player_name.strip():
			raise ValueError('player_name must not be blank')
		if self.processing.use_count > self.grant.max_uses:
			raise ValueError('use_count exceeds max_uses')
		if (self.processing.use_count == 0) != (self.processing.last_used_at is None):
			raise ValueError('use_count and last_used_at must agree')
		return self


def reference_key(player: str) -> str:
	return f'pb.restore:{player}'


@dataclass(frozen=True)
class ManagedApproval:
	approval: ApprovalInfo
	data: RestoreApprovalData

	@property
	def grant_deadline(self) -> int:
		decision = self.approval.decision
		if decision is None:
			raise ValueError('Approval decision is missing')
		return decision.decided_at + self.data.grant.validity_seconds

	@property
	def tracking_deadline(self) -> int:
		return self.approval.expires_at + self.data.grant.validity_seconds

	@property
	def remaining_uses(self) -> int:
		return self.data.grant.max_uses - self.data.processing.use_count

	def is_active(self, now: int) -> bool:
		if self.approval.status == ApprovalStatus.PENDING:
			return now < self.approval.expires_at
		return (
			self.approval.status == ApprovalStatus.APPROVED
			and now < self.grant_deadline and self.remaining_uses > 0
		)


@dataclass(frozen=True)
class BackupDescription:
	target: BackupTarget
	date: str
	comment: str
