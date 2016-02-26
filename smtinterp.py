'''
Translate expressions into SMT via Z3
'''

from language import *
from z3util import *
import z3, operator, logging
import types


logger = logging.getLogger(__name__)

class OpHandler(object):
  '''These essentially act as closures, where the arguments can be read.
  (And modified, so watch out.)

  To add to a class, use

    def MyClass(object):
      field = OpHandler(op)

  Then, MyClass.field returns the OpHandler, and my_obj.field returns a
  method.

  Subclasses must override __call__.
  '''
  def __init__(self, op):
    self.op = op

  def __call__(self, *args):
    raise NotImplementedError

  def __get__(self, obj, cls=None):
    # return a method if invoked as obj.handler.
    if obj:
      return types.MethodType(self, obj, cls)

    return self

class BinOpHandler(OpHandler):
  _fields = ('op', 'defined', 'poisons')
  def __init__(self, op, defined = None, poisons = None):
    self.op = op
    self.poisons = poisons
    self.defined = defined

  def copy(self, **kws):
    dict = {f: kws.get(f, getattr(self, f)) for f in self._fields}
    return type(self)(**dict)

  def __call__(self, obj, term):
    return obj._binary_operator(term, self.op, self.defined, self.poisons)

class FBinOpHandler(OpHandler):
  def __call__(self, obj, term):
    return obj._float_binary_operator(term, self.op)

class MustAnalysis(OpHandler):
  def __call__(self, obj, term):
    return obj._must_analysis(term, self.op)


def _ty_sort(ty):
  'Translate a Type expression to a Z3 Sort'

  if isinstance(ty, IntType):
    return z3.BitVecSort(ty.width)

  return {
    PtrType: z3.BitVecSort(64),
    HalfType: z3.FloatHalf(),
    SingleType: z3.Float32(),
    DoubleType: z3.Float64()}[type(ty)]
    # NOTE: this assumes the global z3 context never changes

class MetaTranslator(MetaVisitor):
  def __init__(cls, name, bases, dict):
    if not hasattr(cls, 'registry'):
      cls.registry = {}

    cls.registry[name.lower()] = cls
    return super(MetaTranslator, cls).__init__(name, bases, dict)

