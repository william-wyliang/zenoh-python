#
# Copyright (c) 2022 ZettaScale Technology
#
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# http://www.eclipse.org/legal/epl-2.0, or the Apache License, Version 2.0
# which is available at https://www.apache.org/licenses/LICENSE-2.0.
#
# SPDX-License-Identifier: EPL-2.0 OR Apache-2.0
#
# Contributors:
#   ZettaScale Zenoh Team, <zenoh@zettascale.tech>
#
import abc
from typing import Generic, Callable, Union, Any, TypeVar, Tuple, List
from threading import Condition, Thread
from collections import deque
import time

In = TypeVar("In")
Out = TypeVar("Out")
Receiver = TypeVar("Receiver")
CallbackCall = Callable[[In], Out]
CallbackDrop = Callable[[], None]

class IClosure(Generic[In, Out]):
    """
    A Closure is a pair of a `call` function that will be used as a callback,
    and a `drop` function that will be called when the closure is destroyed.
    """
    @property
    @abc.abstractmethod
    def call(self) -> Callable[[In], Out]:
        """
        Returns the closure's call function as a lambda.
        """
        ...
    @property
    @abc.abstractmethod
    def drop(self) -> Callable[[], None]:
        """
        Returns the closure's destructor as a lambda.
        """
        ...
    def __enter__(self):
        drop = self.drop
        if drop is not None:
            drop()
    def __exit__(self, *args):
        drop = self.drop
        if drop is not None:
            drop()

class IHandler(Generic[In, Out, Receiver]):
    """
    A Handler is a value that may be converted into a callback closure for zenoh to use on one side, while possibly providing a receiver for the data that zenoh would provide through that callback.
    """
    @property
    @abc.abstractmethod
    def closure(self) -> IClosure[In, Out]:
        """
        The part of the handler that should be passed as a callback to a zenoh function.
        """
        ...
    @property
    @abc.abstractmethod
    def receiver(self) -> Receiver:
        "The part of the handler that should be used as the receiver when the handler is channel-like."
        ...

IntoClosure = Union[IHandler[In, Out, Any], IClosure[In, Out], Tuple[CallbackCall, CallbackDrop], CallbackCall]
class Closure(IClosure, Generic[In, Out]):
    """
    A Closure is a pair of a `call` function that will be used as a callback,
    and a `drop` function that will be called when the closure is destroyed.
    """
    def __init__(self, closure: IntoClosure[In, Out], type_adaptor: Callable[[Any], In] = None):
        _call_ = None
        self._drop_ = lambda: None
        if isinstance(closure, IHandler):
            closure = closure.closure
            # dev-note: do not elif here, the  next if will catch the obtained closure.
        if isinstance(closure, IClosure):
            _call_ = closure.call
            self._drop_ = closure.drop
        elif isinstance(closure, tuple):
            _call_, self._drop_ = closure
        elif callable(closure):
            _call_ = closure
        else:
            raise TypeError("Unexpected type as input for zenoh.Closure")
        if type_adaptor is not None:
            self._call_ = lambda *args: _call_(type_adaptor(*args))
        else:
            self._call_ = _call_
    @property
    def call(self) -> Callable[[In], Out]:
        return self._call_

    @property
    def drop(self) -> Callable[[], None]:
        return self._drop_

