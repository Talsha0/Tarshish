"""Plugin contracts.

Core depends only on these abstract base classes; every concrete source,
generator, publisher and storage backend lives outside ``core/`` and is
discovered by :mod:`core.registry`. To add a capability, drop a module with one
concrete subclass into the matching package — no change to ``core/`` required:

    plugins/sources/     -> BaseSource      (product feeds: AliExpress, Amazon, Admitad…)
    plugins/generators/  -> BaseGenerator   (LLM text, video rendering…)
    plugins/publishers/  -> BasePublisher   (Telegram, TikTok, YouTube Shorts, Instagram…)
    storage/             -> BaseStorage     (local disk, S3, Cloudinary…)

A plugin is referenced from pipeline configuration by its module name
(``plugins/sources/aliexpress_mock.py`` -> ``"aliexpress_mock"``) unless the
class sets ``plugin_name``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Collection, Mapping

from core.models import ContentType, GeneratedContent, Product, PublishReceipt, PublishTarget

if TYPE_CHECKING:
    from core.config import Settings
    from core.resilience import BudgetGuard


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin instance may need, injected by the registry."""

    settings: "Settings"
    pipeline_id: str | None = None
    llm_model: str | None = None                 # opaque model id / provider alias for generators
    options: Mapping[str, Any] = field(default_factory=dict)   # free-form per-pipeline plugin options
    dry_run: bool = False                        # True: no paid API calls, no side effects
    budget_guard: "BudgetGuard | None" = None    # call ensure_within_budget() before paid LLM requests
    storage: "BaseStorage | None" = None


class BaseSource(ABC):
    """Fetches candidate products for a niche."""

    plugin_name: ClassVar[str | None] = None

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Stable identifier stored in ``Product.source``."""

    @abstractmethod
    def fetch_products(self, niche: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        """Return up to ``limit`` products matching the filters."""


class BaseGenerator(ABC):
    """Turns a product into publishable content."""

    plugin_name: ClassVar[str | None] = None
    content_type: ClassVar[ContentType] = "text"

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    @property
    @abstractmethod
    def generator_name(self) -> str:
        """Stable identifier of the generator."""

    @abstractmethod
    def generate(self, product: Product, language: str, prompt_template: str | None) -> GeneratedContent:
        """Produce content for ``product`` in ``language``.

        ``prompt_template`` is the pipeline's optional custom prompt; ``None``
        means "use the generator's default".
        """

    @classmethod
    def supported_models(cls) -> list[str]:
        """Model ids / aliases this generator understands (suggestions for admin UIs)."""
        return []


class BasePublisher(ABC):
    """Delivers content to one page/channel of one platform."""

    plugin_name: ClassVar[str | None] = None
    # Credential names the publisher cannot work without (e.g. ("token", "chat_id")).
    required_credentials: ClassVar[tuple[str, ...]] = ()
    # Fallback env-var names used when a social page does not reference its own.
    default_env_vars: ClassVar[Mapping[str, str]] = {}

    def __init__(self, context: PluginContext, target: PublishTarget) -> None:
        self.context = context
        self.target = target

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Stable identifier stored in ``publications.platform``."""

    @abstractmethod
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        """Publish and return a receipt. May raise; the caller isolates failures."""


class BaseStorage(ABC):
    """Persists generated media and returns where it can be found."""

    plugin_name: ClassVar[str | None] = None

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings

    @abstractmethod
    def save(self, local_path: str, remote_key: str) -> str:
        """Store ``local_path`` under ``remote_key``; return its URI (path or public URL)."""

    @abstractmethod
    def delete(self, remote_key: str) -> bool:
        """Remove the object; return True when something was deleted."""

    def cleanup(self, protected: Collection[str] = ()) -> int:
        """Apply the backend's retention policy; return the number of removed objects.

        ``protected`` lists URIs still referenced by unpublished content.
        """
        return 0