class SMTTranslator(Visitor):
  __metaclass__ = MetaTranslator
  log = logger.getChild('SMTTranslator')

  def __init__(self, type_model):
    self.types = type_model
    self.fresh = 0
    self.defs = []  # current defined-ness conditions
    self.nops = []  # current non-poison conditions
    self.qvars = []

  def eval(self, term):
    '''smt.eval(term) -> Z3 expression

    Translate the term (and subterms), adding its definedness conditons,
    nonpoison conditions, and quantifier variables to the state.
    '''
    self.log.debug('eval %s', term)
    return term.accept(self)

  def __call__(self, term):
    '''smt(term) -> Z3 expression, def conds, nonpoison conds, qvars

    Clear the current state, translate the term (and subterms), and
    return the translation, definedness conditions, nonpoison conditions,
    and quantified variables.

    Quantified variables are guaranteed to be unique between different
    calls to the same SMTTranslator object.
    '''
    self.log.debug('call %s', term)
    self.defs = []
    self.nops = []
    self.qvars = []
    v = term.accept(self)
    return v, self.defs, self.nops, self.qvars

  def add_defs(self, *defs):
    self.defs += defs

  def add_nops(self, *nops):
    self.nops += nops
  
  def add_qvar(self, *qvars):
    self.qvars += qvars

  def type(self, term):
    return self.types[term]

  def fresh_bool(self):
    self.fresh += 1
    return z3.Bool('ana_' + str(self.fresh))

  def fresh_var(self, ty, prefix='undef_'):
    self.fresh += 1
    return z3.Const(prefix + str(self.fresh), _ty_sort(ty))

  def Input(self, term):
    # TODO: unique name check

    ty = self.types[term]
    return z3.Const(term.name, _ty_sort(ty))

  def _binary_operator(self, term, op, defined, poisons):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if defined:
      self.add_defs(*defined(x,y))

    if poisons:
      for f in term.flags:
        self.add_nops(poisons[f](x,y))

    return op(x,y)

  AddInst = BinOpHandler(operator.add,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(1,x)+z3.SignExt(1,y) == z3.SignExt(1,x+y),
       'nuw': lambda x,y: z3.ZeroExt(1,x)+z3.ZeroExt(1,y) == z3.ZeroExt(1,x+y)})

  SubInst = BinOpHandler(operator.sub,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(1,x)-z3.SignExt(1,y) == z3.SignExt(1,x-y),
       'nuw': lambda x,y: z3.ZeroExt(1,x)-z3.ZeroExt(1,y) == z3.ZeroExt(1,x-y)})

  MulInst = BinOpHandler(operator.mul,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(x.size(),x)*z3.SignExt(x.size(),y) == z3.SignExt(x.size(),x*y),
       'nuw': lambda x,y: z3.ZeroExt(x.size(),x)*z3.ZeroExt(x.size(),y) == z3.ZeroExt(x.size(),x*y)})

  SDivInst = BinOpHandler(operator.div,
    defined = lambda x,y: [y != 0, z3.Or(x != (1 << x.size()-1), y != -1)],
    poisons = {'exact': lambda x,y: (x/y)*y == x})

  UDivInst = BinOpHandler(z3.UDiv,
    defined = lambda x,y: [y != 0],
    poisons = {'exact': lambda x,y: z3.UDiv(x,y)*y == x})

  SRemInst = BinOpHandler(z3.SRem,
    defined = lambda x,y: [y != 0, z3.Or(x != (1 << (x.size()-1)), y != -1)])

  URemInst = BinOpHandler(z3.URem,
    defined = lambda x,y: [y != 0])
  
  ShlInst = BinOpHandler(operator.lshift,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons =
      {'nsw': lambda x,y: (x << y) >> y == x,
       'nuw': lambda x,y: z3.LShR(x << y, y) == x})

  AShrInst = BinOpHandler(operator.rshift,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons = {'exact': lambda x,y: (x >> y) << y == x})

  LShrInst = BinOpHandler(z3.LShR,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons = {'exact': lambda x,y: z3.LShR(x, y) << y == x})

  AndInst = BinOpHandler(operator.and_)
  OrInst = BinOpHandler(operator.or_)
  XorInst = BinOpHandler(operator.xor)



  def _float_binary_operator(self, term, op):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if 'nnan' in term.flags:
      self.add_defs(z3.Not(z3.fpIsNaN(x)), z3.Not(z3.fpIsNaN(y)),
        z3.Not(z3.fpIsNaN(op(x,y))))

    if 'ninf' in term.flags:
      self.add_defs(z3.Not(z3.fpIsInfinite(x)), z3.Not(z3.fpIsInfinite(y)),
        z3.Not(z3.fpIsInfinite(op(x,y))))

    if 'nsz' in term.flags:
      # NOTE: this will return a different qvar for each (in)direct reference
      # to this term. Is this desirable?
      b = self.fresh_bool()
      self.add_qvar(b)
      z = op(x,y)
      nz = z3.fpMinusZero(_ty_sort(self.type(term)))
      return z3.If(z3.fpEQ(z,0), z3.If(b, 0, nz), z)

    return op(x,y)

  FAddInst = FBinOpHandler(operator.add)
  FSubInst = FBinOpHandler(operator.sub)
  FMulInst = FBinOpHandler(operator.mul)
  FDivInst = FBinOpHandler(lambda x,y: z3.fpDiv(z3._dflt_rm(), x, y))
  FRemInst = FBinOpHandler(z3.fpRem)


  # NOTE: SExt/ZExt/Trunc should all have IntType args
  def SExtInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg).width
    tgt = self.type(term).width
    return z3.SignExt(tgt - src, v)

  def ZExtInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg).width
    tgt = self.type(term).width
    return z3.ZeroExt(tgt - src, v)

  def TruncInst(self, term):
    v = self.eval(term.arg)
    tgt = self.type(term).width
    return z3.Extract(tgt - 1, 0, v)

  def ZExtOrTruncInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg).width
    tgt = self.type(term).width
    
    if tgt == src:
      return v
    if tgt > src:
      return z3.ZeroExt(tgt - src, v)
    
    return z3.Extract(tgt-1, 0, v)

  # TODO: find better way to do range checks for [su]itofp, fpto[su]i
  def FPtoSIInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg)
    tgt = self.type(term)
    # TODO: fptosi range check

    q = self.fresh_var(tgt)
    self.add_qvar(q)

    x = z3.fpToSBV(z3.RTZ(), v, _ty_sort(tgt))

    return z3.If(
      z3.fpToFP(z3.RTZ(), x, _ty_sort(src)) == z3.fpRoundToIntegral(z3.RTZ(),v),
      x, q)

  def FPtoUIInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg)
    tgt = self.type(term)
    # TODO: fptoui range check

    q = self.fresh_var(tgt)
    self.add_qvar(q)

    x = z3.fpToUBV(z3.RTZ(), v, _ty_sort(tgt))

    return z3.If(
      z3.fpToFPUnsigned(z3.RTZ(), x, _ty_sort(src)) == z3.fpRoundToIntegral(z3.RTZ(),v),
      x, q)

  def SItoFPInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg)
    tgt = self.type(term)

    if src.width + 1 <= tgt.frac:
      return z3.fpToFP(z3.RTZ(), v, _ty_sort(tgt))

    q = self.fresh_var(tgt)
    self.add_qvar(q)

    x = z3.fpToFP(z3.RTZ(), v, _ty_sort(tgt))
    return z3.If(z3.fpToSBV(z3.RTZ(), x, _ty_sort(src)) == v, x, q)

  def UItoFPInst(self, term):
    v = self.eval(term.arg)
    src = self.type(term.arg)
    tgt = self.type(term)

    if src.width < tgt.frac:
      return z3.fpToFPUnsigned(z3.RTZ(), v, _ty_sort(tgt))

    q = self.fresh_var(tgt)
    self.add_qvar(q)

    x = z3.fpToFPUnsigned(z3.RTZ(), v, _ty_sort(tgt))
    return z3.If(z3.fpToUBV(z3.RTZ(), x, _ty_sort(src)) == v, x, q)

  def IcmpInst(self, term):
    x = self.eval(term.x)
    y = self.eval(term.y)

    cmp = {
      'eq': operator.eq,
      'ne': operator.ne,
      'ugt': z3.UGT,
      'uge': z3.UGE,
      'ult': z3.ULT,
      'ule': z3.ULE,
      'sgt': operator.gt,
      'sge': operator.ge,
      'slt': operator.lt,
      'sle': operator.le}[term.pred](x,y)

    return bool_to_BitVec(cmp)

  # TODO: fcmp flags
  def FcmpInst(self, term):
    x = self.eval(term.x)
    y = self.eval(term.y)

    def unordered(op):
      return lambda x,y: z3.Or(op(x,y), z3.fpIsNaN(x), z3.fpIsNaN(y))

    cmp = {
      'false': lambda x,y: z3.BoolVal(False),
      'oeq': z3.fpEQ,
      'ogt': z3.fpGT,
      'oge': z3.fpGEQ,
      'olt': z3.fpLT,
      'ole': z3.fpLEQ,
      'one': z3.fpNEQ,
      'ord': lambda x,y: z3.Not(z3.Or(z3.fpIsNaN(x), z3.fpIsNaN(y))),
      'ueq': unordered(z3.fpEQ),
      'ugt': unordered(z3.fpGT),
      'uge': unordered(z3.fpGEQ),
      'ult': unordered(z3.fpLT),
      'ule': unordered(z3.fpLEQ),
      'une': unordered(z3.fpNEQ),
      'uno': lambda x,y: z3.Or(z3.fpIsNaN(x), z3.fpIsNaN(y)),
      'true': lambda x,y: z3.BoolVal(True),
      }[term.pred](x,y)

    return bool_to_BitVec(cmp)

  def SelectInst(self, term):
    c = self.eval(term.sel)
    x = self.eval(term.arg1)
    y = self.eval(term.arg2)
    
    return z3.If(c == 1, x, y)

  def Literal(self, term):
    ty = self.type(term)
    if isinstance(ty, FloatType):
      return z3.FPVal(term.val, _ty_sort(ty))

    return z3.BitVecVal(term.val, ty.width)

  def FLiteral(self, term):
    ty = self.type(term)
    assert isinstance(ty, FloatType)

    if term.val == 'nz':
      return z3.fpMinusZero(_ty_sort(ty))

    return z3.FPVal(term.val, _ty_sort(ty))


  def UndefValue(self, term):
    ty = self.type(term)
    x = self.fresh_var(ty)
    self.add_qvar(x)
    return x

  # NOTE: constant expressions do no introduce poison or definedness constraints
  #       is this reasonable?
  # FIXME: cnxps need explicit undef checking
  # FIXME: div/rem by 0 is undef
  AddCnxp = BinOpHandler(operator.add)
  SubCnxp = BinOpHandler(operator.sub)
  MulCnxp = BinOpHandler(operator.mul)
  SDivCnxp = BinOpHandler(operator.div)
  UDivCnxp = BinOpHandler(z3.UDiv)
  SRemCnxp = BinOpHandler(operator.mod)
  URemCnxp = BinOpHandler(z3.URem)
  ShlCnxp = BinOpHandler(operator.lshift)
  AShrCnxp = BinOpHandler(operator.rshift)
  LShrCnxp = BinOpHandler(z3.LShR)
  AndCnxp = BinOpHandler(operator.and_)
  OrCnxp = BinOpHandler(operator.or_)
  XorCnxp = BinOpHandler(operator.xor)

  def NotCnxp(self, term):
    return ~self.eval(term.x)

  def NegCnxp(self, term):
    if isinstance(self.type(term), FloatType):
      return z3.fpNeg(self.eval(term.x))

    return -self.eval(term.x)

  def AbsCnxp(self, term):
    x = self.eval(term._args[0])

    if isinstance(self.type(term), FloatType):
      return z3.fpAbs(x)

    return z3.If(x >= 0, x, -x)

  def SignBitsCnxp(self, term):
    x = self.eval(term._args[0])
    ty = self.type(term)

    #b = ComputeNumSignBits(self.fresh_bv(size), size)
    b = self.fresh_var(ty, 'ana_')

    self.add_defs(z3.ULE(b, ComputeNumSignBits(x, ty.width)))

    return b

  def OneBitsCnxp(self, term):
    x = self.eval(term._args[0])
    b = self.fresh_var(self.type(term), 'ana_')

    self.add_defs(b & ~x == 0)

    return b

  def ZeroBitsCnxp(self, term):
    x = self.eval(term._args[0])
    b = self.fresh_var(self.type(term), 'ana_')

    self.add_defs(b & x == 0)

    return b

  def LeadingZerosCnxp(self, term):
    x = self.eval(term._args[0])

    return ctlz(x, self.type(term).width)

  def TrailingZerosCnxp(self, term):
    x = self.eval(term._args[0])
    
    return cttz(x, self.type(term).width)

  def Log2Cnxp(self, term):
    x = self.eval(term._args[0])

    return bv_log2(x, self.type(term).width)

  def LShrFunCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.LShR(x,y)

  def SMaxCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.If(x > y, x, y)

  def UMaxCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.If(z3.UGT(x,y), x, y)

  def SExtCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.type(term).width
    return z3.SignExt(bits - x.size(), x)

  def ZExtCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.type(term).width
    return z3.ZeroExt(bits - x.size(), x)

  def TruncCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.type(term).width
    return z3.Extract(bits-1, 0, x)

  def FPtoSICnxp(self, term):
    x = self.eval(term._args[0])
    tgt = self.type(term)

    return z3.fpToSBV(z3.RTZ(), x, _ty_sort(tgt))

  def FPtoUICnxp(self, term):
    x = self.eval(term._args[0])
    tgt = self.type(term)

    return z3.fpToUBV(z3.RTZ(), x, _ty_sort(tgt))

  def SItoFPCnxp(self, term):
    x = self.eval(term._args[0])
    tgt = self.type(term)

    return z3.fpToFP(z3.RTZ(), x, _ty_sort(tgt))

  def UItoFPCnxp(self, term):
    x = self.eval(term._args[0])
    tgt = self.type(term)

    return z3.fpToFPUnsigned(z3.RTZ(), x, _ty_sort(tgt))

  def WidthCnxp(self, term):
    return z3.BitVecVal(self.type(term._args[0]).width, self.type(term).width)
    # NOTE: nothing bad should happen if we don't evaluate the argument

  def AndPred(self, term):
    return mk_and([self.eval(cl) for cl in term.clauses])

  def OrPred(self, term):
    return mk_or([self.eval(cl) for cl in term.clauses])

  def NotPred(self, term):
    return z3.Not(self.eval(term.p))

  def Comparison(self, term):
    cmp = {
      'eq': operator.eq,
      'ne': operator.ne,
      'ugt': z3.UGT,
      'uge': z3.UGE,
      'ult': z3.ULT,
      'ule': z3.ULE,
      'sgt': operator.gt,
      'sge': operator.ge,
      'slt': operator.lt,
      'sle': operator.le}[term.op]

    return cmp(self.eval(term.x), self.eval(term.y))

  def _must_analysis(self, term, op):
    args = (self.eval(a) for a in term._args)

    if all(isinstance(a, Constant) for a in term._args):
      return op(*args)

    c = self.fresh_bool()
    self.add_defs(z3.Implies(c, op(*args)))
    return c

  def IntMinPred(self, term):
    x = self.eval(term._args[0])

    return x == 1 << (x.size()-1)

  Power2Pred = MustAnalysis(lambda x: z3.And(x != 0, x & (x-1) == 0))
  Power2OrZPred = MustAnalysis(lambda x: x & (x-1) == 0)

  def ShiftedMaskPred(self, term):
    x = self.eval(term._args[0])

    v = (x - 1) | x
    return z3.And(v != 0, ((v+1) & v) == 0)

  MaskZeroPred = MustAnalysis(lambda x,y: x & y == 0)

  NSWAddPred = MustAnalysis(
    lambda x,y: z3.SignExt(1,x) + z3.SignExt(1,y) == z3.SignExt(1,x+y))

  NUWAddPred = MustAnalysis(
    lambda x,y: z3.ZeroExt(1,x) + z3.ZeroExt(1,y) == z3.ZeroExt(1,x+y))

  NSWSubPred = MustAnalysis(
    lambda x,y: z3.SignExt(1,x) - z3.SignExt(1,y) == z3.SignExt(1,x-y))

  NUWSubPred = MustAnalysis(
    lambda x,y: z3.ZeroExt(1,x) - z3.ZeroExt(1,y) == z3.ZeroExt(1,x-y))

  def NSWMulPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    size = x.size()
    return z3.SignExt(size,x) * z3.SignExt(size,y) == z3.SignExt(size,x*y)

  def NUWMulPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    size = x.size()
    return z3.ZeroExt(size,x) * z3.ZeroExt(size,y) == z3.ZeroExt(size,x*y)

  def NUWShlPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.LShR(x << y, y) == x

  def OneUsePred(self, term):
    return z3.BoolVal(True)
    # NOTE: should this have semantics?


