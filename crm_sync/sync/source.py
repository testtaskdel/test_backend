from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping


class CrmSource(ABC):
    """Интерфейс источника данных CRM."""

    @abstractmethod
    def iter_customers(self) -> Iterable[Mapping[str, Any]]:
        """Возвращает поток покупателей."""

    @abstractmethod
    def iter_leads(self) -> Iterable[Mapping[str, Any]]:
        """Возвращает сделки потоковым итератором."""


class CallableCrmSource(CrmSource):
    """Подключает готовые функции-источники данных."""

    def __init__(self, customers_factory, leads_factory):
        self._customers_factory = customers_factory
        self._leads_factory = leads_factory

    def iter_customers(self):
        return self._customers_factory()

    def iter_leads(self):
        return self._leads_factory()
