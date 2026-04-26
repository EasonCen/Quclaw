"""Configuration management."""

import logging
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from utils.config_validators import coerce_id_list, coerce_optional_id

ConfigChangeCallback = Callable[[], None]


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    model_config = ConfigDict(populate_by_name=True)

    provider: str
    model: str
    api_key: str
    base_url: str | None = Field(default=None, alias="base_url")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2333, gt=0)
    
    @field_validator("base_url")
    @classmethod
    def base_url_must_be_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("base_url must be a valid URL")
        return v


class TelegramConfig(BaseModel):
    """Telegram platform configuration."""

    enabled: bool = True
    bot_token: str
    allowed_user_ids: list[str] = Field(default_factory=list)

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def allowed_user_ids_must_be_strings(cls, value: Any) -> list[str]:
        return coerce_id_list(value)

class DiscordConfig(BaseModel):
    """Discord platform configuration."""

    enabled: bool = True
    bot_token: str
    channel_id: str | None = None
    allowed_user_ids: list[str] = Field(default_factory=list)

    @field_validator("channel_id", mode="before")
    @classmethod
    def channel_id_must_be_string(cls, value: Any) -> str | None:
        return coerce_optional_id(value)

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def allowed_user_ids_must_be_strings(cls, value: Any) -> list[str]:
        return coerce_id_list(value)


class TavilySearchConfig(BaseModel):
    """Configuration for Tavily web search provider."""

    provider: Literal["tavily"] = "tavily"
    api_key: str
    search_depth: Literal["basic", "advanced"] = "basic"
    topic: Literal["general", "news", "finance"] = "general"
    max_results: int = Field(default=5, ge=0, le=20)
    chunks_per_source: int = Field(default=3, ge=1, le=3)
    include_answer: bool | Literal["basic", "advanced"] = False
    include_raw_content: bool | Literal["markdown", "text"] = False
    include_images: bool = False
    include_image_descriptions: bool = False
    include_favicon: bool = False
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    auto_parameters: bool = False

class TavilyWebReadConfig(BaseModel):
    """Configuration for Tavily web read provider."""

    provider: Literal["tavily"] = "tavily"
    api_key: str
    extract_depth: Literal["basic", "advanced"] = "basic"
    format: Literal["markdown", "text"] = "markdown"
    include_images: bool = False
    include_favicon: bool = False
    chunks_per_source: int = Field(default=3, ge=1, le=5)
    timeout: float | None = Field(default=None, ge=1.0, le=60.0)

class SourceSessionConfig(BaseModel):
    """Session affinity configuration for a source."""
    session_id: str


class ContextConfig(BaseModel):
    """Context window and compaction configuration."""

    token_threshold: int = Field(default=200000, gt=0)


class ChannelConfig(BaseModel):
    """Channel configuration/"""

    enabled: bool = False
    telegram: TelegramConfig | None = None
    discord: DiscordConfig | None = None