class NewShlSemantics(SMTTranslator):
  ShlInst = SMTTranslator.ShlInst.copy(poisons = {
    'nsw': lambda a,b: z3.Or((a << b) >> b == a,
                             z3.And(a == 1, b == b.size() - 1)),
    'nuw': lambda a,b: z3.LShR(a << b, b) == a})

class FastMathUndef(SMTTranslator):
  def _float_binary_operator(self, term, op):
    x = self.eval(term.x)
    y = self.eval(term.y)

    conds = []
    z = op(x,y)
    if 'nnan' in term.flags:
      conds += [z3.Not(z3.fpIsNaN(x)), z3.Not(z3.fpIsNaN(y)),
        z3.Not(z3.fpIsNaN(op(x,y)))]

    if 'ninf' in term.flags:
      conds += [z3.Not(z3.fpIsInfinite(x)), z3.Not(z3.fpIsInfinite(y)),
        z3.Not(z3.fpIsInfinite(op(x,y)))]

    if 'nsz' in term.flags:
      # NOTE: this will return a different qvar for each (in)direct reference
      # to this term. Is this desirable?
      b = self.fresh_bool()
      self.add_qvar(b)
      z = op(x,y)
      nz = z3.fpMinusZero(_ty_sort(self.type(term)))
      z = z3.If(z3.fpEQ(z,0), z3.If(b, 0, nz), z)

    if conds:
      q = self.fresh_var(self.type(term))
      self.add_qvar(q)

      return z3.If(mk_and(conds), z, q)

    return z

