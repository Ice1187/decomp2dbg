#
# ██████╗ ███████╗ ██████╗ ██████╗ ███╗   ███╗██████╗ ██████╗  ██████╗ ███████╗███████╗
# ██╔══██╗██╔════╝██╔════╝██╔═══██╗████╗ ████║██╔══██╗╚════██╗██╔════╝ ██╔════╝██╔════╝
# ██║  ██║█████╗  ██║     ██║   ██║██╔████╔██║██████╔╝ █████╔╝██║  ███╗█████╗  █████╗
# ██║  ██║██╔══╝  ██║     ██║   ██║██║╚██╔╝██║██╔═══╝ ██╔═══╝ ██║   ██║██╔══╝  ██╔══╝
# ██████╔╝███████╗╚██████╗╚██████╔╝██║ ╚═╝ ██║██║     ███████╗╚██████╔╝███████╗██║
# ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚══════╝ ╚═════╝ ╚══════╝╚═╝
# by mahaloz, 2021.
#
#
# decomp2gef is a plugin to bring a decompiler interface to GEF.
#
# Hacking Around:
# This script packs a lot in just a few lines. If you are trying to modify how decompilation is printed or
# break-like events trigger decompiler callbacks, look at the decompiler ContextPane. For server requsts things,
# or decoding, look in the Decompiler class.
#


import tempfile
import textwrap
import typing
import xmlrpc.client
import functools
import struct
import os


#
# Helper Functions
#

def rebase_addr(addr, up=False):
    """
    Rebases an address to be either in the domain of the base of the binary in GDB VMMAP or
    to be just an offset.

    up -> make an offset to a absolute address
    down -> make an absolute address to an offset
    """
    vmmap = get_process_maps()
    base_address = min([x.page_start for x in vmmap if x.path == get_filepath()])
    checksec_status = checksec(get_filepath())
    pie = checksec_status["PIE"]  # if pie we will have offset instead of abs address.
    corrected_addr = addr
    if pie:
        if up:
            corrected_addr += base_address
        else:
            corrected_addr -= base_address

    return corrected_addr