class WebSocketConfig(BaseModel):
    """WebSocket gateway configuration."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=6948, ge=1, le=65535)
    path: str = "/ws"
    auth_token: str | None = None

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("websocket.path must start with '/'")
        return value


class HeartbeatConfig(BaseModel):
    """Heartbeat background worker configuration."""

    interval_minutes: int = Field(default=0, ge=0)
    agent: str | None = None


class Config(BaseModel):
    """Main configuration for step 03."""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agents_path: Path = Field(default=Path("agents"))
    memories_path: Path = Field(default=Path("memories"))
    skills_path: Path = Field(default=Path("skills"))
    crons_path: Path = Field(default=Path("crons"))
    logging_path: Path = Field(default=Path(".logs"))
    websearch: TavilySearchConfig | None = None
    webread: TavilyWebReadConfig | None = None 
    event_path: Path = Field(default=Path(".event"))
    history_path: Path = Field(default=Path(".history"))
    context: ContextConfig = Field(default_factory=ContextConfig)
    channels: ChannelConfig = Field(default_factory=ChannelConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    sources: dict[str, SourceSessionConfig] = Field(default_factory=dict)
    routing: dict = Field(default_factory=lambda: {"bindings": []})
    default_delivery_source: str | None = None


    @model_validator(mode="after")
    def resolve_paths(self) -> "Config":
        """Resolve relative paths to absolute using workspace."""
        for field_name in (
            "agents_path",
            "skills_path",
            "crons_path",
            "memories_path",
            "history_path",
            "logging_path",
            "event_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)
        return self

    def template_vars(self) -> dict[str, str]:
        """Return config-backed values available to workspace templates."""
        field_names = (
            "workspace",
            "agents_path",
            "memories_path",
            "skills_path",
            "crons_path",
            "history_path",
            "logging_path",
            "event_path",
            "default_agent",
        )
        values = {
            field_name: self._format_template_value(getattr(self, field_name))
            for field_name in field_names
        }
        return values

    @staticmethod
    def _format_template_value(value: Any) -> str:
        """Format template values for markdown and tool arguments."""
        if isinstance(value, Path):
            return value.resolve().as_posix()
        return str(value)

    @classmethod
    def load(cls, workspace_dir: Path) -> "Config":
        """Load configuration from workspace directory."""
        config_data = cls._load_merged_configs(workspace_dir)
        config_data["workspace"] = workspace_dir
        return cls.model_validate(config_data)

    @classmethod
    def _load_merged_configs(cls, workspace_dir: Path) -> dict[str, Any]:
        """Load config from YAML file."""
        config_data: dict[str, Any] = {}

        user_config = workspace_dir / "config.user.yaml"
        runtime_config = workspace_dir / "config.runtime.yaml"

        if user_config.exists():
            with open(user_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        if runtime_config.exists():
            with open(runtime_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        return config_data
    
    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge override dict into base dict."""
        result = base.copy()

        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value

        return result
    

    def _set_nested(self, obj: dict, key: str, value: Any) -> None:
        """Set a nested value in a dict using dot notation."""
        keys = key.split(".")
        for k in keys[:-1]:
            if k not in obj or not isinstance(obj[k],dict):
                obj[k] = {}
            obj = obj[k]
        obj[keys[-1]] = value
        
    def _set_config_value(self, config_path: Path, key: str, value: Any) -> None:
        """Update a config value in a YAML file."""
        # Load existing or start fresh
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        if isinstance(value, BaseModel):
            value = value.model_dump()

        # Update the key (supports nested via dot notation)
        self._set_nested(data, key, value)

        # Write back
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

    def _set_mapping_value(
        self,
        config_path: Path,
        mapping_key: str,
        item_key: str,
        value: Any,
    ) -> None:
        """Update one key inside a top-level mapping without dot-path parsing."""
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        mapping = data.get(mapping_key)
        if not isinstance(mapping, dict):
            mapping = {}
            data[mapping_key] = mapping

        if isinstance(value, BaseModel):
            value = value.model_dump()

        mapping[item_key] = value

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

    def _remove_mapping_value(
        self,
        config_path: Path,
        mapping_key: str,
        item_key: str,
    ) -> bool:
        """Remove one key inside a top-level mapping without dot-path parsing."""
        if not config_path.exists():
            return False

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        mapping = data.get(mapping_key)
        if not isinstance(mapping, dict) or item_key not in mapping:
            return False

        del mapping[item_key]

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        return True

    def set_user(self, key: str, value: Any) -> None:
        """Update a config value in config.user.yaml."""
        self._set_config_value(self.workspace / "config.user.yaml", key, value)

    def set_runtime(self, key: str, value: Any) -> None:
        """Update a runtime value in config.runtime.yaml."""
        self._set_config_value(self.workspace / "config.runtime.yaml", key, value)

    def set_runtime_source(
        self,
        source: str,
        value: SourceSessionConfig,
    ) -> None:
        """Update runtime source affinity while preserving source as a literal key."""
        self.sources[source] = value
        self._set_mapping_value(
            self.workspace / "config.runtime.yaml",
            "sources",
            source,
            value,
        )

    def remove_runtime_source(self, source: str) -> bool:
        """Remove runtime source affinity while preserving source as a literal key."""
        removed_memory = self.sources.pop(source, None) is not None
        removed_file = self._remove_mapping_value(
            self.workspace / "config.runtime.yaml",
            "sources",
            source,
        )
        return removed_memory or removed_file

    def reload(self) -> bool:
        """Re-read config.user.yaml and merge with runtime."""
        try:
            config_data = self._load_merged_configs(self.workspace)
            config_data["workspace"] = self.workspace

            # Create new instance and copy values
            new_config = Config.model_validate(config_data)

            # Update all fields from new config
            for field_name in Config.model_fields:
                setattr(self, field_name, getattr(new_config, field_name))

            return True
        except Exception as e:
            logging.debug("Config reload failed: %s", e)
            return False  
        

class ConfigHandler(FileSystemEventHandler):
    """Handles config file modification events."""

    def __init__(self, on_change: ConfigChangeCallback):
        self._on_change = on_change

    def on_modified(self, event: FileSystemEvent) -> None:
        """Notify when config.user.yaml changes."""
        if event.is_directory:
            return

        if Path(str(event.src_path)).name != "config.user.yaml":
            return

        try:
            self._on_change()
        except Exception:
            logging.exception("Config change callback failed")


class ConfigReloader:
    """Manages watchdog observer for config hot reload."""

    def __init__(
        self,
        config: Config,
        on_change: ConfigChangeCallback | None = None,
    ):
        self._config = config
        self._on_change = on_change
        self._observer: BaseObserver | None = None

    def set_on_change(self, on_change: ConfigChangeCallback | None) -> None:
        """Set the callback invoked when the watched config changes."""
        self._on_change = on_change

    def _handle_change(self) -> None:
        """Handle a watched config file change."""
        if self._on_change is None:
            return
        self._on_change()

    def start(self) -> None:
        """Start watching config file for changes."""
        if self._observer is not None:
            return

        handler = ConfigHandler(self._handle_change)
        observer: BaseObserver = Observer()
        observer.schedule(handler, str(self._config.workspace), recursive=False)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        """Stop watching."""
        if self._observer is None:
            return

        observer = self._observer
        self._observer = None
        observer.stop()
        observer.join()

    
