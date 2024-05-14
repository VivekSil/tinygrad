import ctypes, time
from extra.mockgpu.gpu import VirtGPU
from tinygrad.helpers import to_mv, init_c_struct_t
import tinygrad.runtime.autogen.amd_gpu as amd_gpu

SDMA_MAX_COPY_SIZE = 0x400000

BASE_ADDR = 0x00001260
PACKET3_SET_SH_REG_START = 0x2c00
SUB = PACKET3_SET_SH_REG_START - BASE_ADDR

regCOMPUTE_PGM_LO = 0x1bac - SUB
regCOMPUTE_USER_DATA_0 = 0x1be0 - SUB
regCOMPUTE_START_X = 0x1ba4 - SUB

CACHE_FLUSH_AND_INV_TS_EVENT = 0x14

WAIT_REG_MEM_FUNCTION_ALWAYS = 0
WAIT_REG_MEM_FUNCTION_EQ = 3 # ==
WAIT_REG_MEM_FUNCTION_GEQ = 5 # >=

remu = ctypes.CDLL("/usr/local/lib/libremu.so")
remu.run_asm.restype = ctypes.c_uint32
remu.run_asm.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]

def create_sdma_packets():
  # TODO: clean up this, if we want to keep it
  structs = {}
  for name,pkt in [(name,s) for name,s in amd_gpu.__dict__.items() if name.startswith("struct_SDMA_PKT_") and name.endswith("_TAG")]:
    names = set()
    fields = []
    for pkt_fields in pkt._fields_:
      if not pkt_fields[0].endswith("_UNION"): fields.append(pkt_fields)
      else:
        assert pkt_fields[1]._fields_[0][0] == '_0'
        for union_fields in pkt_fields[1]._fields_[0][1]._fields_:
          fname = union_fields[0]
          if fname in names: fname = pkt_fields[0]+fname
          names.add(fname)
          # merge together 64-bit fields, otherwise just append them
          if fname.endswith("_63_32") and fields[-1][0].endswith("_31_0"): fields[-1] = tuple([fname[:-6], ctypes.c_ulong, 64])
          else: fields.append(tuple([fname, *union_fields[1:]]))
    new_name = name[16:-4].lower()
    structs[new_name] = init_c_struct_t(tuple(fields))
    assert ctypes.sizeof(structs[new_name]) == ctypes.sizeof(pkt), f"{ctypes.sizeof(structs[new_name])} != {ctypes.sizeof(pkt)}"
  return type("SDMA_PKTS", (object, ), structs)
sdma_pkts = create_sdma_packets()

class AMDQueue():
  def __init__(self, base, size, rptr, wptr):
    self.queue, self.size = to_mv(base, size).cast("I"), size
    self.rptr = to_mv(rptr, 8).cast("Q")
    self.wptr = to_mv(wptr, 8).cast("Q")