def only_if_decompiler_connected(f):
    """
    Decorator wrapper to check if Decompiler is online. The _decompiler_ should exist in the
    global namespace before any instance of this is called, which is assured in this file.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if _decompiler_ and _decompiler_.connected:
            return f(*args, **kwargs)

    return wrapper

#
# Symbol Mapping
#

class SymbolMapper:
    """
    A mapper for native symbols
    """

    __slots__ = (
        '_elf_cache',
        '_objcopy',
        '_gcc'
    )

    def __init__(self):
        self._elf_cache = {}
        self._objcopy = None
        self._gcc = None

    #
    # Native Symbol Support (Linux Only)
    # Inspired by Bata24
    #

    def add_native_symbols(self, sym_info_list):
        """
        Adds a list of symbols to gdb's internal symbol listing. Only function and global symbols are supported.
        Symbol info looks like:
        (symbol_name: str, base_addr: int, sym_type: str, size: int)
        If you don't know the size, pass 0.

        Explanation of how this works:
        Adding symbols to GDB is non-trivial, it requires the use of an entire object file. Because of its
        difficulty, this is currently only supported on ELFs. When adding a symbol, we use two binutils,
        gcc and objcopy. After making a small ELF, we strip it of everything but needed sections. We then
        use objcopy to one-by-one add a symbol to the file. Objcopy does not support sizing, so we do a byte
        patch on the binary to allow for a real size. Finally, the whole object is read in with the default
        gdb command: add-symbol-file.
        """

        if not self.check_native_symbol_support():
            err("Native symbol support not supported on this platform.")
            info("If you are on Linux and want native symbol support make sure you have gcc and objcopy.")
            return False

        #info("{:d} symbols will be added".format(len(sym_info_list)))

        # locate the base address of the binary
        vmmap = get_process_maps()
        text_base = min([x.page_start for x in vmmap if x.path == get_filepath()])

        # add each symbol into a mass symbol commit
        max_commit_size = 1000
        supported_types = ["function", "object"]

        objcopy_cmds = []
        queued_sym_sizes = {}
        fname = self._construct_small_elf()
        for i, (name, addr, typ, size) in enumerate(sym_info_list):
            if typ not in supported_types:
                warn("Skipping symbol {}, type is not supported: {}".format(name, typ))
                continue

            # queue the sym for later use
            queued_sym_sizes[i] = size

            # absolute addressing
            if addr >= text_base:
                addr_str = "{:#x}".format(addr)
            # relative addressing
            else:
                addr_str = ".text:{:#x}".format(addr)

            # create a symbol command for the symbol
            objcopy_cmds.append(
                "--add-symbol '{name}'={addr_str},global,{type_flag}".format(
                    name=name, addr_str=addr_str, type_flag=typ
                )
            )

            # batch commit
            if i > 1 and not i % max_commit_size:
                # add the queued symbols
                self._add_symbol_file(fname, objcopy_cmds, text_base, queued_sym_sizes)

                # re-init queues and elf
                fname = self._construct_small_elf(text_base)
                objcopy_cmds = []
                queued_sym_sizes = {}

        # commit remaining symbol commands
        if objcopy_cmds:
            self._add_symbol_file(fname, objcopy_cmds, text_base, queued_sym_sizes)

        return True

    def check_native_symbol_support(self):
        # validate binutils bins exist
        try:
            self._gcc = which("gcc")
            self._objcopy = which("objcopy")
        except FileNotFoundError as e:
            err("Binutils binaries not found: {}".format(e))
            return False

        return True

    def _construct_small_elf(self):
        if self._elf_cache:
            open(self._elf_cache["fname"], "wb").write(self._elf_cache["data"])
            return self._elf_cache["fname"]

        # compile a small elf for symbol loading
        fd, fname = tempfile.mkstemp(dir="/tmp", suffix=".c")
        os.fdopen(fd, "w").write("int main() {}")
        #os.system(f"{self._gcc} {fname} -no-pie -o {fname}.debug")
        os.system(f"{self._gcc} {fname} -o {fname}.debug")
        # destroy the source file
        os.unlink(f"{fname}")

        # delete unneeded sections from object file
        os.system(f"{self._objcopy} --only-keep-debug {fname}.debug")
        os.system(f"{self._objcopy} --strip-all {fname}.debug")
        elf = get_elf_headers(f"{fname}.debug")

        required_sections = [".text", ".interp", ".rela.dyn", ".dynamic", ".bss"]
        for s in elf.shdrs:
            # keep some required sections
            if s.sh_name in required_sections:
                continue

            os.system(f"{self._objcopy} --remove-section={s.sh_name} {fname}.debug 2>/dev/null")

        # cache the small object file for use
        self._elf_cache["fname"] = fname + ".debug"
        self._elf_cache["data"] = open(self._elf_cache["fname"], "rb").read()
        return self._elf_cache["fname"]

    def _force_update_text_size(self, elf_data, new_size):
        # XXX: this is bad and only works on 64bit elf
        default_text_size = 0x92
        elf_rev = elf_data[::-1]
        size_off = elf_rev.find(default_text_size)
        patch = struct.pack("<Q", new_size)
        size_off = len(elf_rev) - size_off - 1

        elf_data[size_off:size_off+len(patch)] = patch
        return elf_data

    def _force_update_sym_sizes(self, fname, queued_sym_sizes):
        # parsing based on: https://github.com/torvalds/linux/blob/master/include/uapi/linux/elf.h
        get_elf_headers.cache_clear()
        elf: Elf = get_elf_headers(fname)
        elf_data = bytearray(open(fname, "rb").read())

        # patch .text to seem large enough for any function
        elf_data = self._force_update_text_size(elf_data, 0xFFFFFF)

        # find the symbol table
        for section in elf.shdrs:
            if section.sh_name == ".symtab":
                break
        else:
            return

        # locate the location of the symbols size in the symtab
        tab_offset = section.sh_offset
        sym_data_size = 24 if elf.ELF_64_BITS else 16
        sym_size_off = sym_data_size - 8

        for i, size in queued_sym_sizes.items():
            # skip sizes of 0
            if not size:
                continue

            # compute offset
            sym_size_loc = tab_offset + sym_data_size * (i + 5) + sym_size_off
            pack_str = "<Q" if elf.ELF_64_BITS else "<I"
            # write the new size
            updated_size = struct.pack(pack_str, size)
            elf_data[sym_size_loc:sym_size_loc + len(updated_size)] = updated_size

        # write data back to elf
        open(fname, "wb").write(elf_data)

    def _add_symbol_file(self, fname, cmd_string_arr, text_base, queued_sym_sizes):
        # first delete any possible symbol files
        try:
            gdb.execute("remove-symbol-file -a {:#x}".format(text_base))
        except Exception:
            pass

        # add the symbols through copying
        cmd_string = ' '.join(cmd_string_arr)
        os.system(f"{self._objcopy} {cmd_string} {fname}")

        # force update the size of each symbol
        self._force_update_sym_sizes(fname, queued_sym_sizes)

        gdb.execute(f"add-symbol-file {fname} {text_base:#x}", to_string=True)

        os.unlink(fname)
        return


_decomp_sym_tab_ = SymbolMapper()


#
# Generic Decompiler Interface
#

class Decompiler:
    def __init__(self, name="decompiler", host="127.0.0.1", port=3662):
        self.name = name
        self.host = host
        self.port = port
        self.server = None
        self.native_symbol_support = True

    #
    # Server Ops
    #

    @property
    def connected(self):
        return True if self.server else False

    def connect(self, name="decompiler", host="127.0.0.1", port=3662) -> bool:
        """
        Connects to the remote decompiler.
        """
        self.name = name
        self.host = host
        self.port = port

        # create a decompiler server connection and test it
        try:
            self.server = xmlrpc.client.ServerProxy("http://{:s}:{:d}".format(host, port))
            self.server.ping()
        except (ConnectionRefusedError, AttributeError) as e:
            self.server = None
            return False

        # import global symbols for the first time
        self.native_symbol_support = _decomp_sym_tab_.check_native_symbol_support()
        worked = self.get_and_set_global_info()
        if not worked:
            info(f"Native symbol support failed for the above reasons, switching to non-native support.")
            self.native_symbol_support = False

        return True

    @only_if_decompiler_connected
    def disconnect(self):
        try:
            self.server.disconnect()
        except Exception:
            pass

        self.server = None

    #
    # Decompilation Server Requests
    #

    def global_info(self):
        """
        Will get the global information associated with the decompiler. Things like:
        - [X] function headers
        - [ ] structs
        - [ ] enums

        Return format:
        {
            "function_headers":
            {
                "<some_func_name>":
                {
                    "name": str
                    "base_addr": int
                    "size": int
                }
            }
        }
        """
        return self.server.global_info()

    @only_if_decompiler_connected
    def decompile(self, addr) -> typing.Dict:
        """
        Decompiles an address that may be in a function boundary. Returns a dict like the following:
        {
            "code": Optional[List[str]],
            "func_name": str,
            "line": int
        }

        Code should be the full decompilation of the function the address is within. If not in a function, None is
        also acceptable.
        """
        return self.server.decompile(rebase_addr(addr))

    @only_if_decompiler_connected
    def get_stack_vars(self, addr) -> typing.Dict:
        """
        Gets all the stack vars associated with the function addr is in. If addr is not in a function, will return None
        for the function addr. Returns a dict like the following:
        {
            "func_addr": Optional[str],
            "members":
            [
                {
                    "offset": int,
                    "name": str,
                    "size": int,
                    "type": str
                }
            ]
        }

        All offsets will be negative offsets of the base pointer.
        """
        return self.server.get_stack_vars(rebase_addr(addr))

    @only_if_decompiler_connected
    def get_structs(self) -> typing.List[typing.Dict]:
        """
        Gets all the structs defined by the decompiler or user. Returns a dict like the following:
        [
            {
                "struct_name": str,
                "size": int,
                "members":
                [
                    {
                        "offset": int,
                        "name": str,
                        "size": int,
                        "type": str
                    }
                ]
            }
            ...
        ]
        """
        return self.server.get_structs()

    @only_if_decompiler_connected
    def set_comment(self, cmt, addr, decompilation=False) -> bool:
        """
        Sets a comment in either disassembly or decompilation based on the address.
        Returns whether it was successful or not.
        """
        return self.server.set_comment(cmt, rebase_addr(addr), decompilation)

    #
    # GDB Info Setters
    #

    def get_and_set_global_info(self):
        try:
            resp = self.global_info()
        except Exception as e:
            err("Failed to get globals from server {}".format(e))
            return False

        funcs_info = resp['function_headers']

        # add symbols with native support if possible
        if self.native_symbol_support:
            funcs_to_add = []
            for _, func_info in funcs_info.items():
                funcs_to_add.append((func_info["name"], func_info["base_addr"], "function", func_info["size"]))

            try:
                _decomp_sym_tab_.add_native_symbols(funcs_to_add)
            except Exception as e:
                err("Failed to set symbols natively: {}".format(e))
                self.native_symbol_support = False
                return False

            return True
        return False


_decompiler_ = Decompiler()


#
# GEF Context Pane for Decompiler
#

class DecompilerCTXPane:
    def __init__(self, decompiler):
        self.decompiler = decompiler

        self.ready_to_display = False
        self.decomp_lines = []
        self.curr_line = -1
        self.curr_func = ""
        self.lvars = []
        self.args = []

        # XXX: this needs to be removed in the future
        self.stop_global_import = False

        # XXX: this needs to be removed in the future
        self.stop_global_import = False

    def _decompile_cur_pc(self, pc):
        # update global info
        if not self.stop_global_import:
            try:
                self.decompiler.get_and_set_global_info()
            except Exception:
                err("Decompiler failed to import global symbols")
                self.stop_global_import = True

        # decompile PC
        try:
            resp = self.decompiler.decompile(pc)
        except Exception as e:
            return False

        code = resp['code']
        if not code:
            return False

        self.decomp_lines = code
        self.curr_func = resp["func_name"]
        self.curr_line = resp["line"]
        self.lvars = resp["lvars"]
        self.args = resp["args"]

        self.set_local_vars(pc)

        return True

    def set_local_vars(self, pc):

        for arg in self.args:
            expr = f"""(({arg['type']}) {current_arch.function_parameters[arg['index']]}"""
            try:
                val = gdb.parse_and_eval(expr)
                gdb.execute(f"set ${arg['name']} {val}")
            except Exception as e:
                pass
                #gdb.execute(f'set ${arg["name"]} NA')

        for lvar in self.lvars:
            if "__" in  lvar["type"]:
                lvar["type"] = lvar["type"].replace("__", "")
                idx = lvar["type"].find("[")
                if idx != -1:
                    lvar["type"] = lvar["type"][:idx] + "_t" + lvar["type"][idx:]
                else:
                    lvar["type"] += "_t"
            lvar["type"] = lvar["type"].replace("unsigned ", "u")

            expr = f"""({lvar['type']}*) ($fp -  {lvar['offset']})"""

            try:
                gdb.execute(f"set ${lvar['name']} = " + expr)
            except Exception as e:
                gdb.execute(f"set ${lvar['name']} = ($fp - {lvar['offset']})")


    def display_pane(self):
        """
        Display the current decompilation, with an arrow next to the current line.
        """
        if not self.decompiler.connected:
            return

        if not self.ready_to_display:
            err("Unable to decompile function")
            return

        # configure based on source config
        past_lines_color = get_gef_setting("theme.old_context")
        nb_lines = get_gef_setting("context.nb_lines_code")
        cur_line_color = get_gef_setting("theme.source_current_line")

        # use GEF source printing method
        for i in range(self.curr_line - nb_lines + 1, self.curr_line + nb_lines):
            if i < 0:
                continue

            if i < self.curr_line:
                gef_print(
                    "{}".format(Color.colorify("  {:4d}\t {:s}".format(i + 1, self.decomp_lines[i], ), past_lines_color))
                )

            if i == self.curr_line:
                prefix = "{}{:4d}\t ".format(RIGHT_ARROW[1:], i + 1)
                gef_print(Color.colorify("{}{:s}".format(prefix, self.decomp_lines[i]), cur_line_color))

            if i > self.curr_line:
                try:
                    gef_print("  {:4d}\t {:s}".format(i + 1, self.decomp_lines[i], ))
                except IndexError:
                    break
        return

    def title(self):
        """
        Special note: this function is always called before display_pane
        """
        if not self.decompiler.connected:
            return None

        self.ready_to_display = self._decompile_cur_pc(current_arch.pc)

        if self.ready_to_display:
            title = "decompiler:{:s}:{:s}:{:d}".format(self.decompiler.name, self.curr_func, self.curr_line+1)
        else:
            title = "decomipler:{:s}:error".format(self.decompiler.name)

        return title


_decompiler_ctx_pane_ = DecompilerCTXPane(_decompiler_)


#
# GEF Command Interface for Decompiler
#

class DecompilerCommand(GenericCommand):
    """The command interface for the remote Decompiler"""
    _cmdline_ = "decompiler"
    _syntax_ = "{:s} [connect | disconnect]".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) < 2:
            self._handle_help(None)
            return

        cmd = argv[0]
        args = argv[1:]
        self._handle_cmd(cmd, args)

    #
    # Decompiler command handlers
    #

    def _handle_cmd(self, cmd, args):
        handler_str = "_handle_{}".format(cmd)
        if hasattr(self, handler_str):
            handler = getattr(self, handler_str)
            handler(args)
        else:
            self._handler_failed("command does not exist")

    def _handle_connect(self, args):
        if len(args) != 1:
            self._handler_failed("not enough args")
            return

        connected = _decompiler_.connect(name=args[0])
        if not connected:
            err("Failed to connect to decompiler!")
            return

        info("Connected to decompiler!")

        # register the context_pane after connection
        register_external_context_pane("decompilation", _decompiler_ctx_pane_.display_pane, _decompiler_ctx_pane_.title)

    def _handle_disconnect(self, args):
        _decompiler_.disconnect()
        info("Disconnected decompiler!")

    @only_if_decompiler_connected
    def _handle_global_info(self, args):
        if len(args) != 1:
            self._handler_failed("not enough args")
            return

        op = args[0]

        # import global info
        if op == "import":
            _decompiler_.get_and_set_global_info()
            return

    def _handle_help(self, args):
        usage_str = """\
        Usage: decompiler <command>

        Commands:
            [] connect <name>
                Connects the decomp2gef plugin to the decompiler. After a succesful connect, a decompilation pane
                will be visible that will get updated with global decompiler info on each break-like event.
                
                * name = name of the decompiler, can be anything

            [] disconnect
                Disconnects the decomp2gef plugin. Not needed to stop decompiler, but useful.

            [] global_info [import | status]
                Does operations on the global_info present in the decompiler. Things like function names, global
                symbols, and structs. Only use this when you think the displayed symbols are wrong.

                - import
                    Imports the global info from the decompiler and sets it in GDB. Take 2 steps to be visible. 

                - status
                    Shows all the symbols currently loaded in the symbol map if native support is not on. Only
                    use when native symbol support is disabled.

        Examples:
            decompiler connect ida
            decompiler global_info import
            decompiler disconnect

        """
        gef_print(textwrap.dedent(usage_str))

    def _handler_failed(self, error):
        gef_print("[!] Failed to handle decompiler command: {}.".format(error))
        self._handle_help(None)


register_external_command(DecompilerCommand())