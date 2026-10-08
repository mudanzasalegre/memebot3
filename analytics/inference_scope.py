"""Bound model versions and memoized inputs to one supervised entry decision."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import threading
from typing import Any, Callable, Iterator

MAX_PREDICTIONS = 256


@dataclass
class InferenceScope:
    values: dict[Any, Any] = field(default_factory=dict)
    predictions: dict[Any, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock)
    closed: bool = False


_CURRENT: ContextVar[InferenceScope | None] = ContextVar("entry_inference_scope", default=None)


@contextmanager
def inference_scope() -> Iterator[InferenceScope]:
    """Each candidate owns its versions; nested/concurrent entries are isolated."""
    state = InferenceScope()
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        with state.lock:
            state.closed = True
            state.values.clear()
            state.predictions.clear()
        _CURRENT.reset(token)


@contextmanager
def ensure_inference_scope() -> Iterator[InferenceScope]:
    """Standalone multi-head reads own a scope; entry children reuse theirs."""
    state = _CURRENT.get()
    if state is not None and not state.closed:
        yield state
    else:
        with inference_scope() as state:
            yield state


def scoped_value(key: Any, loader: Callable[[], Any]) -> Any:
    state = _CURRENT.get()
    if state is None or state.closed:
        return loader()
    with state.lock:
        if state.closed:
            return loader()
        if key not in state.values:
            state.values[key] = loader()  # Include unavailable/None observations.
        return state.values[key]


def scoped_snapshot(key: Any, loader: Callable[[], tuple]) -> tuple:
    """Pin model objects, but never expose mutable cached schema/metadata."""
    def capture():
        model, features, metadata = loader()
        return model, tuple(features), deepcopy(metadata)
    model, features, metadata = scoped_value(key, capture)
    return model, list(features), deepcopy(metadata)


def scoped_prediction(key: Any, frame: Any, predict: Callable[[], Any]) -> Any:
    """Cache only the exact ordered numeric inputs the model actually consumes."""
    state = _CURRENT.get()
    if state is None or state.closed:
        return predict()
    inputs = (tuple(frame.columns), tuple(frame.shape),
              sha256(frame.to_numpy(dtype="float32").tobytes()).hexdigest())
    key = (key, inputs)
    with state.lock:
        if state.closed:
            return predict()
        if key not in state.predictions:
            result = predict()
            if len(state.predictions) >= MAX_PREDICTIONS:
                state.predictions.pop(next(iter(state.predictions)))
            state.predictions[key] = result
        return state.predictions[key]
