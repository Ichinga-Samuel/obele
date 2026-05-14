"""Signal/hook system for model lifecycle events.

Provides a lightweight publish-subscribe mechanism for decoupled
model lifecycle logic::

    from obele import pre_save, post_save, receiver

    @receiver(pre_save, sender=User)
    def hash_password(sender, instance, **kwargs):
        if 'password' in instance.dirty_fields:
            instance.password = bcrypt.hash(instance.password)

    @receiver(post_save)
    def log_save(sender, instance, created, **kwargs):
        print(f"{'Created' if created else 'Updated'} {sender.__name__} pk={instance.pk}")
"""

from __future__ import annotations

import threading
import weakref
from collections import defaultdict
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Model

_Receiver = Callable[..., Any]


class Signal:
    """A signal that receivers can connect to and that senders can emit.

    Thread-safe and supports optional sender filtering.

    Parameters
    ----------
    name:
        Human-readable signal name (used in ``repr``).
    providing_args:
        Documentation-only list of keyword argument names that
        :meth:`send` will provide to receivers.
    """

    __slots__ = ("name", "providing_args", "_receivers", "_lock")

    def __init__(self, name: str = "", providing_args: list[str] | None = None) -> None:
        self.name = name
        self.providing_args = providing_args or []
        self._receivers: dict[Any, list[_Receiver]] = defaultdict(list)
        self._lock = threading.Lock()

    def connect(
        self,
        receiver: _Receiver,
        *,
        sender: type[Model] | None = None,
        weak: bool = False,
    ) -> None:
        """Register *receiver* to be called when this signal is sent.

        Args:
            receiver: Callable ``(sender, instance, **kwargs) -> None``.
            sender: If given, only calls from this specific model class
                trigger the receiver.  ``None`` means all senders.
            weak: If ``True``, store a weak reference so the receiver
                can be garbage-collected without explicit disconnect.
        """
        key = id(sender) if sender is not None else None
        if weak:
            ref = weakref.WeakMethod(receiver) if hasattr(receiver, "__self__") else weakref.ref(receiver)
            fn = ref
        else:
            fn = receiver
        with self._lock:
            self._receivers[key].append(fn)

    def disconnect(
        self,
        receiver: _Receiver,
        *,
        sender: type[Model] | None = None,
    ) -> bool:
        """Remove a previously connected receiver.  Returns ``True`` if found."""
        key = id(sender) if sender is not None else None
        with self._lock:
            receivers = self._receivers.get(key, [])
            for i, fn in enumerate(receivers):
                resolved = fn() if isinstance(fn, weakref.ref) else fn
                if resolved is receiver:
                    receivers.pop(i)
                    return True
        return False

    def send(self, sender: type[Model], **kwargs: Any) -> list[tuple[_Receiver, Any]]:
        """Emit this signal.  Returns ``[(receiver, return_value), ...]``.

        Calls all receivers registered for the specific *sender* **and**
        all receivers registered without a sender filter.
        """
        responses: list[tuple[_Receiver, Any]] = []
        with self._lock:
            # Global receivers (sender=None) + sender-specific receivers
            to_call = list(self._receivers.get(None, []))
            sender_key = id(sender)
            to_call.extend(self._receivers.get(sender_key, []))

        for fn in to_call:
            resolved = fn() if isinstance(fn, weakref.ref) else fn
            if resolved is None:
                continue  # Weak ref was collected
            result = resolved(sender, **kwargs)
            responses.append((resolved, result))
        return responses

    def has_receivers(self, sender: type[Model] | None = None) -> bool:
        """Return ``True`` if any receivers are connected."""
        with self._lock:
            if self._receivers.get(None):
                return True
            if sender is not None and self._receivers.get(id(sender)):
                return True
        return False

    def __repr__(self) -> str:
        total = sum(len(v) for v in self._receivers.values())
        return f"<Signal {self.name!r} receivers={total}>"


# ---- Built-in signals -----------------------------------------------------

pre_save = Signal("pre_save", providing_args=["instance", "created"])
post_save = Signal("post_save", providing_args=["instance", "created"])
pre_delete = Signal("pre_delete", providing_args=["instance"])
post_delete = Signal("post_delete", providing_args=["instance"])
pre_create = Signal("pre_create", providing_args=["instance"])
post_create = Signal("post_create", providing_args=["instance"])


# ---- receiver decorator ---------------------------------------------------

def receiver(
    signal: Signal | list[Signal],
    *,
    sender: type[Model] | None = None,
) -> Callable[[_Receiver], _Receiver]:
    """Decorator to connect a function to one or more signals::

        @receiver(post_save, sender=User)
        def on_user_saved(sender, instance, created, **kwargs):
            ...

        @receiver([pre_save, pre_delete])
        def audit_all(sender, instance, **kwargs):
            ...
    """
    signals = signal if isinstance(signal, (list, tuple)) else [signal]

    def decorator(fn: _Receiver) -> _Receiver:
        for sig in signals:
            sig.connect(fn, sender=sender)
        return fn

    return decorator


__all__ = [
    "Signal",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "pre_create",
    "post_create",
    "receiver",
]
