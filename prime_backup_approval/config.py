from typing import Annotated, Self, cast

from mcdreforged.api.all import PluginServerInterface
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_serializer, model_validator

PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0)]


class ConfigModel(BaseModel):
	model_config = ConfigDict(extra='forbid', frozen=True)


class CenterConfig(ConfigModel):
	base_url: str = 'http://127.0.0.1:8731'
	client_id: str = ''
	client_secret: SecretStr = SecretStr('')
	request_timeout_seconds: PositiveSeconds = 2

	@property
	def configured(self) -> bool:
		return bool(self.client_id.strip() and self.client_secret.get_secret_value())

	@field_serializer('client_secret', when_used='json')
	def serialize_secret(self, value: SecretStr) -> str:
		# MCDR persists filled-in defaults; preserve the configured credential.
		return value.get_secret_value()

	@model_validator(mode='after')
	def validate_connection(self) -> Self:
		from urllib.parse import urlsplit
		url = urlsplit(self.base_url)
		if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.query or url.fragment:
			raise ValueError('base_url must be an HTTP(S) URL without credentials, query or fragment')
		return self


class ApprovalConfig(ConfigModel):
	request_permission: int = Field(default=1, ge=0, le=4)
	decision_timeout_seconds: PositiveInt = 10800
	grant_validity_seconds: PositiveInt = 1800
	max_uses: PositiveInt = 10
	max_active_per_player: PositiveInt = 20


class PollingConfig(ConfigModel):
	interval_seconds: PositiveSeconds = 1
	retry_interval_seconds: PositiveSeconds = 10


class CommandConfig(ConfigModel):
	prefix: str = Field(default='!!pba', min_length=1, pattern=r'^\S+$')


class Config(ConfigModel):
	enabled: bool = True
	server_name: str = Field(default='Minecraft Server', min_length=1, max_length=256)
	approval_center: CenterConfig = Field(default_factory=CenterConfig)
	approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
	polling: PollingConfig = Field(default_factory=PollingConfig)
	command: CommandConfig = Field(default_factory=CommandConfig)

	@model_validator(mode='after')
	def validate_name(self) -> Self:
		if not self.server_name.strip():
			raise ValueError('server_name must not be blank')
		return self


class ConfigError(Exception):
	pass


def load_config(server: PluginServerInterface) -> Config:
	try:
		return cast(Config, server.load_config_simple(
			'config.json', target_class=Config, failure_policy='raise', echo_in_console=False,
		))
	except ValidationError as error:
		# Pydantic's normal exception text includes input values, including secrets.
		locations = ', '.join('.'.join(str(part) for part in item['loc']) for item in error.errors(include_input=False))
		raise ConfigError(f'Invalid configuration fields: {locations}') from None
	except OSError:
		raise ConfigError('Cannot read config.json in the plugin data directory') from None
	except ValueError:
		raise ConfigError('Invalid config.json syntax') from None