class PM4Executor(AMDQueue):
  def __init__(self, gpu, base, size, rptr, wptr): 
    self.gpu = gpu
    super().__init__(base, size, rptr, wptr)

  def _next_dword(self):
    x = self.queue[self.rptr[0] % (self.size // 4)]
    self.rptr[0] += 1
    return x

  def execute(self):
    while self.rptr[0] < self.wptr[0]:
      cont = True
      header = self._next_dword()
      packet_type = header >> 30
      op = (header >> 8) & 0xFF
      n = (header >> 16) & 0x3FFF
      assert packet_type == 3, "Can parse only packet3"
      if op == amd_gpu.PACKET3_SET_SH_REG: self._exec_set_sh_reg(n) 
      elif op == amd_gpu.PACKET3_ACQUIRE_MEM: self._exec_acquire_mem(n)
      elif op == amd_gpu.PACKET3_RELEASE_MEM: self._exec_release_mem(n)
      elif op == amd_gpu.PACKET3_WAIT_REG_MEM: cont = self._exec_wait_reg_mem(n)
      elif op == amd_gpu.PACKET3_DISPATCH_DIRECT: self._exec_dispatch_direct(n)
      else: raise RuntimeError(f"PM4: Unknown opcode: {op}")
      if not cont: return

  def _exec_acquire_mem(self, n):
    assert n == 6
    for _ in range(7): self._next_dword() # TODO: implement

  def _exec_release_mem(self, n):
    assert n == 6
    mem_event_type = (self._next_dword() >> 0) & 0xff
    selectors = self._next_dword()
    mem_data_sel = (selectors >> 29) & 0b111
    int_sel = (selectors >> 24) & 0b11
    mem_dst_sel = (selectors >> 16) & 0b1
    addr_lo = self._next_dword()
    addr_hi = self._next_dword()
    val_lo = self._next_dword()
    val_hi = self._next_dword()
    val = val_lo + (val_hi << 32)
    ev = self._next_dword()

    ptr = to_mv(addr_lo + (addr_hi << 32), 8)
    if mem_data_sel == 1 or mem_data_sel == 2: ptr.cast('Q')[0] = val
    elif mem_data_sel == 3:
      if mem_event_type == CACHE_FLUSH_AND_INV_TS_EVENT: ptr.cast('I')[0] = int(time.perf_counter())
      else: raise RuntimeError(f"Unknown {mem_data_sel=} {mem_event_type=}") 
    else: raise RuntimeError(f"Unknown {mem_data_sel=}")

  def _exec_wait_reg_mem(self, n):
    assert n == 5
    info = self._next_dword()
    addr_lo = self._next_dword()
    addr_hi = self._next_dword()
    val = self._next_dword()
    mask = self._next_dword()
    timeout = self._next_dword()

    mem_function = (info >> 0) & 0b111
    mem_space = (info >> 4) & 0b1
    mem_op = (info >> 6) & 0b1
    mem_engine = (info >> 8) & 0b1

    if mem_space == 0: read_op = lambda: val
    elif mem_space == 1: read_op = lambda: to_mv(addr_lo + (addr_hi << 32), 4).cast('I')[0]

    if mem_function == WAIT_REG_MEM_FUNCTION_GEQ: cmp = lambda x,y: x >= y
    elif mem_function == WAIT_REG_MEM_FUNCTION_EQ: cmp = lambda x,y: x == y
    else: raise RuntimeError(f"Do not support {mem_function=}")

    mval = read_op()
    can_cont = cmp(mval, val)
    if not can_cont: self.rptr[0] = self.rptr[0] - 7 # revert packet, need to wait again
    return can_cont

  def _exec_set_sh_reg(self, n):
    reg = self._next_dword()
    for i in range(n):
      self.gpu.regs[reg] = self._next_dword()
      reg += 1

  def _exec_dispatch_direct(self, n):
    assert n == 3
    gl = [self._next_dword() for _ in range(3)]
    flags = self._next_dword()

    prg_addr = (self.gpu.regs[regCOMPUTE_PGM_LO] + (self.gpu.regs[regCOMPUTE_PGM_LO + 1] << 32)) << 8
    args_addr = self.gpu.regs[regCOMPUTE_USER_DATA_0] + (self.gpu.regs[regCOMPUTE_USER_DATA_0 + 1] << 32)
    lc = [self.gpu.regs[i] for i in range(regCOMPUTE_START_X+3, regCOMPUTE_START_X+6)]

    prg_sz = 0
    for st,sz in self.gpu.mapped_ranges:
      if st <= prg_addr <= st+sz: prg_sz = sz - (prg_addr - st)

    assert prg_sz > 0, "Invalid prg ptr (not found in mapped ranges)"
    remu.run_asm(prg_addr, prg_sz, *gl, *lc, args_addr)

class SDMAExecutor(AMDQueue):
  def __init__(self, gpu, base, size, rptr, wptr): 
    self.gpu, self.base = gpu, base
    super().__init__(base, size, rptr, wptr)

  def execute(self):
    while self.rptr[0] < self.wptr[0]:
      cont = True
      header = self.queue[(self.rptr[0] // 4) % (self.size // 4)]
      op = (header >> 0) & 0xff
      if op == 0: self.rptr[0] += 4
      elif op == amd_gpu.SDMA_OP_FENCE: self._execute_fence()
      elif op == amd_gpu.SDMA_OP_TRAP: self._execute_trap()
      elif op == amd_gpu.SDMA_OP_POLL_REGMEM: cont = self._execute_poll_regmem()
      elif op == amd_gpu.SDMA_OP_GCR: self._execute_gcr()
      elif op == amd_gpu.SDMA_OP_COPY: self._execute_copy()
      else: raise RuntimeError(f"Unknown SDMA op {op}")
      if not cont: return

  def _execute_fence(self):
    struct = sdma_pkts.fence.from_address(self.base + self.rptr[0] % self.size)
    to_mv(struct.addr, 8).cast('Q')[0] = struct.data
    self.rptr[0] += ctypes.sizeof(struct)

  def _execute_trap(self):
    struct = sdma_pkts.trap.from_address(self.base + self.rptr[0] % self.size)
    self.rptr[0] += ctypes.sizeof(struct)

  def _execute_poll_regmem(self):
    struct = sdma_pkts.poll_regmem.from_address(self.base + self.rptr[0] % self.size)

    if struct.mem_poll == 0: read_op = lambda: struct.value
    elif struct.mem_poll == 1: read_op = lambda: to_mv(struct.addr, 4).cast('I')[0]

    if struct.func == WAIT_REG_MEM_FUNCTION_GEQ: cmp = lambda x,y: x >= y
    elif struct.func == WAIT_REG_MEM_FUNCTION_EQ: cmp = lambda x,y: x == y
    elif struct.func == WAIT_REG_MEM_FUNCTION_ALWAYS: cmp = lambda x,y: True
    else: raise RuntimeError(f"Do not support {struct.func=}")

    mval = read_op() & struct.mask
    if not cmp(mval, struct.value): return False

    self.rptr[0] += ctypes.sizeof(struct)
    return True

  def _execute_gcr(self):
    struct = sdma_pkts.gcr.from_address(self.base + self.rptr[0] % self.size)
    self.rptr[0] += ctypes.sizeof(struct)

  def _execute_copy(self):
    struct = sdma_pkts.copy_linear.from_address(self.base + self.rptr[0] % self.size)
    ctypes.memmove(struct.dst_addr, struct.src_addr, struct.count + 1)
    self.rptr[0] += ctypes.sizeof(struct)

class AMDGPU(VirtGPU):
  def __init__(self, gpuid):
    super().__init__(gpuid)
    self.mapped_ranges = set()
    self.queues = []

  def map_range(self, vaddr, size): self.mapped_ranges.add((vaddr, size))
  def unmap_range(self, vaddr, size): self.mapped_ranges.remove((vaddr, size))
  def add_pm4_queue(self, base, size, rptr, wptr):
    self.queues.append(PM4Executor(self, base, size, rptr, wptr))
    return len(self.queues) - 1
  def add_sdma_queue(self, base, size, rptr, wptr):
    self.queues.append(SDMAExecutor(self, base, size, rptr, wptr))
    return len(self.queues) - 1

gpu_props = """cpu_cores_count 0
simd_count 192
mem_banks_count 1
caches_count 206
io_links_count 1
p2p_links_count 5
cpu_core_id_base 0
simd_id_base 2147488032
max_waves_per_simd 16
lds_size_in_kb 64
gds_size_in_kb 0
num_gws 64
wave_front_size 32
array_count 12
simd_arrays_per_engine 2
cu_per_simd_array 8
simd_per_cu 2
max_slots_scratch_cu 32
gfx_target_version 110000
vendor_id 4098
device_id 29772
location_id 34304
domain 0
drm_render_minor {drm_render_minor}
hive_id 0
num_sdma_engines 2
num_sdma_xgmi_engines 0
num_sdma_queues_per_engine 6
num_cp_queues 8
max_engine_clk_fcompute 2482
local_mem_size 0
fw_version 2140
capability 671588992
debug_prop 1495
sdma_fw_version 20
unique_id 11673270660693242239
num_xcc 1
max_engine_clk_ccompute 2400"""