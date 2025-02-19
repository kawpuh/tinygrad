from typing import Tuple, List, NamedTuple, Any, Dict, Optional, Union, DefaultDict, cast
from tinygrad.codegen.linearizer import UOps, Token, ConstOp, MemOp, UOp
from tinygrad.ops import BinaryOps, UnaryOps
from tinygrad.helpers import DType, dtypes, DEBUG
from tinygrad.shape.symbolic import Variable, NumNode, MulNode, DivNode, ModNode, LtNode, SumNode, AndNode
import functools
import math
from collections import defaultdict

_type_to_letter = {dtypes.float32: 'f', dtypes.bool: 'p', dtypes.int32: 'i', dtypes.int64: 'a', dtypes.uint32: 'u', dtypes.uint64: 'b', dtypes._float4: 'x'}
def type_to_letter(x): return _type_to_letter[x[0]].upper() if x[1] else _type_to_letter[x[0]]

class Register(NamedTuple):
  nm:str
  dtype:DType
  scalar:bool
  off:Optional[int] = None
  def __repr__(self): return self.nm if self.off is None else f"{self.nm}:{self.off}"
  def subregs(self):
    if self.dtype == dtypes._float4:
      return [Register(self.nm, dtypes.float, False, off=off) for off in range(4)]
    return []

class AssemblyInstruction(NamedTuple):
  op: UOps
  out: Optional[Register]
  vin: List[Union[Register, int, float]]
  arg: Any = None

# warp size of 32, s registers are shared across the warp, v are 32-wide vectors
class AssemblyLanguage:
  supports_load3: bool = False
  sin_is_sin2pi: bool = False
  no_div: bool = False
  #TODO: these should be global vars
  cnts:DefaultDict[Tuple[DType, bool], int] = defaultdict(int)
  tor: Dict[Any, Register] = {}
  ins: List[AssemblyInstruction] = []

  def newreg(self, tok, dtype=dtypes.float32, scalar=False):
    if isinstance(tok, Token): dtype = tok.dtype  # this
    self.tor[tok] = ret = Register(f"%{type_to_letter((dtype, scalar))}{self.cnts[(dtype, scalar)]}", dtype, scalar)
    if dtype == dtypes._float4:
      for off in range(4):
        self.tor[Token(tok.name, tok.dtype, off)] = Register(ret.nm, dtypes.float, ret.scalar, off)
    self.cnts[(dtype, scalar)] += 1
    return ret

  def render_numnode(self, b):
    key = ("num", b)
    if key not in self.tor: self.ins.append(AssemblyInstruction(UOps.LOAD, self.newreg(key, scalar=True, dtype=dtypes.int32), [], b))
    return self.tor[key]

  def render_alu(self, op, a:Register, b:Union[Register, int, float], dtype=dtypes.int32) -> Register:
    key = (op, a, b)
    if key not in self.tor:
      #if not isinstance(b, Register): b = render_numnode(b)
      self.ins.append(AssemblyInstruction(UOps.ALU, self.newreg(key, dtype=dtype, scalar=a.scalar and (not isinstance(b, Register) or b.scalar)), [a, b], op))
    return self.tor[key]

  def render_cast(self, a:Register, new_dtype:DType) -> Register:
    if a.dtype == new_dtype: return a
    key = (a, new_dtype)
    if key not in self.tor:
      self.ins.append(AssemblyInstruction(UOps.CAST, self.newreg(key, dtype=new_dtype), [a]))
    return self.tor[key]

  render_ops: Any = { Variable: lambda self, ops, ctx: ctx.tor[self], NumNode: lambda self, ops, ctx: ctx.render_numnode(self.b),
                 MulNode: lambda self, ops, ctx: ctx.render_alu(BinaryOps.MUL, self.a.render(ops, ctx), self.b),
                 DivNode: lambda self, ops, ctx: ctx.render_alu(BinaryOps.DIV, self.a.render(ops, ctx), self.b),
                 ModNode: lambda self, ops, ctx: ctx.render_alu(BinaryOps.MOD, self.a.render(ops, ctx), self.b),
                 LtNode: lambda self, ops, ctx: ctx.render_alu(BinaryOps.CMPLT, self.a.render(ops, ctx), self.b, dtype=dtypes.bool),
    SumNode: lambda self,ops,ctx: functools.reduce(lambda a,b: ctx.render_alu(BinaryOps.ADD, a, b.render(ops,ctx)), self.nodes[1:], self.nodes[0].render(ops,ctx)),
    AndNode: lambda self,ops,ctx: functools.reduce(lambda a,b: ctx.render_alu(BinaryOps.MUL, a, b.render(ops,ctx), dtype=dtypes.bool), self.nodes[1:], self.nodes[0].render(ops,ctx)) }

  def addr_w_offset(self, args):
    assert isinstance(args, MemOp)
    idx = args.idx*args.memory_dtype.itemsize
    off = 0  # TODO: should this be None?
    if isinstance(idx, SumNode):
      nums = [n.b for n in idx.nodes if isinstance(n, NumNode)]
      if len(nums) > 0 and nums[0] < 4096 and (idx-nums[0]).min >= 0:  # TODO: different for each GPU?
        idx -= nums[0]
        off = cast(int, nums[0])
    reg = idx.render(self.render_ops, self)
    if self.supports_load3:
      if reg.scalar:
        new_reg = self.newreg((reg.nm, 'vec'), dtype=reg.dtype)
        self.ins.append(AssemblyInstruction(UOps.ALU, new_reg, [reg], UnaryOps.NOOP))
        reg = new_reg
      return self.tor[args.name], reg, off
    reg = self.render_alu(BinaryOps.ADD, self.render_cast(reg, dtypes.uint64), self.tor[args.name], dtype=dtypes.uint64)
    return reg, None, off