class OldNSZ(SMTTranslator):
  def _float_binary_operator(self, term, op):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if 'nnan' in term.flags:
      self.add_defs(z3.Not(z3.fpIsNaN(x)), z3.Not(z3.fpIsNaN(y)),
        z3.Not(z3.fpIsNaN(op(x,y))))

    if 'ninf' in term.flags:
      self.add_defs(z3.Not(z3.fpIsInfinite(x)), z3.Not(z3.fpIsInfinite(y)),
        z3.Not(z3.fpIsInfinite(op(x,y))))

    if 'nsz' in term.flags:
      # NOTE: this will return a different qvar for each (in)direct reference
      # to this term. Is this desirable?
      nz = z3.fpMinusZero(_ty_sort(self.type(term)))
      self.add_defs(z3.Not(x == nz), z3.Not(y == nz))
      return op(x,y)  # turns -0 to +0

    return op(x,y)

class BrokenNSZ(SMTTranslator):
  def _float_binary_operator(self, term, op):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if 'nnan' in term.flags:
      self.add_defs(z3.Not(z3.fpIsNaN(x)), z3.Not(z3.fpIsNaN(y)),
        z3.Not(z3.fpIsNaN(op(x,y))))

    if 'ninf' in term.flags:
      self.add_defs(z3.Not(z3.fpIsInfinite(x)), z3.Not(z3.fpIsInfinite(y)),
        z3.Not(z3.fpIsInfinite(op(x,y))))

    if 'nsz' in term.flags:
      # NOTE: this will return a different qvar for each (in)direct reference
      # to this term. Is this desirable?
      q = self.fresh_var(self.type(term))
      self.add_qvar(q)  # FIXME
      self.add_defs(z3.fpEQ(q,0))
      z = op(x,y)
      return z3.If(z3.fpEQ(z,0), q, z)

    return op(x,y)
