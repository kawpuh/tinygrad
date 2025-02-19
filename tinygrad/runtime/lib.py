import ctypes
import numpy as np
from typing import TypeVar, Type, Any
from tinygrad.helpers import DType, dtypes, prod, GlobalCounters

_T = TypeVar("_T")
class RawBuffer:  # pylint: disable=abstract-method
  def __init__(self, size:int, dtype:DType, buf:Any=None):
    self.size: int = size
    self.dtype: DType = dtype
    self._buf = buf
    self._memsz: int = size*dtype.itemsize
    GlobalCounters.mem_used += self._memsz
  def __del__(self):  # NOTE: if it fails on init (bad dtype), it won't have a _memsz
    if hasattr(self, '_memsz'): GlobalCounters.mem_used -= self._memsz
  def __repr__(self): return f"buffer<{self.size}, {self.dtype}>"
  @property
  def key(self): return (self.size, self.dtype.key)

  # NOTE: this interface allows for 0 copy
  @classmethod
  def fromCPU(cls:Type[_T], x:np.ndarray) -> _T: raise NotImplementedError("must be implemented")
  def toCPU(self) -> np.ndarray: raise NotImplementedError("must be implemented")

class RawBufferCopyIn(RawBuffer):
  def _copyin(self, x:np.ndarray) -> None: raise NotImplementedError("must be implemented")

  @classmethod
  def fromCPU(cls, x:np.ndarray, **kwargs):
    ret = cls(prod(x.shape), dtypes.from_np(x.dtype), **kwargs)
    ret._copyin(x)
    return ret

class RawBufferMapped(RawBufferCopyIn):
  def _buffer(self) -> memoryview: raise NotImplementedError("must be implemented")
  # NOTE: this metadata prevents the backing buffer from being freed. hack can be removed with PEP688
  def toCPU(self) -> np.ndarray: return np.frombuffer(self._buffer(), dtype=np.dtype(self.dtype.np, metadata={"backing": self}))  # type: ignore
  def _copyin(self, x:np.ndarray) -> None: np.copyto(self.toCPU(), x.reshape(-1))

# this one is simple enough that i moved it out of the runtimes
class RawMallocBuffer(RawBufferMapped):
  def __init__(self, size, dtype: DType): super().__init__(size, dtype, ({dtypes.float64:ctypes.c_double, dtypes.float32: ctypes.c_float, dtypes.float16: ctypes.c_int16, dtypes.bfloat16: ctypes.c_int16, dtypes.int8: ctypes.c_int8, dtypes.uint8: ctypes.c_uint8, dtypes.bool: ctypes.c_uint8, dtypes.int32: ctypes.c_int32, dtypes.uint32: ctypes.c_uint32, dtypes.int64: ctypes.c_int64, dtypes.uint64: ctypes.c_uint64}[dtype] * size)())
  def _buffer(self): return memoryview(self._buf)

class RawBufferCopyInOut(RawBufferCopyIn):
  def _copyout(self, x:np.ndarray) -> None: raise NotImplementedError("must be implemented")

  def toCPU(self) -> np.ndarray:
    x: np.ndarray = np.empty(self.size, dtype=self.dtype.np)
    self._copyout(x)
    return x

class RawBufferTransfer(RawBuffer):
  def _transfer(self, x) -> None: raise NotImplementedError("must be implemented")

  @classmethod
  def transfer(cls, x, shape, dtype, **kwargs):
    ret = cls(prod(shape), dtype, **kwargs)
    ret._transfer(x)
    return ret

class RawConst(RawBuffer): # pylint: disable=abstract-method
  def __repr__(self): return f"const<{self._buf}, {self.dtype}>"
  @property
  def key(self): return (str(self._buf), self.dtype.key)

def buf_is_kernel_arg(x) -> bool:
  return x.realized is not None and x.realized.__class__ is not RawConst