def uops_to_asmstyle(lang, function_name:str, uops:List[UOp]):
  #TODO: Do not use clear()
  lang.ins.clear()
  lang.tor.clear()
  buf_to_dtype = {args[0]:args[1] for uop,_,_,args in uops if uop == UOps.DEFINE_GLOBAL}
  buf_index = {x:i for i,x in enumerate(buf_to_dtype.keys())}
  global_size, local_size = [], []
  skipload_branch = 0
  lang.ins += [AssemblyInstruction(UOps.SPECIAL, lang.newreg(buf, dtype=dtypes.uint64, scalar=True), [], buf) for buf in buf_to_dtype]
  for uop,newvar,vin,args in uops:
    if uop == UOps.DEFINE_LOCAL:
      lang.ins.append(AssemblyInstruction(UOps.DEFINE_LOCAL, None, [], args))
      lang.ins.append(AssemblyInstruction(UOps.ALU, lang.newreg(args[0], dtype=dtypes.uint64), [args[0]], UnaryOps.NOOP))
    elif uop == UOps.LOOP:
      if args[1] == "global":
        for i,var in enumerate(args[0]):
          global_size.append(var.max+1)
          lang.ins.append(AssemblyInstruction(UOps.SPECIAL, lang.newreg(var, dtype=dtypes.int32), [], f"gid{len(args[0])-1-i}"))
      elif args[1] == "local":
        for i,var in enumerate(args[0]):
          local_size.append(var.max+1)
          lang.ins.append(AssemblyInstruction(UOps.SPECIAL, lang.newreg(var, dtype=dtypes.int32), [], f"lid{len(args[0])-1-i}"))
      else:
        for var in args[0]:
          if not isinstance(var, NumNode):  # TODO: why is this coming through?
            lang.ins.append(AssemblyInstruction(UOps.LOAD, lang.newreg(var, dtype=dtypes.int32, scalar=True), [], 0)) #FIXME: what should valid be here?
            lang.ins.append(AssemblyInstruction(UOps.LABEL, None, [], "$loop_"+var.expr))
    elif uop == UOps.ENDLOOP:
      if args[1] not in ["global", "local", "global+local"]:
        for var in reversed(args[0]):
          if not isinstance(var, NumNode):  # TODO: why is this coming through?
            lang.ins.append(AssemblyInstruction(UOps.ALU, lang.tor[var], [lang.tor[var], 1], BinaryOps.ADD))
            pred = lang.render_alu(BinaryOps.CMPLT, lang.tor[var], var.max+1, dtypes.bool)
            lang.ins.append(AssemblyInstruction(UOps.COND_BRANCH, None, [pred], ("$loop_"+var.expr, True)))
      elif args[1] == "global+local":
        for i, var in enumerate(reversed(args[0])):
          lang.ins.append(AssemblyInstruction(UOps.ENDLOOP, None, [lang.tor[var]], (var.max+1, f"gid{i}")))

    elif uop == UOps.CAST and newvar is not None:
      # TODO: we should reconsider outputting CAST in the linearizer. these are needless copies
      out = lang.newreg(newvar)
      for i,sr in enumerate(out.subregs()):
        lang.ins.append(AssemblyInstruction(UOps.ALU, sr, [lang.tor[vin[i]]], UnaryOps.NOOP))
    elif uop == UOps.ALU and newvar is not None:
      out = lang.newreg(newvar) if newvar not in lang.tor else lang.tor[newvar]
      # this is the only thing that can violate SSA
      if args in [BinaryOps.CMPLT]:
        pred_reg = lang.newreg((newvar, 'pred'), dtype=dtypes.bool)
        lang.ins.append(AssemblyInstruction(UOps.ALU, pred_reg, [lang.tor[x] for x in vin], args))
        lang.ins.append(AssemblyInstruction(UOps.CAST, out, [pred_reg], args))
      elif args == BinaryOps.DIV and lang.no_div:
        tmp = lang.newreg((newvar, "rcp"))
        lang.ins.append(AssemblyInstruction(UOps.ALU, tmp, [lang.tor[vin[1]]], UnaryOps.RECIP))
        lang.ins.append(AssemblyInstruction(UOps.ALU, out, [lang.tor[vin[0]], tmp], BinaryOps.MUL))
      elif args == UnaryOps.SIN and lang.sin_is_sin2pi:
        tmp = lang.newreg((newvar, "2pi"))
        lang.ins.append(AssemblyInstruction(UOps.ALU, tmp, [lang.tor[vin[0]], 1/(math.pi*2)], BinaryOps.MUL))
        lang.ins.append(AssemblyInstruction(UOps.ALU, out, [tmp], args))
      else:
        lang.ins.append(AssemblyInstruction(UOps.ALU, out, [lang.tor[x] for x in vin], args))
    elif uop == UOps.LOAD and newvar is not None:
      if isinstance(args, ConstOp):
        if args.valid.min == 0 and args.valid.max == 1:
          lang.ins.append(AssemblyInstruction(UOps.LOAD, lang.newreg(newvar, dtype=newvar.dtype), [], args.invalid_value))
          pred = args.valid.render(lang.render_ops, lang)
          lang.ins.append(AssemblyInstruction(UOps.COND_BRANCH, None, [pred], (f"$skipload_{skipload_branch}", False)))
          lang.ins.append(AssemblyInstruction(UOps.LOAD, lang.newreg(newvar, dtype=newvar.dtype), [], args.value))
          lang.ins.append(AssemblyInstruction(UOps.LABEL, None, [], f"$skipload_{skipload_branch}"))
          skipload_branch += 1
        else:
          lang.ins.append(AssemblyInstruction(UOps.LOAD, lang.newreg(newvar, dtype=newvar.dtype), [], args.value if args.valid.min == 1 else args.invalid_value))
      else:
        idx, treg, off = lang.addr_w_offset(args)
        reg = lang.newreg(newvar, dtype=newvar.dtype, scalar=(idx.scalar and (not isinstance(treg, Register) or treg.scalar))) # and not dtypes.is_float(newvar.dtype)))
        if args.valid.min == 0:
          lang.ins.append(AssemblyInstruction(UOps.LOAD, reg, [], 0))
          if args.valid.max == 1:
            pred = args.valid.render(lang.render_ops, lang)
            lang.ins.append(AssemblyInstruction(UOps.COND_BRANCH, None, [pred], (f"$skipload_{skipload_branch}", False)))
        if args.valid.max == 1:
            # NOTE: you can't compute the index in here, because it assumes it's all available later
          lang.ins.append(AssemblyInstruction(UOps.LOAD, reg, [idx] + ([treg] if treg is not None else []), (off, 'global' if buf_index[args.name] != -1 else 'shared', args.memory_dtype if buf_to_dtype[args.name] != dtypes.float else None)))
        if args.valid.min == 0 and args.valid.max == 1:
          lang.ins.append(AssemblyInstruction(UOps.LABEL, None, [], f"$skipload_{skipload_branch}"))
          skipload_branch += 1
    elif uop == UOps.STORE:
      idx, treg, off = lang.addr_w_offset(args)
      lang.ins.append(AssemblyInstruction(UOps.STORE, None, [idx, lang.tor[vin[0]]] + ([treg] if treg is not None else []), (off, 'global' if buf_index[args.name] != -1 else 'shared', args.memory_dtype if buf_to_dtype['data0'] != dtypes.float else None)))
  # define registers
  lang.ins = [AssemblyInstruction(UOps.DEFINE_REGISTER, None, [], (dtype, type_to_letter(dtype), c)) for dtype,c in lang.cnts.items()] + lang.ins

  if DEBUG >= 4:
    for tins in lang.ins: print(tins)
  return global_size, local_size