IntoHandler = Union[IHandler[In, Out, Receiver], IClosure[In, Out],  Tuple[IClosure, Receiver], Tuple[CallbackCall,CallbackDrop, Receiver], Tuple[CallbackCall,CallbackDrop], CallbackCall]
class Handler(IHandler, Generic[In, Out, Receiver]):
    """
    A Handler is a value that may be converted into a callback closure for zenoh to use on one side, while possibly providing a receiver for the data that zenoh would provide through that callback.
    """
    def __init__(self, input: IntoHandler[In, Out, Receiver], type_adaptor: Callable[[Any], In] = None):
        self._receiver_ = None
        if isinstance(input, IHandler):
            self._receiver_ = input.receiver
            self._closure_ = input.closure
        elif isinstance(input, IClosure):
            self._closure_ = input
        elif isinstance(input, tuple):
            if isinstance(input[0], IClosure):
                self._closure_, self._receiver_ = input
            elif len(input) == 3:
                call, drop, self._receiver_ = input
                self._closure_ = (call, drop)
            else:
                self._closure_ = input
        else:
            self._closure_ = input
        self._closure_ = Closure(self._closure_, type_adaptor)

    @property
    def closure(self) -> IClosure[In, Out]:
        return self._closure_
    @property
    def receiver(self) -> Receiver:
        return self._receiver_



class ListCollector(IHandler[In, None, Callable[[],List[In]]], Generic[In]):
    """
    A simple collector that aggregates values into a list.

    When used as a handler, it provides a callback that appends elements to a list,
    and provides a function that will await the closing of the callback before returning said list.
    """
    def __init__(self, timeout=None):
        self._vec_ = []
        self._cv_ = Condition()
        self._done_ = False
        self.timeout = timeout
    
    @property
    def closure(self):
        def call(x):
            self._vec_.append(x)
        def drop():
            with self._cv_:
                self._done_ = True
                self._cv_.notify()
        return Closure((call, drop))
    
    @property
    def receiver(self):
        def wait():
            with self._cv_:
                if not self._done_:
                    self._cv_.wait(timeout=self.timeout)
                return self._vec_
        return wait

class Queue(IHandler[In, None, 'Queue'], Generic[In]):
    """
    A simple single-producer, single-consumer queue implementation.

    When used as a handler, it provides itself as the receiver, and will provide a
    callback that appends elements to the queue.
    """
    def __init__(self, timeout=None):
        self._vec_ = deque()
        self._cv_ = Condition()
        self._done_ = False
    
    @property
    def closure(self) -> IClosure[In, None]:
        def call(x): self.put(x)
        def drop(): self.close()
        return Closure((call, drop))
    
    @property
    def receiver(self) -> 'Queue':
        return self
    
    def put(self, value):
        """
        Puts one element on the queue.
        """
        with self._cv_:
            self._vec_.append(value)
            self._cv_.notify()


    def get(self, timeout=None):
        """
        Gets one element from the queue.

        Raises a `StopIteration` exception if the queue was closed before the timeout ran out.
        Raises a `TimeoutError` if the timeout ran out.
        """
        try:
            return self._vec_.pop()
        except IndexError:
            pass
        if self._done_:
            raise StopIteration()
        with self._cv_:
            self._cv_.wait(timeout=timeout)
            try:
                return self._vec_.pop()
            except IndexError:
                pass
        if self._done_:
            raise StopIteration()
        else:
            raise TimeoutError()
    
    def close(self):
        with self._cv_:
            self._done_ = True
            self._cv_.notify()
    
    def get_remaining(self, timeout=None) -> List[In]:
        """
        Awaits the closing of the queue, returning the remaining queued values in a list.
        The values inserted into the queue up until this happens will be available through `get`.

        Raises a `TimeoutError` if the timeout in seconds provided was exceeded before closing.
        """
        end = (time.time() + timeout) if timeout is not None else None
        while not self._done_:
            with self._cv_:
                self._cv_.wait(timeout=(timeout - time.time()) if timeout else None)
                if self._done_:
                    return
                elif time.time() >= end:
                    raise TimeoutError()
        return list(self._vec_)

    def __iter__(self):
        return self
    def __next__(self):
        return self.get()

if __name__ == "__main__":
    def get(collector):
        import time
        def target():
            with Closure(collector) as closure:
                closure = Closure(collector)
                closure.call('hi')
                closure.call('there')
        Thread(target=target).start()

    collector = ListCollector()
    get(collector)
    print(collector.receiver())
    assert collector.receiver() == ["hi", "there"]

    print("done")


